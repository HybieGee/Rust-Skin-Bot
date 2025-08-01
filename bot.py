#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-User Rust Skin First-Time Creator Telegram Bot
Monitors SCMM API for new items from first-time creators
Supports multiple users with individual sessions and configurations
"""

import os
import json
import sqlite3
import asyncio
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class RustSkinTelegramBot:
    def __init__(self):
        self.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        self.api_base = "https://rust.scmm.app/api"
        
        # Bot state - now per user
        self.user_sessions = {}  # user_id -> session data
        self.known_creators = set()  # Global creator cache
        self.monitoring_tasks = {}  # user_id -> asyncio task
        
        # Initialize database
        self.init_database()
        self.load_global_state()
        
        # Setup telegram application
        self.application = Application.builder().token(self.bot_token).build()
        self.setup_handlers()
    
    def init_database(self):
        """Initialize SQLite database with multi-user support"""
        self.conn = sqlite3.connect('rust_skin_bot.db', check_same_thread=False)
        cursor = self.conn.cursor()
        
        # User sessions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_sessions (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                steam_session_token TEXT,
                is_monitoring BOOLEAN DEFAULT FALSE,
                purchased_count INTEGER DEFAULT 0,
                max_purchases INTEGER DEFAULT 10,
                auto_purchase BOOLEAN DEFAULT TRUE,
                max_price_cents INTEGER DEFAULT 1000,
                max_item_age_days INTEGER DEFAULT 7,
                test_mode BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Creators table (global)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS creators (
                creator_id TEXT PRIMARY KEY,
                creator_name TEXT,
                first_seen TIMESTAMP,
                skin_count INTEGER DEFAULT 1
            )
        ''')
        
        # Purchases table (per user)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                skin_id TEXT,
                creator_id TEXT,
                creator_name TEXT,
                skin_name TEXT,
                purchase_time TIMESTAMP,
                price REAL,
                success BOOLEAN,
                FOREIGN KEY (user_id) REFERENCES user_sessions (user_id)
            )
        ''')
        
        # Processed skins per user
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_skins (
                user_id INTEGER,
                skin_id TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, skin_id),
                FOREIGN KEY (user_id) REFERENCES user_sessions (user_id)
            )
        ''')
        
        self.conn.commit()
    
    def load_global_state(self):
        """Load global creator data"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT creator_id FROM creators")
        self.known_creators = {row[0] for row in cursor.fetchall()}
        logger.info(f"Loaded {len(self.known_creators)} known creators from database")
    
    def get_user_session(self, user_id: int, username: str = None):
        """Get or create user session"""
        if user_id not in self.user_sessions:
            # Load from database
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM user_sessions WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            
            if row:
                self.user_sessions[user_id] = {
                    'user_id': row[0],
                    'username': row[1],
                    'steam_session_token': row[2],
                    'is_monitoring': row[3],
                    'purchased_count': row[4],
                    'max_purchases': row[5],
                    'auto_purchase': row[6] if len(row) > 6 else True,
                    'max_price_cents': row[7] if len(row) > 7 else 1000,
                    'max_item_age_days': row[8] if len(row) > 8 else 7,
                    'test_mode': row[9] if len(row) > 9 else False,
                    'processed_skins': set()
                }
                
                # Load processed skins for this user
                cursor.execute("SELECT skin_id FROM processed_skins WHERE user_id = ?", (user_id,))
                self.user_sessions[user_id]['processed_skins'] = {row[0] for row in cursor.fetchall()}
            else:
                # Create new user session
                self.user_sessions[user_id] = {
                    'user_id': user_id,
                    'username': username,
                    'steam_session_token': None,
                    'is_monitoring': False,
                    'purchased_count': 0,
                    'max_purchases': 10,
                    'auto_purchase': True,
                    'max_price_cents': 1000,
                    'max_item_age_days': 7,
                    'test_mode': False,
                    'processed_skins': set()
                }
                
                # Save to database
                cursor.execute('''
                    INSERT INTO user_sessions (user_id, username) 
                    VALUES (?, ?)
                ''', (user_id, username))
                self.conn.commit()
        
        return self.user_sessions[user_id]
    
    def update_user_session(self, user_id: int, **kwargs):
        """Update user session in memory and database"""
        session = self.get_user_session(user_id)
        
        # Update session data
        for key, value in kwargs.items():
            if key in session:
                session[key] = value
        
        # Update database
        cursor = self.conn.cursor()
        
        # Build dynamic SQL for updates
        updates = []
        values = []
        
        for key, value in kwargs.items():
            if key in ['steam_session_token', 'is_monitoring', 'purchased_count', 'auto_purchase', 'max_price_cents', 'test_mode']:
                updates.append(f"{key} = ?")
                values.append(value)
        
        if updates:
            updates.append("last_active = CURRENT_TIMESTAMP")
            values.append(user_id)
            
            sql = f"UPDATE user_sessions SET {', '.join(updates)} WHERE user_id = ?"
            cursor.execute(sql, values)
            self.conn.commit()
    
    def setup_handlers(self):
        """Setup Telegram bot handlers"""
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("settoken", self.set_token_command))
        self.application.add_handler(CommandHandler("monitor", self.start_monitoring_command))
        self.application.add_handler(CommandHandler("stop", self.stop_monitoring_command))
        self.application.add_handler(CommandHandler("purchases", self.purchases_command))
        self.application.add_handler(CommandHandler("reset", self.reset_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        username = update.effective_user.username or update.effective_user.first_name
        session = self.get_user_session(user_id, username)
        
        keyboard = [
            [InlineKeyboardButton("📊 My Status", callback_data="status"),
             InlineKeyboardButton("🛍️ My Purchases", callback_data="purchases")],
            [InlineKeyboardButton("🔑 Set Steam Token", callback_data="settoken"),
             InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("▶️ Start Monitoring", callback_data="startbot"),
             InlineKeyboardButton("⏹️ Stop Monitoring", callback_data="stopbot")],
            [InlineKeyboardButton("🧪 Test Mode", callback_data="test_mode"),
             InlineKeyboardButton("❓ Help", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_emoji = "🟢" if session['is_monitoring'] else "🔴"
        token_emoji = "✅" if session['steam_session_token'] else "❌"
        test_emoji = "🧪" if session.get('test_mode', False) else "💰"
        
        # Prepare text parts to avoid backslashes in f-strings
        monitoring_status = 'Active' if session['is_monitoring'] else 'Stopped'
        token_status = 'Configured' if session['steam_session_token'] else 'Not Set'
        auto_purchase_status = '✅ Enabled' if session.get('auto_purchase', True) else '❌ Disabled'
        mode_status = '🧪 Test Mode (No Purchases)' if session.get('test_mode', False) else '💰 Live Mode'
        max_price = session.get('max_price_cents', 1000) / 100
        
        test_mode = session.get('test_mode', False)
        action_description = 'Show you opportunities without purchasing (TEST MODE)' if test_mode else 'Automatically purchase items within your price limit'
        notification_type = 'findings' if test_mode else 'purchases/opportunities'
        
        quick_start_1 = 'Enable 🧪 Test Mode to scan without purchasing' if not test_mode else "You're in test mode - perfect for testing!"
        quick_start_3 = "I'll show you what I find without buying anything!" if test_mode else 'Set your Steam token and configure auto-purchase'
        
        welcome_text = f"""🤖 *Welcome to Rust Skin Auto-Purchase Bot!*

👋 Hello {username}! I find AND buy new skins from first-time creators automatically!

📊 **Your Status:**
{status_emoji} **Monitoring**: {monitoring_status}
{token_emoji} **Steam Token**: {token_status}
🤖 **Auto Purchase**: {auto_purchase_status}
{test_emoji} **Mode**: {mode_status}
💰 **Max Price**: ${max_price:.2f}
🎯 **Progress**: {session['purchased_count']}/{session['max_purchases']} items

🎨 **What I Do:**
• Monitor SCMM for new items from first-time creators
• Only consider items that are 7 days old or newer
• {action_description}
• Send instant notifications of {notification_type}
• Track progress and stop after 10 successful actions

**🚀 Quick Start:**
1️⃣ {quick_start_1}
2️⃣ Start monitoring with ▶️ Start Monitoring
3️⃣ {quick_start_3}

Use the buttons below or type /help for more info."""

        await update.message.reply_text(
            welcome_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        user_id = update.effective_user.id
        session = self.get_user_session(user_id)
        
        status_text = f"""📊 *Your Bot Status*

🤖 **Current State:**
Status: {'🟢 Active' if session['is_monitoring'] else '🔴 Stopped'}
Steam Token: {'✅ Set' if session['steam_session_token'] else '❌ Not Set'}
Mode: {'🧪 Test Mode' if session.get('test_mode', False) else '💰 Live Mode'}

📈 **Progress:**
Opportunities Found: {session['purchased_count']}/{session['max_purchases']}
Processed Items: {len(session['processed_skins'])}

⚙️ **Settings:**
Auto Purchase: {'✅ Enabled' if session.get('auto_purchase', True) else '❌ Disabled'}
Max Price: ${session.get('max_price_cents', 1000)/100:.2f}
Max Item Age: {session.get('max_item_age_days', 7)} days"""

        await update.message.reply_text(status_text, parse_mode='Markdown')
    
    async def set_token_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /settoken command"""
        context.user_data['waiting_for_token'] = True
        
        token_text = """🔑 *Set Your Steam Session Token*

**How to get your token:**
1. Login to Steam in your browser
2. Open Developer Tools (F12)
3. Go to Application → Cookies → steamcommunity.com
4. Find 'sessionid' cookie and copy its value

**Now send me your token:**"""
        
        await update.message.reply_text(token_text, parse_mode='Markdown')
    
    async def start_monitoring_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /monitor command"""
        user_id = update.effective_user.id
        session = self.get_user_session(user_id)
        
        if session['is_monitoring']:
            await update.message.reply_text("⚠️ You're already monitoring! Use /stop to stop.")
            return
        
        if not session.get('test_mode', False) and not session['steam_session_token']:
            await update.message.reply_text("❌ Please set your Steam token first with /settoken\n\n(Or enable test mode with the 🧪 Test Mode button)")
            return
        
        if session['purchased_count'] >= session['max_purchases']:
            await update.message.reply_text(f"🛑 You've reached the limit of {session['max_purchases']} opportunities! Use /reset to reset your counter.")
            return
        
        # Start monitoring
        self.update_user_session(user_id, is_monitoring=True)
        task = asyncio.create_task(self.monitor_user_skins(user_id))
        self.monitoring_tasks[user_id] = task
        
        mode_text = "🧪 TEST MODE" if session.get('test_mode', False) else "💰 LIVE MODE"
        await update.message.reply_text(f"🚀 *Monitoring started in {mode_text}!*\n\nI'm now scanning for first-time creator opportunities.", parse_mode='Markdown')
    
    async def stop_monitoring_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop command"""
        user_id = update.effective_user.id
        session = self.get_user_session(user_id)
        
        if not session['is_monitoring']:
            await update.message.reply_text("⚠️ You're not currently monitoring.")
            return
        
        # Stop monitoring
        self.update_user_session(user_id, is_monitoring=False)
        if user_id in self.monitoring_tasks:
            self.monitoring_tasks[user_id].cancel()
            del self.monitoring_tasks[user_id]
        
        await update.message.reply_text("⏹️ *Monitoring stopped.*", parse_mode='Markdown')
    
    async def purchases_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /purchases command"""
        user_id = update.effective_user.id
        
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT skin_name, creator_name, price, purchase_time, success 
            FROM purchases 
            WHERE user_id = ?
            ORDER BY purchase_time DESC 
            LIMIT 10
        ''', (user_id,))
        purchases = cursor.fetchall()
        
        if not purchases:
            await update.message.reply_text("📭 *No opportunities found yet.*\n\nStart monitoring to begin!", parse_mode='Markdown')
        else:
            text = "🛍️ *Your Recent Opportunities*\n\n"
            for skin_name, creator_name, price, purchase_time, success in purchases:
                status = "✅" if success else "🔍"
                price_text = f"${price:.2f}" if price > 0 else "N/A"
                text += f"{status} **{skin_name}** by {creator_name} - {price_text}\n"
            
            await update.message.reply_text(text, parse_mode='Markdown')
    
    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /reset command"""
        user_id = update.effective_user.id
        
        # Reset user data
        self.update_user_session(user_id, purchased_count=0)
        session = self.get_user_session(user_id)
        session['processed_skins'].clear()
        
        # Clear processed skins from database
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM processed_skins WHERE user_id = ?", (user_id,))
        self.conn.commit()
        
        await update.message.reply_text("✅ *Your progress has been reset!* You can now find 10 more opportunities.", parse_mode='Markdown')
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """🤖 *Rust Skin Auto-Purchase Bot - Help*

**🔧 Commands:**
/start - Main menu and status
/monitor - Start monitoring
/stop - Stop monitoring
/status - Check status
/purchases - View opportunities
/settoken - Set Steam token
/reset - Reset counter
/help - This help

**🎯 How it works:**
1. Monitors SCMM API for new items
2. Targets first-time creators (≤1 item)
3. Only considers recent items (≤7 days)
4. Auto-purchases within your price limit
5. Tracks up to 10 opportunities per user

**🧪 Test Mode:**
Enable to scan without purchasing - perfect for testing!"""

        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        
        try:
            if query.data == "status":
                await self.show_status_inline(query)
            elif query.data == "purchases":
                await self.show_purchases_inline(query)
            elif query.data == "settoken":
                await self.show_settoken_inline(query, context)
            elif query.data == "settings":
                await self.show_settings_menu(query)
            elif query.data == "toggle_auto_purchase":
                await self.toggle_auto_purchase(query)
            elif query.data == "set_max_price":
                await self.set_max_price_prompt(query, context)
            elif query.data == "startbot":
                await self.start_monitoring_inline(query)
            elif query.data == "stopbot":
                await self.stop_monitoring_inline(query)
            elif query.data == "help":
                await self.show_help_inline(query)
            elif query.data == "test_mode":
                await self.toggle_test_mode(query)
            elif query.data == "reset":
                await self.show_reset_confirmation(query)
            elif query.data.startswith("reset_confirm_"):
                user_id_to_reset = int(query.data.split("_")[-1])
                if user_id == user_id_to_reset:
                    # Reset user data
                    self.update_user_session(user_id, purchased_count=0)
                    session = self.get_user_session(user_id)
                    session['processed_skins'].clear()
                    
                    # Clear processed skins from database
                    cursor = self.conn.cursor()
                    cursor.execute("DELETE FROM processed_skins WHERE user_id = ?", (user_id,))
                    self.conn.commit()
                    
                    await query.edit_message_text("✅ Your data has been reset! You can now find 10 more opportunities.", parse_mode='Markdown')
            elif query.data == "reset_cancel":
                await query.edit_message_text("❌ Reset cancelled.")
            elif query.data == "back_main":
                await self.show_main_menu_inline(query)
            else:
                await query.edit_message_text("❌ Unknown command. Use /start to return to main menu.")
        except Exception as e:
            logger.error(f"Error in button callback: {e}")
            await query.edit_message_text("❌ Something went wrong. Use /start to return to main menu.")
    
    async def show_settoken_inline(self, query, context):
        """Show settoken inline"""
        context.user_data['waiting_for_token'] = True
        
        text = """🔑 *Set Your Steam Session Token*

**How to get your token:**
1. Login to Steam in your browser
2. Open Developer Tools (F12)
3. Go to Application → Cookies → steamcommunity.com
4. Find 'sessionid' cookie and copy its value

**Now send me your token** (it will be stored securely):

⚠️ *Make sure you're in a private chat - don't share tokens in groups!*"""
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def show_settings_menu(self, query):
        """Show settings menu"""
        user_id = query.from_user.id
        session = self.get_user_session(user_id)
        
        auto_status = "✅ ENABLED" if session['auto_purchase'] else "❌ DISABLED"
        max_price = session['max_price_cents'] / 100
        
        keyboard = [
            [InlineKeyboardButton(f"🤖 Auto Purchase: {auto_status}", callback_data="toggle_auto_purchase")],
            [InlineKeyboardButton(f"💰 Max Price: ${max_price:.2f}", callback_data="set_max_price")],
            [InlineKeyboardButton("🔑 Update Steam Token", callback_data="settoken")],
            [InlineKeyboardButton("🔄 Reset Progress", callback_data="reset")],
            [InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Prepare variables to avoid backslashes in f-strings
        auto_text = '   • Items will be purchased automatically' if session['auto_purchase'] else '   • You will only get notifications'
        
        settings_text = f"""⚙️ *Your Bot Settings*

🤖 **Auto Purchase**: {auto_status}
{auto_text}

💰 **Max Price**: ${max_price:.2f}
   • Won't buy items above this price

🎯 **Purchase Limit**: {session['max_purchases']} opportunities  
⏰ **Check Interval**: 30 seconds
📅 **Max Item Age**: {session.get('max_item_age_days', 7)} days
🎨 **Target**: First-time creators only

**How Auto Purchase Works:**
• When a first-time creator item is found
• If auto purchase is enabled AND price ≤ max price  
• Bot will attempt to buy it automatically
• You'll get notified of success/failure

*Use the buttons below to modify settings:*"""
        
        await query.edit_message_text(settings_text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def toggle_auto_purchase(self, query):
        """Toggle auto purchase setting"""
        user_id = query.from_user.id
        session = self.get_user_session(user_id)
        
        new_setting = not session['auto_purchase']
        self.update_user_session(user_id, auto_purchase=new_setting)
        
        # Show updated settings menu
        await self.show_settings_menu(query)
    
    async def show_help_inline(self, query):
        """Show help inline"""
        help_text = """🤖 *Rust Skin Auto-Purchase Bot - Help*

**🎯 Main Commands:**
/start - Show main menu and status
/monitor - Start monitoring and auto-purchasing
/stop - Stop monitoring
/status - Check your current status
/purchases - View your purchase history
/settoken - Set your Steam session token
/reset - Reset your purchase counter
/help - Show this help message

**🔧 How Auto-Purchase Works:**
1. I monitor the SCMM API every 30 seconds
2. I look for items from creators with only 1 accepted item
3. I only consider items that are 7 days old or newer
4. If auto-purchase is enabled AND price ≤ your max price
5. I automatically place a buy order on Steam Market
6. You get notified of success/failure immediately
7. I track up to 10 purchases per user

**🧪 Test Mode:**
• Enable test mode to scan without spending money
• **SIMULATES purchases** with fake success/failure results
• Perfect for testing the bot logic before going live
• Shows detailed analysis of what would be purchased
• No Steam token required in test mode

**⚙️ Settings You Can Control:**
• **Auto Purchase**: Enable/disable automatic buying
• **Max Price**: Set maximum price per item ($0.50 - $500)
• **Steam Token**: Your session for making purchases

Need more help? Check the GitHub repository or contact support!"""

        keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(help_text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def start_monitoring_inline(self, query):
        """Start monitoring inline"""
        user_id = query.from_user.id
        session = self.get_user_session(user_id)
        
        # In test mode, we don't need Steam token
        if not session.get('test_mode', False) and not session['steam_session_token']:
            text = "❌ Please set your Steam token first using the 🔑 Set Steam Token button\n\n(Or enable 🧪 Test Mode to scan without purchasing)"
            keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup)
            return
        
        if session['is_monitoring']:
            text = "⚠️ You're already monitoring! Use ⏹️ Stop Monitoring to stop."
            keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup)
            return
        
        if session['purchased_count'] >= session['max_purchases']:
            text = f"🛑 You've already found {session['max_purchases']} opportunities!\n\nUse /reset to reset your counter and start monitoring again."
            keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup)
            return
        
        # Start monitoring
        self.update_user_session(user_id, is_monitoring=True)
        
        # Start monitoring task
        task = asyncio.create_task(self.monitor_user_skins(user_id))
        self.monitoring_tasks[user_id] = task
        
        mode_text = "🧪 TEST MODE" if session.get('test_mode', False) else "💰 LIVE MODE"
        action_text = "scanning and reporting" if session.get('test_mode', False) else "scanning and purchasing"
        
        # Prepare variables to avoid backslashes in f-strings
        result_text = "I'll report what I find without making purchases!" if session.get('test_mode', False) else "I'll send you alerts when I find and purchase opportunities!"
        
        text = f"""🚀 *Monitoring started in {mode_text}!*

I'm now {action_text} first-time creator items.
Progress: {session['purchased_count']}/{session['max_purchases']}

{result_text}
Use ⏹️ Stop Monitoring to stop anytime."""
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def stop_monitoring_inline(self, query):
        """Stop monitoring inline"""
        user_id = query.from_user.id
        session = self.get_user_session(user_id)
        
        if not session['is_monitoring']:
            text = "⚠️ You're not currently monitoring."
            keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(text, reply_markup=reply_markup)
            return
        
        # Stop monitoring
        self.update_user_session(user_id, is_monitoring=False)
        
        # Cancel monitoring task
        if user_id in self.monitoring_tasks:
            self.monitoring_tasks[user_id].cancel()
            del self.monitoring_tasks[user_id]
        
        text = "⏹️ *Monitoring stopped.*\n\nUse ▶️ Start Monitoring to start monitoring again anytime!"
        keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def show_reset_confirmation(self, query):
        """Show reset confirmation"""
        user_id = query.from_user.id
        
        keyboard = [
            [InlineKeyboardButton("✅ Yes, Reset Everything", callback_data=f"reset_confirm_{user_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel")],
            [InlineKeyboardButton("🔙 Back to Settings", callback_data="settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        text = """⚠️ *Reset Your Data*

This will reset:
• Your opportunity counter to 0
• Your processed items list
• Allow you to find 10 more opportunities

Your Steam token and purchase history will be kept.

Are you sure?"""
        
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def set_max_price_prompt(self, query, context):
        """Prompt user to set max price"""
        context.user_data['waiting_for_max_price'] = True
        
        text = """💰 *Set Maximum Purchase Price*

Send me the maximum price you want to spend per item (in USD).

**Examples:**
• `5` = $5.00
• `10.50` = $10.50
• `25` = $25.00

*Send your max price now:*"""
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Settings", callback_data="settings")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages"""
        user_id = update.effective_user.id
        
        if context.user_data.get('waiting_for_token'):
            token = update.message.text.strip()
            
            if len(token) > 10 and len(token) < 200:
                self.update_user_session(user_id, steam_session_token=token)
                context.user_data['waiting_for_token'] = False
                
                await update.message.reply_text(
                    "✅ *Steam token saved successfully!*\n\n"
                    "You can now start monitoring with /monitor\n\n"
                    "🔐 *Your token is stored securely and will only be used for automatic purchases.*",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    "❌ That doesn't look like a valid Steam session token.\n\n"
                    "Please make sure you copied the full 'sessionid' cookie value and try again."
                )
        
        elif context.user_data.get('waiting_for_max_price'):
            try:
                price_str = update.message.text.strip().replace(',', '')
                max_price = float(price_str)
                
                if 0.50 <= max_price <= 500:
                    max_price_cents = int(max_price * 100)
                    self.update_user_session(user_id, max_price_cents=max_price_cents)
                    context.user_data['waiting_for_max_price'] = False
                    
                    await update.message.reply_text(
                        f"✅ *Max price set to ${max_price:.2f}!*\n\n"
                        f"The bot will only auto-purchase items at or below this price.\n\n"
                        f"Use /start to return to the main menu.",
                        parse_mode='Markdown'
                    )
                else:
                    await update.message.reply_text(
                        "❌ Price must be between $0.50 and $500.00\n\n"
                        "Please send a valid price (e.g., 5, 10.50, 25)"
                    )
            except ValueError:
                await update.message.reply_text(
                    "❌ Invalid price format.\n\n"
                    "Please send a number like: 5, 10.50, or 25"
                )
    
    async def monitor_user_skins(self, user_id: int):
        """Main monitoring loop for a specific user"""
        session = self.get_user_session(user_id)
        logger.info(f"Starting skin monitoring for user {user_id}")
        
        try:
            while session['is_monitoring'] and session['purchased_count'] < session['max_purchases']:
                await self.check_new_skins_for_user(user_id)
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info(f"Monitoring cancelled for user {user_id}")
        except Exception as e:
            logger.error(f"Error in monitoring loop for user {user_id}: {e}")
            await self.send_user_message(user_id, f"❌ Monitoring error: {str(e)}\nTry restarting with /monitor")
        finally:
            self.update_user_session(user_id, is_monitoring=False)
            if user_id in self.monitoring_tasks:
                del self.monitoring_tasks[user_id]
        
        if session['purchased_count'] >= session['max_purchases']:
            await self.send_user_message(
                user_id, 
                f"🎉 Found {session['max_purchases']} opportunities! "
                f"Monitoring stopped. Use /reset to find more!"
            )
        
        logger.info(f"Skin monitoring stopped for user {user_id}")
    
    async def check_new_skins_for_user(self, user_id: int):
        """Check for new skins for a specific user"""
        session = self.get_user_session(user_id)
        
        try:
            item_response = requests.get(
                f"{self.api_base}/item", 
                params={
                    'sortBy': 'timeCreated', 
                    'sortByOrder': 'desc',
                    'count': 50
                }, 
                timeout=10
            )
            
            if item_response.status_code == 200:
                item_data = item_response.json()
                items = item_data.get('items', [])
                
                new_items_count = 0
                for item in items:
                    item_id = str(item.get('id', ''))
                    if item_id and item_id not in session['processed_skins']:
                        session['processed_skins'].add(item_id)
                        
                        cursor = self.conn.cursor()
                        cursor.execute('''
                            INSERT OR IGNORE INTO processed_skins (user_id, skin_id) 
                            VALUES (?, ?)
                        ''', (user_id, item_id))
                        self.conn.commit()
                        
                        await self.process_item_for_user(user_id, item)
                        new_items_count += 1
                        
                        if session['purchased_count'] >= session['max_purchases']:
                            break
                
                if new_items_count > 0:
                    logger.info(f"Processed {new_items_count} new items for user {user_id}")
                
        except Exception as e:
            logger.error(f"Error checking new skins for user {user_id}: {e}")
    
    async def process_item_for_user(self, user_id: int, item_data: Dict):
        """Process a single item for a specific user"""
        try:
            session = self.get_user_session(user_id)
            
            creator_id = item_data.get('creatorId')
            creator_name = item_data.get('creatorName', 'Unknown Creator')
            item_name = item_data.get('name', 'Unknown Item')
            item_type = item_data.get('itemType', 'Unknown Type')
            item_collection = item_data.get('itemCollection', 'Unknown Collection')
            is_accepted = item_data.get('isAccepted', False)
            workshop_file_id = item_data.get('workshopFileId')
            
            time_accepted = item_data.get('timeAccepted')
            time_created = item_data.get('timeCreated')
            
            if not is_accepted:
                return
            
            if not self.is_recent_item(time_accepted, time_created, session['max_item_age_days']):
                return
            
            if not creator_id:
                return
            
            if await self.is_first_time_creator(creator_id, creator_name):
                logger.info(f"User {user_id}: Found first-time creator {creator_name} with RECENT item {item_name}")
                await self.record_opportunity_for_user(
                    user_id, item_data, creator_id, creator_name, item_name, 
                    item_type, item_collection, workshop_file_id
                )
                
        except Exception as e:
            logger.error(f"Error processing item for user {user_id}: {e}")
    
    def is_recent_item(self, time_accepted: str, time_created: str, max_age_days: int = 7) -> bool:
        """Check if item was accepted/created within the specified number of days"""
        try:
            time_str = time_accepted or time_created
            
            if not time_str:
                logger.warning("No timestamp found for item - skipping")
                return False
            
            if time_str.endswith('Z'):
                item_time = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            else:
                item_time = datetime.fromisoformat(time_str)
            
            item_time = item_time.replace(tzinfo=None)
            current_time = datetime.utcnow()
            
            age_limit = timedelta(days=max_age_days)
            item_age = current_time - item_time
            
            is_recent = item_age <= age_limit
            
            if not is_recent:
                logger.debug(f"Item too old: {item_age.days} days old (limit: {max_age_days} days)")
            else:
                logger.debug(f"Item is recent: {item_age.days} days old (within {max_age_days} day limit)")
            
            return is_recent
            
        except Exception as e:
            logger.error(f"Error checking item age: {e}")
            return False
    
    async def record_opportunity_for_user(self, user_id: int, item_data: Dict, creator_id: int, 
                                        creator_name: str, item_name: str, item_type: str, 
                                        item_collection: str, workshop_file_id: int):
        """Record a purchase opportunity and attempt automatic purchase (or show test info)"""
        session = self.get_user_session(user_id)
        
        self.add_creator_to_db(str(creator_id), creator_name)
        
        session['purchased_count'] += 1
        self.update_user_session(user_id, purchased_count=session['purchased_count'])
        
        item_id = item_data.get('id')
        market_id = item_data.get('marketId')
        
        if market_id:
            steam_url = f"https://steamcommunity.com/market/listings/252490/{market_id}"
        else:
            steam_name = item_name.replace(' ', '%20').replace('|', '%7C')
            steam_url = f"https://steamcommunity.com/market/listings/252490/{steam_name}"
        
        scmm_url = f"https://rust.scmm.app/item/{item_id}" if item_id else "https://rust.scmm.app"
        workshop_url = item_data.get('workshopFileUrl', '')
        
        market_price = item_data.get('marketSellOrderLowestPrice', 0)
        buy_orders = item_data.get('marketBuyOrderCount', 0)
        sell_orders = item_data.get('marketSellOrderCount', 0)
        
        time_accepted = item_data.get('timeAccepted')
        time_created = item_data.get('timeCreated')
        item_age = self.calculate_item_age(time_accepted, time_created)
        
        # Prepare variables to avoid backslashes in f-strings
        budget_check = '✅ Would purchase (within budget)' if market_price <= session.get('max_price_cents', 1000) else '❌ Would skip (over budget)'
        auto_purchase_check = '✅ Auto-purchase enabled' if session.get('auto_purchase', True) else '❌ Auto-purchase disabled'
        
        would_purchase = 'purchased' if (session.get('auto_purchase', True) and market_price <= session.get('max_price_cents', 1000)) else 'skipped'
        
        if session.get('test_mode', False):
            import random
            
            # Simulate purchase attempt in test mode for testing bot logic
            would_attempt_purchase = (session.get('auto_purchase', True) and 
                                    market_price > 0 and 
                                    market_price <= session.get('max_price_cents', 1000))
            
            if would_attempt_purchase:
                # 70% success rate for fake purchases to simulate realistic conditions
                purchase_success = random.random() < 0.7
                if purchase_success:
                    purchase_details = f"""🧪 **TEST MODE - SIMULATED SUCCESSFUL PURCHASE**

✅ **Fake Purchase Details:**
💰 Price: ${market_price/100:.2f} (simulated payment)
🎯 Status: ✅ Successfully "purchased" (fake)
⚡ Method: Test mode simulation

📊 **Analysis Results:**
✅ Creator has ≤1 accepted items (first-time!)
✅ Item age: {item_age} (within {session.get('max_item_age_days', 7)} day limit)
✅ Price within budget: ${market_price/100:.2f} ≤ ${session.get('max_price_cents', 1000)/100:.2f}
✅ Auto-purchase enabled

🧪 **This WOULD be a real purchase in live mode!**

"""
                else:
                    purchase_details = f"""🧪 **TEST MODE - SIMULATED FAILED PURCHASE**

❌ **Fake Purchase Details:** 
💰 Price: ${market_price/100:.2f} (would have been paid)
🎯 Status: ❌ "Purchase failed" (simulated error)
⚡ Error: Random test failure (item sold out, network error, etc.)

📊 **Analysis Results:**
✅ Creator has ≤1 accepted items (first-time!)
✅ Item age: {item_age} (within {session.get('max_item_age_days', 7)} day limit)
✅ Price within budget: ${market_price/100:.2f} ≤ ${session.get('max_price_cents', 1000)/100:.2f}
✅ Auto-purchase enabled

🧪 **This shows how failed purchases are handled!**

"""
            else:
                purchase_success = False
                purchase_details = f"""🧪 **TEST MODE - WOULD NOT PURCHASE**

📊 **Analysis Results:**
✅ Creator has ≤1 accepted items (first-time!)
✅ Item age: {item_age} (within {session.get('max_item_age_days', 7)} day limit)
💰 Market price: ${market_price/100:.2f} vs your max ${session.get('max_price_cents', 1000)/100:.2f}
{budget_check}
{auto_purchase_check}

🎯 **This item would be SKIPPED in live mode**

"""
            
        else:
            # LIVE MODE - Attempt actual purchase
            purchase_success = False
            purchase_details = ""
            
            if (session['auto_purchase'] and 
                session['steam_session_token'] and 
                market_price > 0 and 
                market_price <= session['max_price_cents']):
                
                try:
                    purchase_result = await self.attempt_steam_purchase(
                        session['steam_session_token'], 
                        item_name, 
                        market_price,
                        item_data
                    )
                    
                    if purchase_result['success']:
                        purchase_success = True
                        purchase_details = f"✅ **PURCHASED SUCCESSFULLY!**\n💰 **Price**: ${purchase_result['price']:.2f}\n"
                    else:
                        purchase_details = f"❌ **Purchase Failed**: {purchase_result['error']}\n"
                        
                except Exception as e:
                    purchase_details = f"❌ **Purchase Error**: {str(e)}\n"
                    logger.error(f"Purchase error for user {user_id}: {e}")
            
            elif session['auto_purchase'] and market_price > session['max_price_cents']:
                purchase_details = f"⚠️ **Price too high**: ${market_price/100:.2f} > ${session['max_price_cents']/100:.2f} (your max)\n"
            
            elif not session['auto_purchase']:
                purchase_details = f"ℹ️ **Auto-purchase disabled** - Manual purchase needed\n"
        
        # Build message
        market_info = ""
        if market_price > 0:
            market_info = f"💰 **Market Price**: ${market_price/100:.2f}\n"
        if buy_orders > 0 or sell_orders > 0:
            market_info += f"📊 **Orders**: {buy_orders} buy, {sell_orders} sell\n"
        
        mode_emoji = "🧪" if session.get('test_mode', False) else ("🎉" if purchase_success else "🎯")
        mode_text = "TEST SCAN" if session.get('test_mode', False) else ("PURCHASED" if purchase_success else "ALERT")
        
        # Prepare final message variables
        test_mode_active = session.get('test_mode', False)
        
        if test_mode_active:
            final_message_suffix = "🧪 *Test mode active - no purchases made*"
        elif purchase_success:
            final_message_suffix = "🎉 *Item purchased automatically! Check your Steam inventory!*"
        else:
            final_message_suffix = "⚡ *New creator detected - Manual purchase may be needed!*"

        message = f"""{mode_emoji} *FIRST-TIME CREATOR {mode_text}!*

🎨 **Item**: {item_name}
👤 **Creator**: {creator_name}
🏷️ **Type**: {item_type}
📦 **Collection**: {item_collection}
📅 **Age**: {item_age}
{market_info}{purchase_details}📈 **Your Progress**: {session['purchased_count']}/{session['max_purchases']}

🔗 **Links**:
[Steam Market]({steam_url})
[SCMM Item Page]({scmm_url})"""

        if workshop_url:
            message += f"\n[Workshop Page]({workshop_url})"

        message += f"\n\n{final_message_suffix}"
        
        await self.send_user_message(user_id, message)
        
        # Record in database
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO purchases 
            (user_id, skin_id, creator_id, creator_name, skin_name, purchase_time, price, success) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id,
            str(item_id),
            str(creator_id),
            creator_name,
            item_name,
            datetime.now(),
            market_price / 100 if market_price else 0,
            purchase_success
        ))
        self.conn.commit()
        
        mode_text = "test scan" if session.get('test_mode', False) else ("purchase" if purchase_success else "opportunity")
        logger.info(f"Recorded {mode_text} for user {user_id}: {item_name} by {creator_name}")
    
    def calculate_item_age(self, time_accepted: str, time_created: str) -> str:
        """Calculate and format item age"""
        try:
            time_str = time_accepted or time_created
            if not time_str:
                return "Unknown age"
            
            if time_str.endswith('Z'):
                item_time = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            else:
                item_time = datetime.fromisoformat(time_str)
            
            item_time = item_time.replace(tzinfo=None)
            current_time = datetime.utcnow()
            age_delta = current_time - item_time
            
            if age_delta.days > 0:
                return f"{age_delta.days} days old"
            elif age_delta.seconds > 3600:
                hours = age_delta.seconds // 3600
                return f"{hours} hours old"
            else:
                minutes = age_delta.seconds // 60
                return f"{minutes} minutes old"
                
        except Exception:
            return "Unknown age"
    
    async def attempt_steam_purchase(self, steam_session_token: str, item_name: str, 
                                   price_cents: int, item_data: Dict) -> Dict:
        """Attempt to purchase item from Steam Community Market using Selenium"""
        try:
            from selenium import webdriver
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.action_chains import ActionChains
            import time
            import random
            
            chrome_options = Options()
            chrome_options.add_argument('--headless')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
            
            driver = webdriver.Chrome(options=chrome_options)
            
            try:
                driver.get('https://steamcommunity.com')
                
                driver.add_cookie({
                    'name': 'sessionid',
                    'value': steam_session_token,
                    'domain': '.steamcommunity.com'
                })
                
                await asyncio.sleep(random.uniform(1.5, 3.0))
                
                market_url = f'https://steamcommunity.com/market/listings/252490/{item_name.replace(" ", "%20")}'
                driver.get(market_url)
                
                wait = WebDriverWait(driver, 15)
                
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/3);")
                await asyncio.sleep(random.uniform(0.8, 1.5))
                
                try:
                    buy_order_button = wait.until(
                        EC.element_to_be_clickable((By.ID, "market_buynow_dialog_purchase"))
                    )
                    
                    actions = ActionChains(driver)
                    actions.move_to_element(buy_order_button)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                    actions.click()
                    actions.perform()
                    
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    
                    confirm_button = wait.until(
                        EC.element_to_be_clickable((By.ID, "market_buynow_dialog_purchase_final"))
                    )
                    
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    
                    actions = ActionChains(driver)
                    actions.move_to_element(confirm_button)
                    actions.click()
                    actions.perform()
                    
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    
                    page_source = driver.page_source.lower()
                    
                    success_indicators = [
                        "market_buynow_dialog_success",
                        "your purchase was successful",
                        "has been added to your inventory"
                    ]
                    
                    for indicator in success_indicators:
                        if indicator.lower() in page_source:
                            return {
                                'success': True,
                                'price': price_cents / 100,
                                'method': 'selenium_purchase'
                            }
                    
                    return {
                        'success': False,
                        'error': 'Purchase attempt completed but result unclear',
                        'method': 'selenium_purchase'
                    }
                    
                except Exception as purchase_error:
                    return {
                        'success': False,
                        'error': f'Purchase process failed: {str(purchase_error)}',
                        'method': 'selenium_purchase'
                    }
            
            finally:
                driver.quit()
                
        except ImportError:
            return {
                'success': False,
                'error': 'Selenium not installed',
                'method': 'selenium_purchase'
            }
        except Exception as e:
            return {
                'success': False,
                'error': f'Selenium purchase error: {str(e)}',
                'method': 'selenium_purchase'
            }
    
    async def is_first_time_creator(self, creator_id: int, creator_name: str) -> bool:
        """Check if this is a creator's first skin using SCMM profile API"""
        creator_id_str = str(creator_id)
        
        if creator_id_str in self.known_creators:
            return False
        
        try:
            response = requests.get(f"{self.api_base}/profile/{creator_id}/summary", timeout=10)
            
            if response.status_code == 200:
                creator_items_response = requests.get(
                    f"{self.api_base}/item", 
                    params={
                        'creatorId': creator_id,
                        'count': 100
                    }, 
                    timeout=10
                )
                
                if creator_items_response.status_code == 200:
                    creator_items = creator_items_response.json()
                    total_items = creator_items.get('total', 0)
                    
                    if total_items > 1:
                        self.add_creator_to_db(creator_id_str, creator_name, total_items)
                        logger.info(f"Creator {creator_name} has {total_items} items - not first-time")
                        return False
                    
                    logger.info(f"Creator {creator_name} has {total_items} items - potentially first-time")
                    return total_items <= 1
                
                logger.info(f"Could not verify item count for {creator_name} - assuming first-time")
                return True
            
            elif response.status_code == 404:
                logger.info(f"Profile not found for {creator_name} (ID: {creator_id}) - likely first-time creator")
                return True
            
            else:
                logger.warning(f"Error {response.status_code} checking profile for {creator_name}")
                return True
            
        except Exception as e:
            logger.error(f"Error checking creator profile for {creator_id}: {e}")
            return True
    
    def add_creator_to_db(self, creator_id: str, creator_name: str, skin_count: int = 1):
        """Add creator to global database"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO creators 
            (creator_id, creator_name, first_seen, skin_count) 
            VALUES (?, ?, ?, ?)
        ''', (creator_id, creator_name, datetime.now(), skin_count))
        self.conn.commit()
        self.known_creators.add(creator_id)
    
    async def send_user_message(self, user_id: int, message: str):
        """Send message to a specific user"""
        try:
            await self.application.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error sending message to user {user_id}: {e}")
    
    def run(self):
        """Start the bot with conflict handling"""
        logger.info("Starting Multi-User Rust Skin Telegram Bot...")
        
        async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            """Log errors and handle conflicts"""
            logger.error(f"Exception while handling an update: {context.error}")
            
            if "Conflict" in str(context.error):
                logger.warning("Bot conflict detected, continuing...")
                return
        
        self.application.add_error_handler(error_handler)
        
        try:
            self.application.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES
            )
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            if "Conflict" in str(e):
                logger.error("Another bot instance is running. Please stop other instances first.")
            raise
    
    async def toggle_test_mode(self, query):
        """Toggle test mode"""
        user_id = query.from_user.id
        session = self.get_user_session(user_id)
        
        new_test_mode = not session.get('test_mode', False)
        self.update_user_session(user_id, test_mode=new_test_mode)
        
        if new_test_mode:
            text = """🧪 *Test Mode Enabled!*

**What Test Mode Does:**
• Scans SCMM for first-time creator items (expanded to 7 days!)
• Shows you detailed info about what it finds
• Reports item age, creator details, prices
• **SIMULATES purchases** with fake success/failure results
• Perfect for testing the bot logic without spending money

**You'll see reports like:**
✅ Found first-time creator: "ArtistName"
📅 Item age: 2 days old (within 7 day limit)
💰 Price: $5.50 (within your $10 budget)
🧪 **SIMULATED SUCCESSFUL PURCHASE** (fake)
🎯 This WOULD be a real purchase in live mode

**Use ▶️ Start Monitoring to begin test scanning!**"""
        else:
            text = """💰 *Live Mode Enabled!*

**What Live Mode Does:**
• Scans SCMM for first-time creator items  
• **ACTUALLY PURCHASES** qualifying items
• Requires Steam session token
• Uses Selenium for human-like purchasing

**Make sure you:**
✅ Set your Steam session token
✅ Fund your Steam wallet
✅ Configure your max price

**Ready for real purchases!**"""
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def show_main_menu_inline(self, query):
        """Show main menu inline"""
        user_id = query.from_user.id
        username = query.from_user.username or query.from_user.first_name
        session = self.get_user_session(user_id, username)
        
        keyboard = [
            [InlineKeyboardButton("📊 My Status", callback_data="status"),
             InlineKeyboardButton("🛍️ My Purchases", callback_data="purchases")],
            [InlineKeyboardButton("🔑 Set Steam Token", callback_data="settoken"),
             InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("▶️ Start Monitoring", callback_data="startbot"),
             InlineKeyboardButton("⏹️ Stop Monitoring", callback_data="stopbot")],
            [InlineKeyboardButton("🧪 Test Mode", callback_data="test_mode"),
             InlineKeyboardButton("❓ Help", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_emoji = "🟢" if session['is_monitoring'] else "🔴"
        token_emoji = "✅" if session['steam_session_token'] else "❌"
        test_emoji = "🧪" if session.get('test_mode', False) else "💰"
        
        # Prepare text parts to avoid backslashes in f-strings
        monitoring_status = 'Active' if session['is_monitoring'] else 'Stopped'
        token_status = 'Configured' if session['steam_session_token'] else 'Not Set'
        auto_purchase_status = '✅ Enabled' if session.get('auto_purchase', True) else '❌ Disabled'
        mode_status = '🧪 Test Mode (No Purchases)' if session.get('test_mode', False) else '💰 Live Mode'
        max_price = session.get('max_price_cents', 1000) / 100
        
        test_mode = session.get('test_mode', False)
        action_description = 'Show you opportunities without purchasing (TEST MODE)' if test_mode else 'Automatically purchase items within your price limit'
        notification_type = 'findings' if test_mode else 'purchases/opportunities'
        
        quick_start_1 = 'Enable 🧪 Test Mode to scan without purchasing' if not test_mode else "You're in test mode - perfect for testing!"
        quick_start_3 = "I'll show you what I find without buying anything!" if test_mode else 'Set your Steam token and configure auto-purchase'
        
        welcome_text = f"""🤖 *Welcome to Rust Skin Auto-Purchase Bot!*

👋 Hello {username}! I find AND buy new skins from first-time creators automatically!

📊 **Your Status:**
{status_emoji} **Monitoring**: {monitoring_status}
{token_emoji} **Steam Token**: {token_status}
🤖 **Auto Purchase**: {auto_purchase_status}
{test_emoji} **Mode**: {mode_status}
💰 **Max Price**: ${max_price:.2f}
🎯 **Progress**: {session['purchased_count']}/{session['max_purchases']} items

🎨 **What I Do:**
• Monitor SCMM for new items from first-time creators
• Only consider items that are 7 days old or newer
• {action_description}
• Send instant notifications of {notification_type}
• Track progress and stop after 10 successful actions

**🚀 Quick Start:**
1️⃣ {quick_start_1}
2️⃣ Start monitoring with ▶️ Start Monitoring
3️⃣ {quick_start_3}

Use the buttons below or type /help for more info."""

        await query.edit_message_text(welcome_text, parse_mode='Markdown', reply_markup=reply_markup)

if __name__ == "__main__":
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable not set!")
        print("Get your token from @BotFather on Telegram and set it in Railway dashboard.")
        exit(1)
    
    bot = RustSkinTelegramBot()
    bot.run()
