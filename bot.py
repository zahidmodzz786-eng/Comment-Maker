import logging
import json
import os
import certifi
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from pymongo import MongoClient
from datetime import datetime
import asyncio
import traceback

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# States for conversation handlers
WAITING_FOR_CHANNEL, WAITING_FOR_BUTTON_NAME, WAITING_FOR_COMMENTS, WAITING_FOR_OVER_MESSAGE = range(4)

# Get environment variables
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '').split(',')))
MONGO_URI = os.environ.get('MONGO_URI')

print(f"🔧 Starting bot with Admin IDs: {ADMIN_IDS}")
print(f"🔧 MongoDB URI: {MONGO_URI[:50]}...")  # Print first 50 chars only for security

# Initialize MongoDB with SSL fix
try:
    # Create client with certifi for SSL certificates
    client = MongoClient(
        MONGO_URI,
        tlsCAFile=certifi.where(),  # This fixes SSL issues
        serverSelectionTimeoutMS=30000  # 30 second timeout
    )
    
    # Test connection
    client.admin.command('ping')
    print("✅ Successfully connected to MongoDB!")
    
    # Initialize database and collections
    db = client['comment_bot']
    
    users_collection = db['users']
    channels_collection = db['channels']
    buttons_collection = db['buttons']
    comments_collection = db['comments']
    settings_collection = db['settings']
    pending_approvals_collection = db['pending_approvals']
    
    # Initialize default settings if not exists
    if settings_collection.count_documents({}) == 0:
        settings_collection.insert_one({
            'bot_status': True,
            'over_message': 'No more comments available for this app.',
            'buttons': []
        })
        print("✅ Default settings created!")
    else:
        print("✅ Settings already exist in database")
        
except Exception as e:
    print(f"❌ MongoDB connection failed: {e}")
    print("Please check your connection string and network settings")
    # Create dummy collections to prevent crashes (will be replaced when connection works)
    class DummyCollection:
        def find(self, *args, **kwargs): return []
        def find_one(self, *args, **kwargs): return None
        def insert_one(self, *args, **kwargs): return None
        def update_one(self, *args, **kwargs): return None
        def delete_one(self, *args, **kwargs): return None
        def count_documents(self, *args, **kwargs): return 0
    
    users_collection = DummyCollection()
    channels_collection = DummyCollection()
    buttons_collection = DummyCollection()
    comments_collection = DummyCollection()
    settings_collection = DummyCollection()
    pending_approvals_collection = DummyCollection()

class CommentBot:
    def __init__(self):
        # Safely get settings with error handling
        self.load_settings()
    
    def load_settings(self):
        """Load settings safely with error handling"""
        try:
            settings = settings_collection.find_one()
            if settings:
                self.bot_status = settings.get('bot_status', True)
                self.over_message = settings.get('over_message', 'No more comments available for this app.')
                print(f"✅ Settings loaded: bot_status={self.bot_status}")
            else:
                print("⚠️ No settings found, creating default...")
                # Create default settings if not exists
                settings_collection.insert_one({
                    'bot_status': True,
                    'over_message': 'No more comments available for this app.',
                    'buttons': []
                })
                self.bot_status = True
                self.over_message = 'No more comments available for this app.'
                print("✅ Created default settings in __init__")
        except Exception as e:
            print(f"⚠️ Error loading settings: {e}")
            traceback.print_exc()
            self.bot_status = True
            self.over_message = 'No more comments available for this app.'

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        
        print(f"👤 User {user_id} (@{user.username}) started the bot")
        
        # Reload settings to ensure latest status
        self.load_settings()
        
        # Check if bot is on
        if not self.bot_status:
            await update.message.reply_text("No Apps Available For Comment")
            return
        
        # Check if user is admin
        if user_id in ADMIN_IDS:
            print(f"👑 Admin {user_id} accessed bot")
            await self.show_admin_panel_direct(update, context)
            return
        
        # Check if user is approved
        try:
            user_data = users_collection.find_one({'user_id': user_id})
        except Exception as e:
            print(f"❌ Error checking user data: {e}")
            user_data = None
        
        if user_data and user_data.get('approved', False):
            print(f"✅ Approved user {user_id} accessing bot")
            # Show main menu
            await self.show_main_menu(update, context)
        elif user_data and user_data.get('rejected', False):
            print(f"❌ Rejected user {user_id} tried to access")
            await update.message.reply_text(
                "Sorry But Your Approval Has Been Rejected By Owner. "
                "If You Have Any Issue Contact To @DTXZAHID"
            )
        elif user_data and user_data.get('pending', False):
            print(f"⏳ Pending user {user_id} tried to access")
            await update.message.reply_text(
                "Your approval request is already pending. Please wait for admin response."
            )
        else:
            print(f"🆕 New user {user_id} started bot")
            # Check force join channels first
            await self.check_force_join_before_approval(update, context)

    async def admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /admin command"""
        user_id = update.effective_user.id
        
        print(f"👑 User {user_id} used /admin command")
        
        if user_id in ADMIN_IDS:
            await self.show_admin_panel_direct(update, context)
        else:
            print(f"⛔ Non-admin {user_id} tried to use /admin")
            await update.message.reply_text("⛔ Unauthorized! This command is for admins only.")

    async def show_admin_panel_direct(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show admin panel directly without requiring main menu"""
        try:
            print("🔄 Loading admin panel directly...")
            
            # Reload settings to ensure latest
            self.load_settings()
            
            # Try to get settings from database
            try:
                settings = settings_collection.find_one()
                if not settings:
                    print("⚠️ No settings found in database, creating...")
                    # Create settings if still not exists
                    settings_collection.insert_one({
                        'bot_status': True,
                        'over_message': 'No more comments available for this app.',
                        'buttons': []
                    })
                    settings = settings_collection.find_one()
                    print("✅ Settings created successfully")
            except Exception as e:
                print(f"❌ Error accessing settings: {e}")
                traceback.print_exc()
                settings = {'bot_status': True, 'over_message': 'Default message'}
            
            bot_status = settings.get('bot_status', True)
            status_text = "ON ✅" if bot_status else "OFF ❌"
            
            print(f"📊 Admin panel loaded with status: {status_text}")
            
            keyboard = [
                [InlineKeyboardButton("📢 Manage Channels", callback_data="manage_channels")],
                [InlineKeyboardButton("🔘 Manage Buttons", callback_data="manage_buttons")],
                [InlineKeyboardButton("➕ Add Comments", callback_data="show_buttons_for_comments")],
                [InlineKeyboardButton("📊 View Stats", callback_data="view_stats")],
                [InlineKeyboardButton("✏️ Set Over Message", callback_data="set_over_message")],
                [InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")]
            ]
            
            # Add bot status button at the top
            if bot_status:
                keyboard.insert(0, [InlineKeyboardButton("🤖 Turn Bot OFF", callback_data="bot_off")])
            else:
                keyboard.insert(0, [InlineKeyboardButton("🤖 Turn Bot ON", callback_data="bot_on")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "⚙️ Admin Panel\n\n"
                f"Bot Status: {status_text}\n"
                "Select an option:",
                reply_markup=reply_markup
            )
            print("✅ Admin panel displayed successfully")
            
        except Exception as e:
            print(f"❌ Error in admin panel: {e}")
            traceback.print_exc()
            # Fallback simple admin panel
            keyboard = [
                [InlineKeyboardButton("📢 Manage Channels", callback_data="manage_channels")],
                [InlineKeyboardButton("🔘 Manage Buttons", callback_data="manage_buttons")],
                [InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "⚙️ Admin Panel (Limited Mode)\n\n"
                "Some features may be unavailable due to database issues.\n"
                "Please check MongoDB connection.",
                reply_markup=reply_markup
            )

    async def check_force_join_before_approval(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check force join channels before showing approval option"""
        try:
            channels = list(channels_collection.find())
        except Exception as e:
            print(f"❌ Error getting channels: {e}")
            channels = []
        
        if not channels:
            # No channels to join, show approval option
            await self.show_approval_option(update, context)
            return
        
        user_id = update.effective_user.id
        not_joined = []
        
        for channel in channels:
            try:
                member = await context.bot.get_chat_member(chat_id=channel['channel_id'], user_id=user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    not_joined.append(channel)
            except Exception as e:
                print(f"⚠️ Error checking channel {channel.get('channel_name')}: {e}")
                not_joined.append(channel)
        
        if not_joined:
            # Create buttons for channels
            keyboard = []
            for channel in not_joined:
                keyboard.append([InlineKeyboardButton(
                    f"Join {channel['channel_name']}", 
                    url=channel['channel_link']
                )])
            
            keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join_before_approval")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "Please join these channels to use the bot:",
                reply_markup=reply_markup
            )
        else:
            await self.show_approval_option(update, context)

    async def show_approval_option(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show approval request option to user"""
        user = update.effective_user
        
        # Check if already pending
        try:
            existing = pending_approvals_collection.find_one({'user_id': user.id})
        except Exception as e:
            print(f"❌ Error checking pending approvals: {e}")
            existing = None
        
        if existing:
            await update.message.reply_text(
                "Your approval request is already pending. Please wait for admin response."
            )
            return
        
        # Check if already approved
        try:
            user_data = users_collection.find_one({'user_id': user.id})
        except Exception as e:
            print(f"❌ Error checking user data: {e}")
            user_data = None
            
        if user_data and user_data.get('approved', False):
            await self.show_main_menu(update, context)
            return
        
        # Check if rejected
        if user_data and user_data.get('rejected', False):
            await update.message.reply_text(
                "Sorry But Your Approval Has Been Rejected By Owner. "
                "If You Have Any Issue Contact To @DTXZAHID"
            )
            return
        
        # Show approval option with buttons
        keyboard = [
            [InlineKeyboardButton("✅ Request Approval", callback_data="request_approval")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_approval")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "To use this bot, you need approval from admin.\n\n"
            "Do you want to send an approval request?",
            reply_markup=reply_markup
        )

    async def request_approval_handler(self, query, context):
        """Handle approval request from user"""
        user = query.from_user
        
        print(f"📨 User {user.id} requested approval")
        
        # Check if already pending
        try:
            existing = pending_approvals_collection.find_one({'user_id': user.id})
        except Exception as e:
            print(f"❌ Error checking pending: {e}")
            existing = None
            
        if existing:
            await query.message.edit_text(
                "Your approval request is already pending. Please wait for admin response."
            )
            return
        
        # Check if already approved
        try:
            user_data = users_collection.find_one({'user_id': user.id})
        except Exception as e:
            print(f"❌ Error checking user: {e}")
            user_data = None
            
        if user_data and user_data.get('approved', False):
            await query.message.edit_text("You are already approved! Send /start to use the bot.")
            return
        
        # Save pending approval
        try:
            pending_approvals_collection.insert_one({
                'user_id': user.id,
                'username': user.username,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'date': datetime.now(),
                'status': 'pending'
            })
            
            # Update user status
            users_collection.update_one(
                {'user_id': user.id},
                {'$set': {'pending': True}},
                upsert=True
            )
            print(f"✅ Saved pending approval for user {user.id}")
        except Exception as e:
            print(f"❌ Error saving pending approval: {e}")
            await query.message.edit_text("❌ Error sending request. Please try again later.")
            return
        
        # Notify admins
        admin_notified = False
        for admin_id in ADMIN_IDS:
            try:
                keyboard = [
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user.id}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🔔 New Approval Request:\n\n"
                         f"User ID: {user.id}\n"
                         f"Username: @{user.username if user.username else 'None'}\n"
                         f"Name: {user.first_name} {user.last_name or ''}\n"
                         f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    reply_markup=reply_markup
                )
                admin_notified = True
                print(f"✅ Notified admin {admin_id}")
            except Exception as e:
                print(f"❌ Failed to notify admin {admin_id}: {e}")
        
        if admin_notified:
            await query.message.edit_text(
                "✅ Your approval request has been sent to admin!\n\n"
                "Please wait for response. You'll be notified once approved."
            )
        else:
            await query.message.edit_text(
                "⚠️ Your request was saved but couldn't notify admins.\n"
                "Please contact @DTXZAHID directly."
            )

    async def handle_approval(self, query, context):
        """Handle admin approval/rejection"""
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("You are not authorized!")
            return
        
        data = query.data
        user_id = int(data.split('_')[1])
        
        print(f"👑 Admin {query.from_user.id} is handling user {user_id}: {data}")
        
        if data.startswith("approve"):
            # Approve user
            try:
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {
                        'approved': True, 
                        'rejected': False,
                        'pending': False
                    }},
                    upsert=True
                )
                pending_approvals_collection.delete_one({'user_id': user_id})
                print(f"✅ User {user_id} approved in database")
            except Exception as e:
                print(f"❌ Error approving user in DB: {e}")
            
            # Notify user
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="✅ Welcome To Comment Provider Bot By Zahid!\n\n"
                         "Your approval request has been accepted!\n"
                         "Send /start to begin using the bot."
                )
                print(f"✅ Notified user {user_id} of approval")
            except Exception as e:
                print(f"❌ Failed to notify user {user_id}: {e}")
            
            await query.message.edit_text(
                query.message.text + "\n\n✅ User approved successfully!"
            )
        else:
            # Reject user
            try:
                users_collection.update_one(
                    {'user_id': user_id},
                    {'$set': {
                        'approved': False, 
                        'rejected': True,
                        'pending': False
                    }},
                    upsert=True
                )
                pending_approvals_collection.delete_one({'user_id': user_id})
                print(f"✅ User {user_id} rejected in database")
            except Exception as e:
                print(f"❌ Error rejecting user in DB: {e}")
            
            # Notify user
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="❌ Sorry But Your Approval Has Been Rejected By Owner.\n\n"
                         "If You Have Any Issue Contact To @DTXZAHID"
                )
                print(f"✅ Notified user {user_id} of rejection")
            except Exception as e:
                print(f"❌ Failed to notify user {user_id}: {e}")
            
            await query.message.edit_text(
                query.message.text + "\n\n❌ User rejected!"
            )

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data
        user_id = query.from_user.id
        
        print(f"🔄 Callback from user {user_id}: {data}")
        
        if data == "request_approval":
            await self.request_approval_handler(query, context)
        elif data == "cancel_approval":
            await query.message.edit_text("Approval request cancelled. Send /start if you change your mind.")
        elif data == "check_join_before_approval":
            await self.check_join_before_approval(query, context)
        elif data.startswith("approve_") or data.startswith("reject_"):
            await self.handle_approval(query, context)
        elif data == "admin_panel":
            await self.show_admin_panel(query, context)
        elif data.startswith("button_"):
            button_id = data.replace("button_", "")
            await self.show_comment_confirmation(query, context, button_id)
        elif data.startswith("agree_"):
            button_id = data.replace("agree_", "")
            await self.provide_comment(query, context, button_id)
        elif data == "no_thanks":
            await self.show_main_menu_from_callback(query, context)
        elif data.startswith("add_comments_"):
            button_id = data.replace("add_comments_", "")
            context.user_data['current_button'] = button_id
            await query.message.reply_text(
                "Please send comments in this format:\n"
                "Comment 1, Comment 2, Comment 3, ...\n\n"
                "You can send as many comments as you want separated by commas."
            )
        elif data.startswith("stats_"):
            button_id = data.replace("stats_", "")
            await self.show_button_stats(query, context, button_id)
        elif data == "add_channel":
            await query.message.reply_text("Please send the channel username or ID:")
            context.user_data['waiting_for_channel'] = True
        elif data == "remove_channel":
            await self.show_channels_to_remove(query, context)
        elif data == "add_button":
            await query.message.reply_text("Please send the name for the new button:")
            context.user_data['waiting_for_button_name'] = True
        elif data == "remove_button":
            await self.show_buttons_to_remove(query, context)
        elif data == "bot_on":
            try:
                settings_collection.update_one({}, {'$set': {'bot_status': True}})
                self.bot_status = True
                await query.message.edit_text("Bot turned ON successfully!")
            except Exception as e:
                print(f"❌ Error turning bot on: {e}")
                await query.message.edit_text("❌ Error turning bot on. Check database.")
            await self.show_admin_panel(query, context)
        elif data == "bot_off":
            try:
                settings_collection.update_one({}, {'$set': {'bot_status': False}})
                self.bot_status = False
                await query.message.edit_text("Bot turned OFF successfully!")
            except Exception as e:
                print(f"❌ Error turning bot off: {e}")
                await query.message.edit_text("❌ Error turning bot off. Check database.")
            await self.show_admin_panel(query, context)
        elif data == "set_over_message":
            await query.message.reply_text("Please send the new over message:")
            context.user_data['waiting_for_over_message'] = True
        elif data == "manage_channels":
            await self.show_manage_channels(query, context)
        elif data == "manage_buttons":
            await self.show_manage_buttons(query, context)
        elif data == "show_buttons_for_comments":
            await self.show_buttons_for_comments(query, context)
        elif data == "view_stats":
            await self.show_view_stats(query, context)
        elif data == "back_to_main":
            await self.show_main_menu_from_callback(query, context)
        elif data.startswith("remove_channel_"):
            channel_id = data.replace("remove_channel_", "")
            try:
                channels_collection.delete_one({'channel_id': channel_id})
                await query.message.edit_text("✅ Channel removed successfully!")
            except Exception as e:
                print(f"❌ Error removing channel: {e}")
                await query.message.edit_text("❌ Error removing channel.")
            await self.show_manage_channels(query, context)
        elif data.startswith("remove_button_"):
            button_id = data.replace("remove_button_", "")
            try:
                buttons_collection.delete_one({'button_id': button_id})
                # Also remove related comments
                comments_collection.delete_many({'button_id': button_id})
                await query.message.edit_text("✅ Button removed successfully!")
            except Exception as e:
                print(f"❌ Error removing button: {e}")
                await query.message.edit_text("❌ Error removing button.")
            await self.show_manage_buttons(query, context)

    async def check_join_before_approval(self, query, context):
        """Check if user has joined all channels before showing approval option"""
        try:
            channels = list(channels_collection.find())
        except Exception as e:
            print(f"❌ Error getting channels: {e}")
            channels = []
            
        user_id = query.from_user.id
        not_joined = []
        
        for channel in channels:
            try:
                member = await context.bot.get_chat_member(chat_id=channel['channel_id'], user_id=user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    not_joined.append(channel)
            except Exception as e:
                print(f"⚠️ Error checking channel: {e}")
                not_joined.append(channel)
        
        if not_joined:
            keyboard = []
            for channel in not_joined:
                keyboard.append([InlineKeyboardButton(
                    f"Join {channel['channel_name']}", 
                    url=channel['channel_link']
                )])
            
            keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join_before_approval")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text(
                "You haven't joined all channels yet. Please join:",
                reply_markup=reply_markup
            )
        else:
            # Show approval option
            keyboard = [
                [InlineKeyboardButton("✅ Request Approval", callback_data="request_approval")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_approval")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.edit_text(
                "✅ You've joined all channels!\n\n"
                "Do you want to send an approval request?",
                reply_markup=reply_markup
            )

    async def show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show main menu with buttons for approved users"""
        try:
            buttons = list(buttons_collection.find())
        except Exception as e:
            print(f"❌ Error getting buttons: {e}")
            buttons = []
        
        if not buttons:
            message = update.message if update.message else update.callback_query.message
            await message.reply_text(
                "No apps available at the moment. Please check back later."
            )
            return
        
        keyboard = []
        for button in buttons:
            keyboard.append([InlineKeyboardButton(
                button['button_name'], 
                callback_data=f"button_{button['button_id']}"
            )])
        
        # Add admin panel button if user is admin
        if update.effective_user.id in ADMIN_IDS:
            keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message = update.message if update.message else update.callback_query.message
        await message.reply_text(
            "Welcome To Comment Provider Bot By Zahid\n\nPlease select an app:",
            reply_markup=reply_markup
        )

    async def show_admin_panel(self, query, context):
        """Show admin panel from callback"""
        if query.from_user.id not in ADMIN_IDS:
            await query.message.reply_text("Unauthorized access!")
            return
        
        try:
            print("🔄 Loading admin panel from callback...")
            
            # Reload settings to ensure latest
            self.load_settings()
            
            # Try to get settings from database
            try:
                settings = settings_collection.find_one()
                if not settings:
                    print("⚠️ No settings found in database, creating...")
                    # Create settings if still not exists
                    settings_collection.insert_one({
                        'bot_status': True,
                        'over_message': 'No more comments available for this app.',
                        'buttons': []
                    })
                    settings = settings_collection.find_one()
                    print("✅ Settings created successfully")
            except Exception as e:
                print(f"❌ Error accessing settings: {e}")
                traceback.print_exc()
                settings = {'bot_status': True, 'over_message': 'Default message'}
            
            bot_status = settings.get('bot_status', True)
            status_text = "ON ✅" if bot_status else "OFF ❌"
            
            print(f"📊 Admin panel loaded with status: {status_text}")
            
            keyboard = [
                [InlineKeyboardButton("📢 Manage Channels", callback_data="manage_channels")],
                [InlineKeyboardButton("🔘 Manage Buttons", callback_data="manage_buttons")],
                [InlineKeyboardButton("➕ Add Comments", callback_data="show_buttons_for_comments")],
                [InlineKeyboardButton("📊 View Stats", callback_data="view_stats")],
                [InlineKeyboardButton("✏️ Set Over Message", callback_data="set_over_message")],
                [InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")]
            ]
            
            # Add bot status button at the top
            if bot_status:
                keyboard.insert(0, [InlineKeyboardButton("🤖 Turn Bot OFF", callback_data="bot_off")])
            else:
                keyboard.insert(0, [InlineKeyboardButton("🤖 Turn Bot ON", callback_data="bot_on")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.edit_text(
                "⚙️ Admin Panel\n\n"
                f"Bot Status: {status_text}\n"
                "Select an option:",
                reply_markup=reply_markup
            )
            print("✅ Admin panel displayed successfully from callback")
            
        except Exception as e:
            print(f"❌ Error in admin panel callback: {e}")
            traceback.print_exc()
            # Fallback simple admin panel
            keyboard = [
                [InlineKeyboardButton("📢 Manage Channels", callback_data="manage_channels")],
                [InlineKeyboardButton("🔘 Manage Buttons", callback_data="manage_buttons")],
                [InlineKeyboardButton("🔙 Main Menu", callback_data="back_to_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.message.edit_text(
                "⚙️ Admin Panel (Limited Mode)\n\n"
                "Some features may be unavailable due to database issues.\n"
                "Please check MongoDB connection.",
                reply_markup=reply_markup
            )

    # ... (rest of the methods remain the same as before - show_comment_confirmation, provide_comment, show_manage_channels, etc.)

    async def show_comment_confirmation(self, query, context, button_id):
        keyboard = [
            [InlineKeyboardButton("✅ I Agree For This", callback_data=f"agree_{button_id}")],
            [InlineKeyboardButton("❌ No Sorry", callback_data="no_thanks")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Do You Really Want This App Comment?\n"
            "Note:- If You Don't Do 2-3 Apps And Take Comment You Maybe Banned",
            reply_markup=reply_markup
        )

    async def provide_comment(self, query, context, button_id):
        # Get next available comment - ATOMIC operation prevents duplicates
        try:
            comment = comments_collection.find_one_and_delete(
                {'button_id': button_id, 'used': False},
                sort=[('_id', 1)]
            )
        except Exception as e:
            print(f"❌ Error getting comment: {e}")
            await query.message.edit_text("❌ Error getting comment. Please try again.")
            return
        
        if comment:
            # Mark as used and track who got it
            try:
                comments_collection.update_one(
                    {'_id': comment['_id']},
                    {'$set': {
                        'used': True, 
                        'used_by': query.from_user.id, 
                        'used_date': datetime.now()
                    }}
                )
            except Exception as e:
                print(f"❌ Error marking comment as used: {e}")
            
            message_text = f"Here Is Your Comment Go And Do Review\n\n<code>{comment['comment']}</code>"
            
            await query.message.edit_text(
                message_text,
                parse_mode='HTML'
            )
        else:
            # No comments available
            self.load_settings()  # Reload to get latest over_message
            await query.message.edit_text(self.over_message)

    async def show_manage_channels(self, query, context):
        keyboard = [
            [InlineKeyboardButton("➕ Add Channel", callback_data="add_channel")],
            [InlineKeyboardButton("❌ Remove Channel", callback_data="remove_channel")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            channels = list(channels_collection.find())
        except Exception as e:
            print(f"❌ Error getting channels: {e}")
            channels = []
        
        channel_list = "No channels added yet." if not channels else "\n".join([
            f"• {c.get('channel_name', 'Unknown')} ({c.get('channel_id', 'No ID')})" for c in channels
        ])
        
        await query.message.edit_text(
            f"📢 Manage Channels\n\nCurrent Channels:\n{channel_list}\n\nSelect option:",
            reply_markup=reply_markup
        )

    async def show_manage_buttons(self, query, context):
        keyboard = [
            [InlineKeyboardButton("➕ Add Button", callback_data="add_button")],
            [InlineKeyboardButton("❌ Remove Button", callback_data="remove_button")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            buttons = list(buttons_collection.find())
        except Exception as e:
            print(f"❌ Error getting buttons: {e}")
            buttons = []
        
        button_list = "No buttons added yet." if not buttons else "\n".join([
            f"• {b.get('button_name', 'Unknown')}" for b in buttons
        ])
        
        await query.message.edit_text(
            f"🔘 Manage Buttons\n\nCurrent Buttons:\n{button_list}\n\nSelect option:",
            reply_markup=reply_markup
        )

    async def show_buttons_for_comments(self, query, context):
        try:
            buttons = list(buttons_collection.find())
        except Exception as e:
            print(f"❌ Error getting buttons: {e}")
            buttons = []
        
        if not buttons:
            await query.message.edit_text(
                "No buttons found. Please add buttons first.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="admin_panel")
                ]])
            )
            return
        
        keyboard = []
        for button in buttons:
            keyboard.append([InlineKeyboardButton(
                button.get('button_name', 'Unknown'), 
                callback_data=f"add_comments_{button.get('button_id', '')}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Select button to add comments:",
            reply_markup=reply_markup
        )

    async def show_button_stats(self, query, context, button_id):
        try:
            button = buttons_collection.find_one({'button_id': button_id})
            if not button:
                await query.message.edit_text("Button not found!")
                return
                
            total_comments = comments_collection.count_documents({'button_id': button_id})
            used_comments = comments_collection.count_documents({'button_id': button_id, 'used': True})
            available_comments = total_comments - used_comments
        except Exception as e:
            print(f"❌ Error getting stats: {e}")
            await query.message.edit_text("❌ Error getting stats. Check database.")
            return
        
        stats_text = (
            f"📊 Stats for: {button.get('button_name', 'Unknown')}\n\n"
            f"Total Comments: {total_comments}\n"
            f"Used Comments: {used_comments}\n"
            f"Available: {available_comments}"
        )
        
        await query.message.edit_text(
            stats_text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="view_stats")
            ]])
        )

    async def show_view_stats(self, query, context):
        try:
            buttons = list(buttons_collection.find())
        except Exception as e:
            print(f"❌ Error getting buttons: {e}")
            buttons = []
        
        if not buttons:
            await query.message.edit_text(
                "No buttons found.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="admin_panel")
                ]])
            )
            return
        
        keyboard = []
        for button in buttons:
            keyboard.append([InlineKeyboardButton(
                button.get('button_name', 'Unknown'), 
                callback_data=f"stats_{button.get('button_id', '')}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Select button to view stats:",
            reply_markup=reply_markup
        )

    async def show_channels_to_remove(self, query, context):
        try:
            channels = list(channels_collection.find())
        except Exception as e:
            print(f"❌ Error getting channels: {e}")
            channels = []
        
        if not channels:
            await query.message.edit_text(
                "No channels to remove.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="manage_channels")
                ]])
            )
            return
        
        keyboard = []
        for channel in channels:
            keyboard.append([InlineKeyboardButton(
                f"Remove {channel.get('channel_name', 'Unknown')}", 
                callback_data=f"remove_channel_{channel.get('channel_id', '')}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="manage_channels")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Select channel to remove:",
            reply_markup=reply_markup
        )

    async def show_buttons_to_remove(self, query, context):
        try:
            buttons = list(buttons_collection.find())
        except Exception as e:
            print(f"❌ Error getting buttons: {e}")
            buttons = []
        
        if not buttons:
            await query.message.edit_text(
                "No buttons to remove.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="manage_buttons")
                ]])
            )
            return
        
        keyboard = []
        for button in buttons:
            keyboard.append([InlineKeyboardButton(
                f"Remove {button.get('button_name', 'Unknown')}", 
                callback_data=f"remove_button_{button.get('button_id', '')}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="manage_buttons")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Select button to remove:",
            reply_markup=reply_markup
        )

    async def show_main_menu_from_callback(self, query, context):
        try:
            buttons = list(buttons_collection.find())
        except Exception as e:
            print(f"❌ Error getting buttons: {e}")
            buttons = []
        
        if not buttons:
            await query.message.edit_text(
                "No apps available at the moment. Please check back later."
            )
            return
        
        keyboard = []
        for button in buttons:
            keyboard.append([InlineKeyboardButton(
                button.get('button_name', 'Unknown'), 
                callback_data=f"button_{button.get('button_id', '')}"
            )])
        
        if query.from_user.id in ADMIN_IDS:
            keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Welcome To Comment Provider Bot By Zahid\n\nPlease select an app:",
            reply_markup=reply_markup
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        print(f"📝 Message from user {user_id}: {update.message.text[:50]}...")
        
        if 'current_button' in context.user_data:
            # Adding comments
            button_id = context.user_data['current_button']
            text = update.message.text
            
            # Split comments by comma
            comments = [c.strip() for c in text.split(',') if c.strip()]
            
            # Save comments
            saved_count = 0
            for comment in comments:
                try:
                    comments_collection.insert_one({
                        'button_id': button_id,
                        'comment': comment,
                        'used': False,
                        'added_date': datetime.now()
                    })
                    saved_count += 1
                except Exception as e:
                    print(f"❌ Error saving comment: {e}")
            
            del context.user_data['current_button']
            
            await update.message.reply_text(
                f"✅ Added {saved_count} comments successfully!\n\n"
                f"Comments: {', '.join(comments[:5])}{'...' if len(comments) > 5 else ''}"
            )
            
            # Show admin panel again
            await self.show_admin_panel_from_message(update, context)
        
        elif context.user_data.get('waiting_for_channel'):
            # Adding channel
            channel_input = update.message.text
            context.user_data['channel_input'] = channel_input
            await update.message.reply_text("Please send the channel invite link:")
            context.user_data['waiting_for_channel_link'] = True
            del context.user_data['waiting_for_channel']
        
        elif context.user_data.get('waiting_for_channel_link'):
            # Save channel with link
            channel_input = context.user_data.get('channel_input')
            channel_link = update.message.text
            
            # Generate a unique ID
            channel_id = str(datetime.timestamp()).replace('.', '')
            
            try:
                channels_collection.insert_one({
                    'channel_id': channel_id,
                    'channel_name': channel_input,
                    'channel_link': channel_link
                })
                await update.message.reply_text("✅ Channel added successfully!")
            except Exception as e:
                print(f"❌ Error saving channel: {e}")
                await update.message.reply_text("❌ Error saving channel. Check database.")
            
            del context.user_data['channel_input']
            del context.user_data['waiting_for_channel_link']
            
            await self.show_admin_panel_from_message(update, context)
        
        elif context.user_data.get('waiting_for_button_name'):
            # Adding button
            button_name = update.message.text
            
            # Generate a unique ID
            button_id = str(datetime.timestamp()).replace('.', '')
            
            try:
                buttons_collection.insert_one({
                    'button_id': button_id,
                    'button_name': button_name
                })
                await update.message.reply_text("✅ Button added successfully!")
            except Exception as e:
                print(f"❌ Error saving button: {e}")
                await update.message.reply_text("❌ Error saving button. Check database.")
            
            del context.user_data['waiting_for_button_name']
            
            await self.show_admin_panel_from_message(update, context)
        
        elif context.user_data.get('waiting_for_over_message'):
            # Set over message
            over_message = update.message.text
            
            try:
                settings_collection.update_one(
                    {},
                    {'$set': {'over_message': over_message}},
                    upsert=True
                )
                self.over_message = over_message
                await update.message.reply_text("✅ Over message updated successfully!")
            except Exception as e:
                print(f"❌ Error saving over message: {e}")
                await update.message.reply_text("❌ Error saving over message. Check database.")
            
            del context.user_data['waiting_for_over_message']
            
            await self.show_admin_panel_from_message(update, context)
        
        else:
            # Just start the bot
            await self.start(update, context)

    async def show_admin_panel_from_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show admin panel from message"""
        # Create a fake callback query
        class FakeQuery:
            def __init__(self, user, message):
                self.from_user = user
                self.message = message
                self.data = "admin_panel"
            
            async def edit_text(self, text, reply_markup=None, parse_mode=None):
                await self.message.reply_text(text, reply_markup=reply_markup)
            
            async def answer(self):
                pass
        
        fake_query = FakeQuery(update.effective_user, update.message)
        await self.show_admin_panel(fake_query, context)

def main():
    """Start the bot."""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    bot = CommentBot()
    
    # Add handlers
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("admin", bot.admin_command))
    application.add_handler(CallbackQueryHandler(bot.button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
    
    # Start bot
    print("🤖 Bot is starting...")
    print(f"✅ Admin IDs: {ADMIN_IDS}")
    print(f"✅ Use /admin command to access admin panel")
    print("✅ Bot is now running! Press Ctrl+C to stop.")
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
