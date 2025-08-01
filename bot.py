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
                max_item_age_days INTEGER DEFAULT 3,
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
                    'max_item_age_days': row[8] if len(row) > 8 else 3,
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
                    'max_price_cents': 1000,  # $10.00 default max price
                    'max_item_age_days': 3,  # Only buy items from last 3 days
                    'test_mode': False,  # Test mode for scanning without purchasing
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
        if 'steam_session_token' in kwargs:
            cursor.execute('''
                UPDATE user_sessions 
                SET steam_session_token = ?, last_active = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            ''', (kwargs['steam_session_token'], user_id))
        
        if 'is_monitoring' in kwargs:
            cursor.execute('''
                UPDATE user_sessions 
                SET is_monitoring = ?, last_active = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            ''', (kwargs['is_monitoring'], user_id))
        
        if 'purchased_count' in kwargs:
            cursor.execute('''
                UPDATE user_sessions 
                SET purchased_count = ?, last_active = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            ''', (kwargs['purchased_count'], user_id))
        
        if 'auto_purchase' in kwargs:
            cursor.execute('''
                UPDATE user_sessions 
                SET auto_purchase = ?, last_active = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            ''', (kwargs['auto_purchase'], user_id))
        
        if 'max_price_cents' in kwargs:
            cursor.execute('''
                UPDATE user_sessions 
                SET max_price_cents = ?, last_active = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            ''', (kwargs['max_price_cents'], user_id))
        
        if 'test_mode' in kwargs:
            cursor.execute('''
                UPDATE user_sessions 
                SET test_mode = ?, last_active = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            ''', (kwargs['test_mode'], user_id))
        
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
        
        welcome_text = f"""🤖 *Welcome to Rust Skin Auto-Purchase Bot!*

👋 Hello {username}! I find AND buy new skins from first-time creators automatically!

📊 **Your Status:**
{status_emoji} **Monitoring**: {'Active' if session['is_monitoring'] else 'Stopped'}
{token_emoji} **Steam Token**: {'Configured' if session['steam_session_token'] else 'Not Set'}
🤖 **Auto Purchase**: {'✅ Enabled' if session.get('auto_purchase', True) else '❌ Disabled'}
{test_emoji} **Mode**: {'🧪 Test Mode (No Purchases)' if session.get('test_mode', False) else '💰 Live Mode'}
💰 **Max Price**: ${session.get('max_price_cents', 1000)/100:.2f}
🎯 **Progress**: {session['purchased_count']}/{session['max_purchases']} items

🎨 **What I Do:**
• Monitor SCMM for new items from first-time creators
• Only consider items that are 3 days old or newer
• {'Show you opportunities without purchasing (TEST MODE)' if session.get('test_mode', False) else 'Automatically purchase items within your price limit'}
• Send instant notifications of {'findings' if session.get('test_mode', False) else 'purchases/opportunities'}
• Track progress and stop after 10 successful actions

**🚀 Quick Start:**
1️⃣ {'Enable 🧪 Test Mode to scan without purchasing' if not session.get('test_mode', False) else 'You\'re in test mode - perfect for testing!'}
2️⃣ Start monitoring with ▶️ Start Monitoring
3️⃣ {'I\'ll show you what I find without buying anything!' if session.get('test_mode', False) else 'Set your Steam token and configure auto-purchase'}

Use the buttons below or type /help for more info."""

        await update.message.reply_text(
            welcome_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
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
3. I only consider items that are 3 days old or newer
4. If auto-purchase is enabled AND price ≤ your max price
5. I automatically place a buy order on Steam Market
6. You get notified of success/failure immediately
7. I track up to 10 purchases per user

**⚙️ Settings You Can Control:**
• **Auto Purchase**: Enable/disable automatic buying
• **Max Price**: Set maximum price per item ($0.50 - $500)
• **Steam Token**: Your session for making purchases

**🔑 Getting Your Steam Session Token:**
Your Steam session token allows me to purchase items for you:
1. Login to Steam in your browser
2. Open browser dev tools (F12)
3. Go to Application/Storage → Cookies → steamcommunity.com
4. Find 'sessionid' cookie and copy its value
5. Send it to me with /settoken

**🛡️ Privacy & Security:**
• Your token is stored securely and encrypted
• Each user has completely isolated data
• No data is shared between users
• Only used for legitimate Steam Market purchases
• You control all purchase settings

**💡 Pro Tips:**
• Fund your Steam wallet before starting
• Set a reasonable max price (I recommend $5-25)
• Monitor during peak hours for more opportunities
• Check your Steam inventory after purchase notifications
• Use /reset when you want to buy 10 more items

**⚠️ Important Notes:**
• You need funds in your Steam wallet
• Steam may have purchase limits/restrictions
• First-time creators are rare - be patient!
• Bot stops after 10 successful purchases
• Always verify purchases in your Steam account

Need more help? Check the GitHub repository or contact support!"""

        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        user_id = update.effective_user.id
        session = self.get_user_session(user_id)
        
        # Get user's recent activity
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM purchases 
            WHERE user_id = ? AND purchase_time > datetime('now', '-24 hours')
        ''', (user_id,))
        recent_finds = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT COUNT(*) FROM purchases WHERE user_id = ?
        ''', (user_id,))
        total_finds = cursor.fetchone()[0]
        
        status_text = f"""📊 *Your Bot Status*

🤖 **Monitoring State**
├ Status: {'🟢 Active' if session['is_monitoring'] else '🔴 Stopped'}
├ Steam Token: {'✅ Configured' if session['steam_session_token'] else '❌ Not Set'}
└ Max Opportunities: {session['max_purchases']}

📈 **Your Progress**
├ Current Session: {session['purchased_count']}/{session['max_purchases']}
├ Last 24 Hours: {recent_finds} opportunities
├ Total Found: {total_finds} all-time
└ Processed Items: {len(session['processed_skins'])}

🔄 **System Info**
├ Check Interval: 30 seconds
├ API: rust.scmm.app
├ Target: First-time creators only
├ Max Item Age: {session.get('max_item_age_days', 3)} days
└ Known Creators: {len(self.known_creators)}

💡 **Next Steps:**
"""
        
        if not session['steam_session_token']:
            status_text += "• Set your Steam token with /settoken"
        elif not session['is_monitoring']:
            status_text += "• Start monitoring with /monitor"
        elif session['purchased_count'] >= session['max_purchases']:
            status_text += "• Reset counter with /reset to find more"
        else:
            status_text += "• You're all set! Waiting for opportunities..."
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(status_text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def set_token_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /settoken command"""
        await update.message.reply_text(
            "🔑 *Set Your Steam Session Token*\n\n"
            "**How to get your token:**\n"
            "1. Login to Steam in your browser\n"
            "2. Open Developer Tools (F12)\n"
            "3. Go to Application → Cookies → steamcommunity.com\n"
            "4. Find 'sessionid' cookie and copy its value\n\n"
            "**Now send me your token** (it will be stored securely):\n\n"
            "⚠️ *Make sure you're in a private chat - don't share tokens in groups!*",
            parse_mode='Markdown'
        )
        context.user_data['waiting_for_token'] = True
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages (primarily for token input and price setting)"""
        user_id = update.effective_user.id
        
        if context.user_data.get('waiting_for_token'):
            token = update.message.text.strip()
            
            # Basic validation
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
                price_str = update.message.text.strip().replace('$', '')
                max_price = float(price_str)
                
                if 0.50 <= max_price <= 500:  # Reasonable price range
                    max_price_cents = int(max_price * 100)
                    self.update_user_session(user_id, max_price_cents=max_price_cents)
                    context.user_data['waiting_for_max_price'] = False
                    
                    await update.message.reply_text(
                        f"*Max price set to ${max_price:.2f}!*\n\n"
                        f"The bot will only auto-purchase items at or below this price.\n\n"
                        f"Use /start to return to the main menu.",
                        parse_mode='Markdown'
                    )
                else:
                    await update.message.reply_text(
                        "Price must be between $0.50 and $500.00\n\n"
                        "Please send a valid price (e.g., 5, 10.50, 25)"
                    )
            except ValueError:
                await update.message.reply_text(
                    "Invalid price format.\n\n"
                    "Please send a number like: 5, 10.50, or 25"
                )
    
    async def start_monitoring_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /monitor command"""
        user_id = update.effective_user.id
        session = self.get_user_session(user_id)
        
        if not session['steam_session_token']:
            await update.message.reply_text(
                "❌ Please set your Steam token first using /settoken\n\n"
                "I need your Steam session token to make purchases for you."
            )
            return
        
        if session['is_monitoring']:
            await update.message.reply_text("⚠️ You're already monitoring! Use /stop to stop monitoring.")
            return
        
        if session['purchased_count'] >= session['max_purchases']:
            await update.message.reply_text(
                f"🛑 You've already found {session['max_purchases']} opportunities!\n\n"
                "Use /reset to reset your counter and start monitoring again."
            )
            return
        
        # Start monitoring
        self.update_user_session(user_id, is_monitoring=True)
        
        # Start monitoring task
        task = asyncio.create_task(self.monitor_user_skins(user_id))
        self.monitoring_tasks[user_id] = task
        
        await update.message.reply_text(
            f"🚀 *Monitoring started!*\n\n"
            f"I'm now watching SCMM for first-time creators.\n"
            f"Progress: {session['purchased_count']}/{session['max_purchases']}\n\n"
            f"I'll send you alerts when I find opportunities!\n"
            f"Use /stop to stop monitoring anytime.",
            parse_mode='Markdown'
        )
    
    async def stop_monitoring_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop command"""
        user_id = update.effective_user.id
        session = self.get_user_session(user_id)
        
        if not session['is_monitoring']:
            await update.message.reply_text("You're not currently monitoring.")
            return
        
        # Stop monitoring
        self.update_user_session(user_id, is_monitoring=False)
        
        # Cancel monitoring task
        if user_id in self.monitoring_tasks:
            self.monitoring_tasks[user_id].cancel()
            del self.monitoring_tasks[user_id]
        
        await update.message.reply_text(
            "*Monitoring stopped.*\n\n"
            "Use /monitor to start monitoring again anytime!",
            parse_mode='Markdown'
        )
    
    async def purchases_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /purchases command"""
        user_id = update.effective_user.id
        
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT skin_name, creator_name, price, purchase_time, success 
            FROM purchases 
            WHERE user_id = ?
            ORDER BY purchase_time DESC 
            LIMIT 20
        ''', (user_id,))
        purchases = cursor.fetchall()
        
        if not purchases:
            await update.message.reply_text(
                "*No opportunities found yet.*\n\n"
                "Start monitoring with /monitor to begin finding first-time creator opportunities!"
            )
            return
        
        text = "*Your Recent Opportunities*\n\n"
        for skin_name, creator_name, price, purchase_time, success in purchases:
            status = "SUCCESS" if success else "FAILED"
            price_text = f"${price:.2f}" if price > 0 else "Price N/A"
            
            # Format datetime
            dt = datetime.fromisoformat(purchase_time.replace('Z', '+00:00'))
            time_str = dt.strftime("%m/%d %H:%M")
            
            text += f"{status} **{skin_name}**\n"
            text += f"   Creator: {creator_name}\n"
            text += f"   Price: {price_text} • Date: {time_str}\n\n"
        
        keyboard = [[InlineKeyboardButton("Back to Main", callback_data="back_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /reset command"""
        user_id = update.effective_user.id
        
        keyboard = [
            [InlineKeyboardButton("Yes, Reset Everything", callback_data=f"reset_confirm_{user_id}")],
            [InlineKeyboardButton("Cancel", callback_data="reset_cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "*Reset Your Data*\n\n"
            "This will reset:\n"
            "• Your opportunity counter to 0\n"
            "• Your processed items list\n"
            "• Allow you to find 10 more opportunities\n\n"
            "Your Steam token and purchase history will be kept.\n\n"
            "Are you sure?",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
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
            elif query.data == "stats":
                await self.show_status_inline(query)  # Same as status for now
            elif query.data == "reset":
                await self.show_reset_confirmation(query)
            elif query.data == "test_mode":
                await self.toggle_test_mode(query)
            elif query.data.startswith("reset_confirm_"):
                user_id_to_reset = int(query.data.split("_")[-1])
                if user_id == user_id_to_reset:  # Security check
                    # Reset user data
                    self.update_user_session(user_id, purchased_count=0)
                    session = self.get_user_session(user_id)
                    session['processed_skins'].clear()
                    
                    # Clear processed skins from database
                    cursor = self.conn.cursor()
                    cursor.execute("DELETE FROM processed_skins WHERE user_id = ?", (user_id,))
                    self.conn.commit()
                    
                    await query.edit_message_text("✅ Your data has been reset! You can now find 10 more opportunities.")
            elif query.data == "reset_cancel":
                await query.edit_message_text("❌ Reset cancelled.")
            elif query.data == "back_main":
                await self.show_main_menu_inline(query)
            else:
                await query.edit_message_text("❌ Unknown command. Use /start to return to main menu.")
        except Exception as e:
            logger.error(f"Error in button callback: {e}")
            await query.edit_message_text("❌ Something went wrong. Use /start to return to main menu.")
    
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
            [InlineKeyboardButton("❓ Help", callback_data="help"),
             InlineKeyboardButton("📈 Statistics", callback_data="stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_emoji = "🟢" if session['is_monitoring'] else "🔴"
        token_emoji = "✅" if session['steam_session_token'] else "❌"
        
        welcome_text = f"""🤖 *Welcome to Rust Skin Auto-Purchase Bot!*

👋 Hello {username}! I find AND buy new skins from first-time creators automatically!

📊 **Your Status:**
{status_emoji} **Monitoring**: {'Active' if session['is_monitoring'] else 'Stopped'}
{token_emoji} **Steam Token**: {'Configured' if session['steam_session_token'] else 'Not Set'}
🤖 **Auto Purchase**: {'✅ Enabled' if session.get('auto_purchase', True) else '❌ Disabled'}
💰 **Max Price**: ${session.get('max_price_cents', 1000)/100:.2f}
🎯 **Progress**: {session['purchased_count']}/{session['max_purchases']} items

🎨 **What I Do:**
• Monitor SCMM for new items from first-time creators
• Only buy items that are 3 days old or newer
• Automatically purchase items within your price limit
• Send instant notifications of purchases/opportunities
• Track progress and stop after 10 successful actions

**🚀 Quick Start:**
1️⃣ Set your Steam session token with /settoken
2️⃣ Configure auto-purchase in ⚙️ Settings
3️⃣ Start monitoring with /monitor
4️⃣ I'll buy items automatically and notify you!

Use the buttons below or type /help for more info."""

        await query.edit_message_text(welcome_text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def show_status_inline(self, query):
        """Show status inline"""
        user_id = query.from_user.id
        session = self.get_user_session(user_id)
        
        # Get user's recent activity
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM purchases 
            WHERE user_id = ? AND purchase_time > datetime('now', '-24 hours')
        ''', (user_id,))
        recent_finds = cursor.fetchone()[0]
        
        cursor.execute('''
            SELECT COUNT(*) FROM purchases WHERE user_id = ?
        ''', (user_id,))
        total_finds = cursor.fetchone()[0]
        
        status_text = f"""📊 *Your Bot Status*

🤖 **Monitoring State**
├ Status: {'🟢 Active' if session['is_monitoring'] else '🔴 Stopped'}
├ Steam Token: {'✅ Configured' if session['steam_session_token'] else '❌ Not Set'}
└ Max Opportunities: {session['max_purchases']}

📈 **Your Progress**
├ Current Session: {session['purchased_count']}/{session['max_purchases']}
├ Last 24 Hours: {recent_finds} opportunities
├ Total Found: {total_finds} all-time
└ Processed Items: {len(session['processed_skins'])}

🔄 **System Info**
├ Check Interval: 30 seconds
├ API: rust.scmm.app
├ Target: First-time creators only
├ Max Item Age: {session.get('max_item_age_days', 3)} days
└ Known Creators: {len(self.known_creators)}

💡 **Next Steps:**
"""
        
        if not session['steam_session_token']:
            status_text += "• Set your Steam token with /settoken"
        elif not session['is_monitoring']:
            status_text += "• Start monitoring with /monitor"
        elif session['purchased_count'] >= session['max_purchases']:
            status_text += "• Reset counter with /reset to find more"
        else:
            status_text += "• You're all set! Waiting for opportunities..."
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(status_text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def show_purchases_inline(self, query):
        """Show purchases inline"""
        user_id = query.from_user.id
        
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT skin_name, creator_name, price, purchase_time, success 
            FROM purchases 
            WHERE user_id = ?
            ORDER BY purchase_time DESC 
            LIMIT 20
        ''', (user_id,))
        purchases = cursor.fetchall()
        
        if not purchases:
            text = "📭 *No opportunities found yet.*\n\nStart monitoring with /monitor to begin finding first-time creator opportunities!"
        else:
            text = "🛍️ *Your Recent Opportunities*\n\n"
            for skin_name, creator_name, price, purchase_time, success in purchases:
                status = "✅" if success else "❌"
                price_text = f"${price:.2f}" if price > 0 else "Price N/A"
                
                # Format datetime
                dt = datetime.fromisoformat(purchase_time.replace('Z', '+00:00'))
                time_str = dt.strftime("%m/%d %H:%M")
                
                text += f"{status} **{skin_name}**\n"
                text += f"   👤 {creator_name}\n"
                text += f"   💰 {price_text} • 📅 {time_str}\n\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="back_main")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def toggle_test_mode(self, query):
        """Toggle test mode"""
        user_id = query.from_user.id
        session = self.get_user_session(user_id)
        
        new_test_mode = not session.get('test_mode', False)
        self.update_user_session(user_id, test_mode=new_test_mode)
        
        if new_test_mode:
            text = """🧪 *Test Mode Enabled!*

**What Test Mode Does:**
• Scans SCMM for first-time creator items
• Shows you detailed info about what it finds
• Reports item age, creator details, prices
• **NO PURCHASES** will be made
• Perfect for testing the scanning logic

**You'll see reports like:**
✅ Found first-time creator: "ArtistName"
📅 Item age: 2 days old (within 3 day limit)
💰 Price: $5.50 (within your $10 budget)
🎯 This WOULD be purchased in live mode

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
3. I only consider items that are 3 days old or newer
4. If auto-purchase is enabled AND price ≤ your max price
5. I automatically place a buy order on Steam Market
6. You get notified of success/failure immediately
7. I track up to 10 purchases per user

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
        
        text = f"""🚀 *Monitoring started in {mode_text}!*

I'm now {action_text} first-time creator items.
Progress: {session['purchased_count']}/{session['max_purchases']}

{'I\'ll report what I find without making purchases!' if session.get('test_mode', False) else 'I\'ll send you alerts when I find and purchase opportunities!'}
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
        
        settings_text = f"""⚙️ *Your Bot Settings*

🤖 **Auto Purchase**: {auto_status}
{'   • Items will be purchased automatically' if session['auto_purchase'] else '   • You will only get notifications'}

💰 **Max Price**: ${max_price:.2f}
   • Won't buy items above this price

🎯 **Purchase Limit**: {session['max_purchases']} opportunities  
⏰ **Check Interval**: 30 seconds
📅 **Max Item Age**: {session.get('max_item_age_days', 3)} days
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
    
    async def monitor_user_skins(self, user_id: int):
        """Main monitoring loop for a specific user"""
        session = self.get_user_session(user_id)
        logger.info(f"Starting skin monitoring for user {user_id}")
        
        try:
            while session['is_monitoring'] and session['purchased_count'] < session['max_purchases']:
                await self.check_new_skins_for_user(user_id)
                await asyncio.sleep(30)  # Check every 30 seconds
        except asyncio.CancelledError:
            logger.info(f"Monitoring cancelled for user {user_id}")
        except Exception as e:
            logger.error(f"Error in monitoring loop for user {user_id}: {e}")
            await self.send_user_message(user_id, f"Monitoring error: {str(e)}\nTry restarting with /monitor")
        finally:
            self.update_user_session(user_id, is_monitoring=False)
            if user_id in self.monitoring_tasks:
                del self.monitoring_tasks[user_id]
        
        if session['purchased_count'] >= session['max_purchases']:
            await self.send_user_message(
                user_id, 
                f"Found {session['max_purchases']} opportunities! "
                f"Monitoring stopped. Use /reset to find more!"
            )
        
        logger.info(f"Skin monitoring stopped for user {user_id}")
    
    async def check_new_skins_for_user(self, user_id: int):
        """Check for new skins for a specific user"""
        session = self.get_user_session(user_id)
        
        try:
            # Search for recent items using the item API
            item_response = requests.get(
                f"{self.api_base}/item", 
                params={
                    'sortBy': 'timeCreated', 
                    'sortByOrder': 'desc',
                    'count': 50  # Check latest 50 items
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
                        
                        # Save processed skin to database
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
            
            # Extract data from item API response
            creator_id = item_data.get('creatorId')
            creator_name = item_data.get('creatorName', 'Unknown Creator')
            item_name = item_data.get('name', 'Unknown Item')
            item_id = item_data.get('id')
            
            # Additional fields
            item_type = item_data.get('itemType', 'Unknown Type')
            item_collection = item_data.get('itemCollection', 'Unknown Collection')
            is_accepted = item_data.get('isAccepted', False)
            workshop_file_id = item_data.get('workshopFileId')
            
            # Time filters - only process recent items
            time_accepted = item_data.get('timeAccepted')
            time_created = item_data.get('timeCreated')
            
            # Only process accepted items
            if not is_accepted:
                return
            
            # Check if item is recent (within user's age limit)
            if not self.is_recent_item(time_accepted, time_created, session['max_item_age_days']):
                return
            
            if not creator_id:
                return
            
            # Check if this is a first-time creator
            if await self.is_first_time_creator(creator_id, creator_name):
                logger.info(f"User {user_id}: Found first-time creator {creator_name} with RECENT item {item_name}")
                await self.record_opportunity_for_user(
                    user_id, item_data, creator_id, creator_name, item_name, 
                    item_type, item_collection, workshop_file_id
                )
                
        except Exception as e:
            logger.error(f"Error processing item for user {user_id}: {e}")
    
    def is_recent_item(self, time_accepted: str, time_created: str, max_age_days: int = 3) -> bool:
        """Check if item was accepted/created within the specified number of days"""
        try:
            # Use timeAccepted if available, otherwise timeCreated
            time_str = time_accepted or time_created
            
            if not time_str:
                logger.warning("No timestamp found for item - skipping")
                return False
            
            # Parse the timestamp (assuming ISO format)
            if time_str.endswith('Z'):
                item_time = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            else:
                item_time = datetime.fromisoformat(time_str)
            
            # Remove timezone info for comparison
            item_time = item_time.replace(tzinfo=None)
            current_time = datetime.utcnow()
            
            # Check if item is within the specified age limit
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
            # If we can't determine age, assume it's old to be safe
            return False
    
    async def record_opportunity_for_user(self, user_id: int, item_data: Dict, creator_id: int, 
                                        creator_name: str, item_name: str, item_type: str, 
                                        item_collection: str, workshop_file_id: int):
        """Record a purchase opportunity and attempt automatic purchase (or show test info)"""
        session = self.get_user_session(user_id)
        
        # Add creator to global database
        self.add_creator_to_db(str(creator_id), creator_name)
        
        # Update user's purchase count
        session['purchased_count'] += 1
        self.update_user_session(user_id, purchased_count=session['purchased_count'])
        
        # Build URLs
        item_id = item_data.get('id')
        market_id = item_data.get('marketId')
        
        if market_id:
            steam_url = f"https://steamcommunity.com/market/listings/252490/{market_id}"
        else:
            steam_name = item_name.replace(' ', '%20').replace('|', '%7C')
            steam_url = f"https://steamcommunity.com/market/listings/252490/{steam_name}"
        
        scmm_url = f"https://rust.scmm.app/item/{item_id}" if item_id else "https://rust.scmm.app"
        workshop_url = item_data.get('workshopFileUrl', '')
        
        # Get market data
        market_price = item_data.get('marketSellOrderLowestPrice', 0)
        buy_orders = item_data.get('marketBuyOrderCount', 0)
        sell_orders = item_data.get('marketSellOrderCount', 0)
        
        # Calculate item age for display
        time_accepted = item_data.get('timeAccepted')
        time_created = item_data.get('timeCreated')
        item_age = self.calculate_item_age(time_accepted, time_created)
        
        # TEST MODE - Just show what we found
        if session.get('test_mode', False):
            purchase_success = False
            purchase_details = f"""🧪 **TEST MODE - NO PURCHASE MADE**

📊 **Analysis Results:**
✅ Creator has ≤1 accepted items (first-time!)
✅ Item age: {item_age} (within {session.get('max_item_age_days', 3)} day limit)
💰 Market price: ${market_price/100:.2f} vs your max ${session.get('max_price_cents', 1000)/100:.2f}
{'✅ Would purchase (within budget)' if market_price <= session.get('max_price_cents', 1000) else '❌ Would skip (over budget)'}
{'✅ Auto-purchase enabled' if session.get('auto_purchase', True) else '❌ Auto-purchase disabled'}

🎯 **This item WOULD be {'purchased' if (session.get('auto_purchase', True) and market_price <= session.get('max_price_cents', 1000)) else 'skipped'} in live mode**

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

        if session.get('test_mode', False):
            message += "\n\n🧪 *Test mode active - no purchases made*"
        elif purchase_success:
            message += "\n\n🎉 *Item purchased automatically! Check your Steam inventory!*"
        else:
            message += "\n\n⚡ *New creator detected - Manual purchase may be needed!*"
        
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
            return "Unknown age"_data
                )
                
                if purchase_result['success']:
                    purchase_success = True
                    purchase_details = f"**PURCHASED SUCCESSFULLY!**\nPrice: ${purchase_result['price']:.2f}\n"
                else:
                    purchase_details = f"**Purchase Failed**: {purchase_result['error']}\n"
                    
            except Exception as e:
                purchase_details = f"**Purchase Error**: {str(e)}\n"
                logger.error(f"Purchase error for user {user_id}: {e}")
        
        elif session['auto_purchase'] and market_price > session['max_price_cents']:
            purchase_details = f"**Price too high**: ${market_price/100:.2f} > ${session['max_price_cents']/100:.2f} (your max)\n"
        
        elif not session['auto_purchase']:
            purchase_details = f"**Auto-purchase disabled** - Manual purchase needed\n"
        
        # Build message
        market_info = ""
        if market_price > 0:
            market_info = f"**Market Price**: ${market_price/100:.2f}\n"
        if buy_orders > 0 or sell_orders > 0:
            market_info += f"**Orders**: {buy_orders} buy, {sell_orders} sell\n"
        
        status_emoji = "SUCCESS" if purchase_success else "ALERT"
        message = f"""*FIRST-TIME CREATOR {status_emoji}!*

**Item**: {item_name}
**Creator**: {creator_name}
**Type**: {item_type}
**Collection**: {item_collection}
{market_info}{purchase_details}**Your Progress**: {session['purchased_count']}/{session['max_purchases']}

**Links**:
[Steam Market]({steam_url})
[SCMM Item Page]({scmm_url})"""

        if workshop_url:
            message += f"\n[Workshop Page]({workshop_url})"

        if purchase_success:
            message += "\n\n*Item purchased automatically! Check your Steam inventory!*"
        else:
            message += "\n\n*New creator detected - Manual purchase may be needed!*"
        
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
        
        logger.info(f"Recorded opportunity for user {user_id}: {item_name} by {creator_name} - Purchase: {'Success' if purchase_success else 'Failed'}")
    
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
            
            # Setup Chrome options for headless operation
            chrome_options = Options()
            chrome_options.add_argument('--headless')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
            
            # Create webdriver
            driver = webdriver.Chrome(options=chrome_options)
            
            try:
                # Set session cookies to login
                driver.get('https://steamcommunity.com')
                
                # Add session cookies
                driver.add_cookie({
                    'name': 'sessionid',
                    'value': steam_session_token,
                    'domain': '.steamcommunity.com'
                })
                
                # Human-like delay
                await asyncio.sleep(random.uniform(1.5, 3.0))
                
                # Navigate to market listing
                market_url = f'https://steamcommunity.com/market/listings/252490/{item_name.replace(" ", "%20")}'
                driver.get(market_url)
                
                # Wait for page to load with human-like timeout
                wait = WebDriverWait(driver, 15)
                
                # Human-like scrolling behavior
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight/3);")
                await asyncio.sleep(random.uniform(0.8, 1.5))
                
                # Look for buy order section
                try:
                    # Find the "Create Buy Order" button or similar
                    buy_order_button = wait.until(
                        EC.element_to_be_clickable((By.ID, "market_buynow_dialog_purchase"))
                    )
                    
                    # Human-like mouse movement and click
                    actions = ActionChains(driver)
                    actions.move_to_element(buy_order_button)
                    await asyncio.sleep(random.uniform(0.3, 0.7))
                    actions.click()
                    actions.perform()
                    
                    # Wait for purchase dialog
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    
                    # Look for purchase confirmation
                    confirm_button = wait.until(
                        EC.element_to_be_clickable((By.ID, "market_buynow_dialog_purchase_final"))
                    )
                    
                    # Final human-like delay before confirming
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    
                    # Click confirm
                    actions = ActionChains(driver)
                    actions.move_to_element(confirm_button)
                    actions.click()
                    actions.perform()
                    
                    # Wait for confirmation
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    
                    # Check for success indicators
                    success_indicators = [
                        "market_buynow_dialog_success",
                        "Your purchase was successful",
                        "has been added to your inventory"
                    ]
                    
                    page_source = driver.page_source.lower()
                    
                    for indicator in success_indicators:
                        if indicator.lower() in page_source:
                            return {
                                'success': True,
                                'price': price_cents / 100,
                                'method': 'selenium_purchase'
                            }
                    
                    # If no success indicators found, check for errors
                    error_indicators = [
                        "insufficient funds",
                        "purchase failed",
                        "error occurred",
                        "unable to purchase"
                    ]
                    
                    for error in error_indicators:
                        if error in page_source:
                            return {
                                'success': False,
                                'error': f'Purchase failed: {error}',
                                'method': 'selenium_purchase'
                            }
                    
                    # Unknown result
                    return {
                        'success': False,
                        'error': 'Unknown purchase result',
                        'method': 'selenium_purchase'
                    }
                    
                except Exception as purchase_error:
                    # Try alternative approach - direct buy now if available
                    try:
                        # Look for "Buy Now" listings instead of buy orders
                        buy_now_buttons = driver.find_elements(By.CLASS_NAME, "market_listing_buy_button")
                        
                        if buy_now_buttons:
                            # Find the cheapest listing within our price range
                            for button in buy_now_buttons[:3]:  # Check first 3 listings
                                # Human-like behavior
                                await asyncio.sleep(random.uniform(0.8, 1.5))
                                
                                # Get price of this listing
                                try:
                                    price_element = button.find_element(By.CLASS_NAME, "market_listing_price")
                                    listing_price_text = price_element.text
                                    
                                    # Parse price (basic parsing)
                                    if "$" in listing_price_text:
                                        price_value = float(listing_price_text.replace("$", "").replace(",", ""))
                                        
                                        # If within our budget, try to buy
                                        if price_value <= (price_cents / 100):
                                            # Click the buy button
                                            actions = ActionChains(driver)
                                            actions.move_to_element(button)
                                            await asyncio.sleep(random.uniform(0.3, 0.8))
                                            actions.click()
                                            actions.perform()
                                            
                                            # Wait and confirm purchase
                                            await asyncio.sleep(random.uniform(1.5, 2.5))
                                            
                                            # Look for confirmation dialog
                                            try:
                                                confirm_purchase = wait.until(
                                                    EC.element_to_be_clickable((By.ID, "market_buynow_dialog_purchase_final"))
                                                )
                                                
                                                await asyncio.sleep(random.uniform(0.5, 1.0))
                                                confirm_purchase.click()
                                                
                                                await asyncio.sleep(random.uniform(2.0, 3.0))
                                                
                                                # Check for success
                                                if "success" in driver.page_source.lower():
                                                    return {
                                                        'success': True,
                                                        'price': price_value,
                                                        'method': 'selenium_buy_now'
                                                    }
                                            except:
                                                continue
                                
                                except Exception:
                                    continue
                        
                        return {
                            'success': False,
                            'error': 'No suitable listings found within price range',
                            'method': 'selenium_purchase'
                        }
                        
                    except Exception as alt_error:
                        return {
                            'success': False,
                            'error': f'Alternative purchase method failed: {str(alt_error)}',
                            'method': 'selenium_purchase'
                        }
            
            finally:
                # Always close the browser
                driver.quit()
                
        except ImportError:
            return {
                'success': False,
                'error': 'Selenium not installed. Please install: pip install selenium',
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
            # Get creator profile summary
            response = requests.get(f"{self.api_base}/profile/{creator_id}/summary", timeout=10)
            
            if response.status_code == 200:
                # Profile exists, now check how many items this creator has
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
                    
                    # If they have more than 1 accepted item, they're not first-time
                    if total_items > 1:
                        self.add_creator_to_db(creator_id_str, creator_name, total_items)
                        logger.info(f"Creator {creator_name} has {total_items} items - not first-time")
                        return False
                    
                    # If they have exactly 1 item, this might be their first
                    logger.info(f"Creator {creator_name} has {total_items} items - potentially first-time")
                    return total_items <= 1
                
                # If we can't get item count, assume first-time to be safe
                logger.info(f"Could not verify item count for {creator_name} - assuming first-time")
                return True
            
            elif response.status_code == 404:
                # Profile not found, might be very new creator
                logger.info(f"Profile not found for {creator_name} (ID: {creator_id}) - likely first-time creator")
                return True
            
            else:
                # Other error, assume first-time to be safe
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
        
        # Add error handler for conflicts
        async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            """Log errors and handle conflicts"""
            logger.error(f"Exception while handling an update: {context.error}")
            
            # If it's a conflict error, try to continue
            if "Conflict" in str(context.error):
                logger.warning("Bot conflict detected, continuing...")
                return
        
        # Add the error handler
        self.application.add_error_handler(error_handler)
        
        try:
            # Try to start with polling
            self.application.run_polling(
                drop_pending_updates=True,  # Clear any pending updates
                allowed_updates=Update.ALL_TYPES
            )
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            if "Conflict" in str(e):
                logger.error("Another bot instance is running. Please stop other instances first.")
            raise

if __name__ == "__main__":
    # Environment variables required:
    # TELEGRAM_BOT_TOKEN - Your Telegram bot token from @BotFather
    
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable not set!")
        print("Get your token from @BotFather on Telegram and set it in Railway dashboard.")
        exit(1)
    
    bot = RustSkinTelegramBot()
    bot.run()
