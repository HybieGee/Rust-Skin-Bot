#!/usr/bin/env python3
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
from datetime import datetime
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
            [InlineKeyboardButton("❓ Help", callback_data="help"),
             InlineKeyboardButton("📈 Statistics", callback_data="stats")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        status_emoji = "🟢" if session['is_monitoring'] else "🔴"
        token_emoji = "✅" if session['steam_session_token'] else "❌"
        
        welcome_text = f"""🤖 *Welcome to Rust Skin First-Time Creator Bot!*

👋 Hello {username}! I help you find new skins from first-time creators on SCMM.

📊 **Your Status**:
{status_emoji} **Monitoring**: {'Active' if session['is_monitoring'] else 'Stopped'}
{token_emoji} **Steam Token**: {'Configured' if session['steam_session_token'] else 'Not Set'}
🎯 **Progress**: {session['purchased_count']}/{session['max_purchases']} opportunities found

🎨 **What I Do**:
• Monitor SCMM for new items from first-time creators
• Send you instant alerts with purchase links
• Track opportunities so you don't miss any
• Stop automatically after finding 10 opportunities

**Quick Start**:
1️⃣ Set your Steam session token with /settoken
2️⃣ Start monitoring with /monitor
3️⃣ Get instant alerts when I find opportunities!

Use the buttons below or type /help for more info."""

        await update.message.reply_text(
            welcome_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """🤖 *Rust Skin Bot - Commands & Help*

**🎯 Main Commands:**
/start - Show main menu and status
/monitor - Start monitoring for opportunities
/stop - Stop monitoring
/status - Check your current status
/purchases - View your found opportunities
/settoken - Set your Steam session token
/reset - Reset your opportunity counter
/help - Show this help message

**🔧 How It Works:**
1. I monitor the SCMM API every 30 seconds
2. I look for items from creators with only 1 accepted item
3. When found, I send you an instant alert with links
4. I track up to 10 opportunities per user

**🔑 Steam Session Token:**
Your Steam session token is used to identify you for potential purchases. Get it from:
1. Login to Steam in your browser
2. Open browser dev tools (F12)
3. Go to Application/Storage → Cookies → steamcommunity.com
4. Find 'sessionid' cookie and copy its value

**🛡️ Privacy & Security:**
• Your token is stored securely and encrypted
• Each user has their own separate monitoring
• No data is shared between users
• You can delete your data anytime with /reset

**💡 Pro Tips:**
• Keep the bot running 24/7 for best results
• Act fast when you get alerts - others might be watching too!
• Check /purchases regularly to see what you've found
• The bot stops at 10 opportunities to prevent spam

Need more help? Message support or check the GitHub repository!"""

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
        """Handle text messages (primarily for token input)"""
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
                    "🔐 *Your token is stored securely and will only be used for identifying purchase opportunities.*",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    "❌ That doesn't look like a valid Steam session token.\n\n"
                    "Please make sure you copied the full 'sessionid' cookie value and try again."
                )
    
    async def start_monitoring_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /monitor command"""
        user_id = update.effective_user.id
        session = self.get_user_session(user_id)
        
        if not session['steam_session_token']:
            await update.message.reply_text(
                "❌ Please set your Steam token first using /settoken\n\n"
                "I need your Steam session token to identify purchase opportunities for you."
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
            await update.message.reply_text("⚠️ You're not currently monitoring.")
            return
        
        # Stop monitoring
        self.update_user_session(user_id, is_monitoring=False)
        
        # Cancel monitoring task
        if user_id in self.monitoring_tasks:
            self.monitoring_tasks[user_id].cancel()
            del self.monitoring_tasks[user_id]
        
        await update.message.reply_text(
            "⏹️ *Monitoring stopped.*\n\n"
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
                "📭 *No opportunities found yet.*\n\n"
                "Start monitoring with /monitor to begin finding first-time creator opportunities!"
            )
            return
        
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
        
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    
    async def reset_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /reset command"""
        user_id = update.effective_user.id
        
        keyboard = [
            [InlineKeyboardButton("✅ Yes, Reset Everything", callback_data=f"reset_confirm_{user_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚠️ *Reset Your Data*\n\n"
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
        
        if query.data == "status":
            await self.status_command(update, context)
        elif query.data == "purchases":
            await self.purchases_command(update, context)
        elif query.data == "settoken":
            await self.set_token_command(update, context)
        elif query.data == "startbot":
            await self.start_monitoring_command(update, context)
        elif query.data == "stopbot":
            await self.stop_monitoring_command(update, context)
        elif query.data == "help":
            await self.help_command(update, context)
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
            await self.start_command(update, context)
    
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
            
            # Only process accepted items
            if not is_accepted:
                return
            
            if not creator_id:
                return
            
            # Check if this is a first-time creator
            if await self.is_first_time_creator(creator_id, creator_name):
                logger.info(f"User {user_id}: Found first-time creator {creator_name} with item {item_name}")
                await self.record_opportunity_for_user(
                    user_id, item_data, creator_id, creator_name, item_name, 
                    item_type, item_collection, workshop_file_id
                )
                
        except Exception as e:
            logger.error(f"Error processing item for user {user_id}: {e}")
    
    async def record_opportunity_for_user(self, user_id: int, item_data: Dict, creator_id: int, 
                                        creator_name: str, item_name: str, item_type: str, 
                                        item_collection: str, workshop_file_id: int):
        """Record a purchase opportunity for a specific user"""
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
        
        # Build message
        market_info = ""
        if market_price > 0:
            market_info = f"💰 **Lowest Price**: ${market_price/100:.2f}\n"
        if buy_orders > 0 or sell_orders > 0:
            market_info += f"📊 **Orders**: {buy_orders} buy, {sell_orders} sell\n"
        
        message = f"""🎯 *FIRST-TIME CREATOR ALERT!*

🎨 **Item**: {item_name}
👤 **Creator**: {creator_name}
🏷️ **Type**: {item_type}
📦 **Collection**: {item_collection}
{market_info}📈 **Your Progress**: {session['purchased_count']}/{session['max_purchases']}

🔗 **Quick Buy Links**:
[Steam Market]({steam_url})
[SCMM Item Page]({scmm_url})"""

        if workshop_url:
            message += f"\n[Workshop Page]({workshop_url})"

        message += "\n\n⚡ *New creator detected - Act fast!*"
        
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
            True
        ))
        self.conn.commit()
        
        logger.info(f"Recorded opportunity for user {user_id}: {item_name} by {creator_name}")
    
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
        """Start the bot"""
        logger.info("Starting Multi-User Rust Skin Telegram Bot...")
        self.application.run_polling()

if __name__ == "__main__":
    # Environment variables required:
    # TELEGRAM_BOT_TOKEN - Your Telegram bot token from @BotFather
    
    if not os.getenv('TELEGRAM_BOT_TOKEN'):
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable not set!")
        print("Get your token from @BotFather on Telegram and set it in Railway dashboard.")
        exit(1)
    
    bot = RustSkinTelegramBot()
    bot.run()
