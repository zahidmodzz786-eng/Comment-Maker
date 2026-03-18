import logging
import json
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from pymongo import MongoClient
from datetime import datetime
import asyncio

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
MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')

# Initialize MongoDB
client = MongoClient(MONGO_URI)
db = client['comment_bot']

# Collections
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

class CommentBot:
    def __init__(self):
        self.bot_status = settings_collection.find_one()['bot_status']
        self.over_message = settings_collection.find_one()['over_message']

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id
        
        # Check if bot is on
        if not self.bot_status:
            await update.message.reply_text("No Apps Available For Comment")
            return
        
        # Check if user is approved
        user_data = users_collection.find_one({'user_id': user_id})
        
        if user_data and user_data.get('approved', False):
            # Show main menu
            await self.show_main_menu(update, context)
        elif user_data and user_data.get('rejected', False):
            await update.message.reply_text(
                "Sorry But Your Approval Has Been Rejected By Owner. "
                "If You Have Any Issue Contact To @DTXZAHID"
            )
        else:
            # Check force join channels
            await self.check_force_join(update, context)

    async def check_force_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        channels = list(channels_collection.find())
        
        if not channels:
            # No channels to join, proceed to approval
            await self.request_approval(update, context)
            return
        
        user_id = update.effective_user.id
        not_joined = []
        
        for channel in channels:
            try:
                member = await context.bot.get_chat_member(chat_id=channel['channel_id'], user_id=user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    not_joined.append(channel)
            except:
                not_joined.append(channel)
        
        if not_joined:
            # Create buttons for channels
            keyboard = []
            for channel in not_joined:
                keyboard.append([InlineKeyboardButton(
                    f"Join {channel['channel_name']}", 
                    url=channel['channel_link']
                )])
            
            keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "Please join these channels to use the bot:",
                reply_markup=reply_markup
            )
        else:
            await self.request_approval(update, context)

    async def request_approval(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        
        # Check if already pending
        existing = pending_approvals_collection.find_one({'user_id': user.id})
        if existing:
            await update.message.reply_text("Your approval request is already pending. Please wait for admin response.")
            return
        
        # Save pending approval
        pending_approvals_collection.insert_one({
            'user_id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'date': datetime.now()
        })
        
        # Notify admins
        for admin_id in ADMIN_IDS:
            try:
                keyboard = [
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user.id}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"New Approval Request:\n\n"
                         f"User ID: {user.id}\n"
                         f"Username: @{user.username if user.username else 'None'}\n"
                         f"Name: {user.first_name} {user.last_name or ''}\n"
                         f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    reply_markup=reply_markup
                )
            except:
                pass
        
        await update.message.reply_text(
            "Your approval request has been sent to admin. Please wait for response."
        )

    async def show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        buttons = buttons_collection.find()
        
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

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        data = query.data
        
        if data == "check_join":
            await self.check_force_join_after_button(query, context)
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
            return WAITING_FOR_COMMENTS
        elif data.startswith("stats_"):
            button_id = data.replace("stats_", "")
            await self.show_button_stats(query, context, button_id)
        elif data == "add_channel":
            await query.message.reply_text("Please send the channel username or ID:")
            return WAITING_FOR_CHANNEL
        elif data == "remove_channel":
            await self.show_channels_to_remove(query, context)
        elif data == "add_button":
            await query.message.reply_text("Please send the name for the new button:")
            return WAITING_FOR_BUTTON_NAME
        elif data == "remove_button":
            await self.show_buttons_to_remove(query, context)
        elif data == "bot_on":
            settings_collection.update_one({}, {'$set': {'bot_status': True}})
            self.bot_status = True
            await query.message.reply_text("Bot turned ON successfully!")
            await self.show_admin_panel(query, context)
        elif data == "bot_off":
            settings_collection.update_one({}, {'$set': {'bot_status': False}})
            self.bot_status = False
            await query.message.reply_text("Bot turned OFF successfully!")
            await self.show_admin_panel(query, context)
        elif data == "set_over_message":
            await query.message.reply_text("Please send the new over message:")
            return WAITING_FOR_OVER_MESSAGE

    async def check_force_join_after_button(self, query, context):
        channels = list(channels_collection.find())
        user_id = query.from_user.id
        not_joined = []
        
        for channel in channels:
            try:
                member = await context.bot.get_chat_member(chat_id=channel['channel_id'], user_id=user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    not_joined.append(channel)
            except:
                not_joined.append(channel)
        
        if not_joined:
            keyboard = []
            for channel in not_joined:
                keyboard.append([InlineKeyboardButton(
                    f"Join {channel['channel_name']}", 
                    url=channel['channel_link']
                )])
            
            keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.edit_text(
                "You haven't joined all channels yet. Please join:",
                reply_markup=reply_markup
            )
        else:
            await self.request_approval_from_callback(query, context)

    async def request_approval_from_callback(self, query, context):
        user = query.from_user
        
        existing = pending_approvals_collection.find_one({'user_id': user.id})
        if existing:
            await query.message.edit_text("Your approval request is already pending.")
            return
        
        pending_approvals_collection.insert_one({
            'user_id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'date': datetime.now()
        })
        
        for admin_id in ADMIN_IDS:
            try:
                keyboard = [
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user.id}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user.id}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"New Approval Request:\n\n"
                         f"User ID: {user.id}\n"
                         f"Username: @{user.username if user.username else 'None'}\n"
                         f"Name: {user.first_name} {user.last_name or ''}\n"
                         f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    reply_markup=reply_markup
                )
            except:
                pass
        
        await query.message.edit_text(
            "Your approval request has been sent to admin. Please wait for response."
        )

    async def handle_approval(self, query, context):
        if query.from_user.id not in ADMIN_IDS:
            await query.message.reply_text("You are not authorized!")
            return
        
        data = query.data
        user_id = int(data.split('_')[1])
        
        if data.startswith("approve"):
            # Approve user
            users_collection.update_one(
                {'user_id': user_id},
                {'$set': {'approved': True, 'rejected': False}},
                upsert=True
            )
            pending_approvals_collection.delete_one({'user_id': user_id})
            
            # Notify user
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="Welcome To Comment Provider Bot By Zahid"
                )
            except:
                pass
            
            await query.message.edit_text(
                query.message.text + "\n\n✅ User approved successfully!"
            )
        else:
            # Reject user
            users_collection.update_one(
                {'user_id': user_id},
                {'$set': {'approved': False, 'rejected': True}},
                upsert=True
            )
            pending_approvals_collection.delete_one({'user_id': user_id})
            
            # Notify user
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="Sorry But Your Approval Has Been Rejected By Owner. "
                         "If You Have Any Issue Contact To @DTXZAHID"
                )
            except:
                pass
            
            await query.message.edit_text(
                query.message.text + "\n\n❌ User rejected!"
            )

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
        # Get next available comment
        comment = comments_collection.find_one_and_delete(
            {'button_id': button_id, 'used': False},
            sort=[('_id', 1)]
        )
        
        if comment:
            # Mark as used
            comments_collection.update_one(
                {'_id': comment['_id']},
                {'$set': {'used': True, 'used_by': query.from_user.id, 'used_date': datetime.now()}}
            )
            
            message_text = f"Here Is Your Comment Go And Do Review\n\n<code>{comment['comment']}</code>"
            
            await query.message.edit_text(
                message_text,
                parse_mode='HTML'
            )
        else:
            # No comments available
            over_message = settings_collection.find_one()['over_message']
            await query.message.edit_text(over_message)

    async def show_admin_panel(self, query, context):
        if query.from_user.id not in ADMIN_IDS:
            await query.message.reply_text("Unauthorized access!")
            return
        
        settings = settings_collection.find_one()
        bot_status = settings['bot_status']
        status_text = "ON ✅" if bot_status else "OFF ❌"
        
        keyboard = [
            [InlineKeyboardButton("📢 Manage Channels", callback_data="manage_channels")],
            [InlineKeyboardButton("🔘 Manage Buttons", callback_data="manage_buttons")],
            [InlineKeyboardButton("➕ Add Comments", callback_data="show_buttons_for_comments")],
            [InlineKeyboardButton("📊 View Stats", callback_data="view_stats")],
            [InlineKeyboardButton(f"🤖 Bot Status: {status_text}", callback_data="toggle_bot")],
            [InlineKeyboardButton("✏️ Set Over Message", callback_data="set_over_message")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_main")]
        ]
        
        # Split toggle bot button
        if bot_status:
            keyboard[3] = [InlineKeyboardButton("🤖 Turn Bot OFF", callback_data="bot_off")]
        else:
            keyboard[3] = [InlineKeyboardButton("🤖 Turn Bot ON", callback_data="bot_on")]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "⚙️ Admin Panel\n\n"
            f"Bot Status: {status_text}\n"
            "Select an option:",
            reply_markup=reply_markup
        )

    async def show_manage_channels(self, query, context):
        keyboard = [
            [InlineKeyboardButton("➕ Add Channel", callback_data="add_channel")],
            [InlineKeyboardButton("❌ Remove Channel", callback_data="remove_channel")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        channels = list(channels_collection.find())
        channel_list = "No channels added yet." if not channels else "\n".join([
            f"• {c['channel_name']} ({c['channel_id']})" for c in channels
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
        
        buttons = list(buttons_collection.find())
        button_list = "No buttons added yet." if not buttons else "\n".join([
            f"• {b['button_name']}" for b in buttons
        ])
        
        await query.message.edit_text(
            f"🔘 Manage Buttons\n\nCurrent Buttons:\n{button_list}\n\nSelect option:",
            reply_markup=reply_markup
        )

    async def show_buttons_for_comments(self, query, context):
        buttons = list(buttons_collection.find())
        
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
                button['button_name'], 
                callback_data=f"add_comments_{button['button_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Select button to add comments:",
            reply_markup=reply_markup
        )

    async def show_button_stats(self, query, context, button_id):
        button = buttons_collection.find_one({'button_id': button_id})
        total_comments = comments_collection.count_documents({'button_id': button_id})
        used_comments = comments_collection.count_documents({'button_id': button_id, 'used': True})
        available_comments = total_comments - used_comments
        
        stats_text = (
            f"📊 Stats for: {button['button_name']}\n\n"
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
        buttons = list(buttons_collection.find())
        
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
                f"{button['button_name']}", 
                callback_data=f"stats_{button['button_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Select button to view stats:",
            reply_markup=reply_markup
        )

    async def show_channels_to_remove(self, query, context):
        channels = list(channels_collection.find())
        
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
                f"Remove {channel['channel_name']}", 
                callback_data=f"remove_channel_{channel['channel_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="manage_channels")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Select channel to remove:",
            reply_markup=reply_markup
        )

    async def show_buttons_to_remove(self, query, context):
        buttons = list(buttons_collection.find())
        
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
                f"Remove {button['button_name']}", 
                callback_data=f"remove_button_{button['button_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="manage_buttons")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.edit_text(
            "Select button to remove:",
            reply_markup=reply_markup
        )

    async def show_main_menu_from_callback(self, query, context):
        await self.show_main_menu(query, context)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if 'current_button' in context.user_data:
            # Adding comments
            button_id = context.user_data['current_button']
            text = update.message.text
            
            # Split comments by comma
            comments = [c.strip() for c in text.split(',') if c.strip()]
            
            # Save comments
            for comment in comments:
                comments_collection.insert_one({
                    'button_id': button_id,
                    'comment': comment,
                    'used': False,
                    'added_date': datetime.now()
                })
            
            del context.user_data['current_button']
            
            await update.message.reply_text(
                f"✅ Added {len(comments)} comments successfully!\n\n"
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
            
            channels_collection.insert_one({
                'channel_id': channel_id,
                'channel_name': channel_input,
                'channel_link': channel_link
            })
            
            del context.user_data['channel_input']
            del context.user_data['waiting_for_channel_link']
            
            await update.message.reply_text("✅ Channel added successfully!")
            await self.show_admin_panel_from_message(update, context)
        
        elif context.user_data.get('waiting_for_button_name'):
            # Adding button
            button_name = update.message.text
            
            # Generate a unique ID
            button_id = str(datetime.timestamp()).replace('.', '')
            
            buttons_collection.insert_one({
                'button_id': button_id,
                'button_name': button_name
            })
            
            del context.user_data['waiting_for_button_name']
            
            await update.message.reply_text("✅ Button added successfully!")
            await self.show_admin_panel_from_message(update, context)
        
        elif context.user_data.get('waiting_for_over_message'):
            # Set over message
            over_message = update.message.text
            
            settings_collection.update_one(
                {},
                {'$set': {'over_message': over_message}}
            )
            self.over_message = over_message
            
            del context.user_data['waiting_for_over_message']
            
            await update.message.reply_text("✅ Over message updated successfully!")
            await self.show_admin_panel_from_message(update, context)
        
        else:
            # Check if user is approved
            user = update.effective_user
            user_data = users_collection.find_one({'user_id': user.id})
            
            if user_data and user_data.get('approved', False):
                await self.show_main_menu(update, context)
            else:
                await self.start(update, context)

    async def show_admin_panel_from_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    async def handle_channel_removal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data.startswith("remove_channel_"):
            channel_id = query.data.replace("remove_channel_", "")
            channels_collection.delete_one({'channel_id': channel_id})
            await query.message.edit_text("✅ Channel removed successfully!")
            await self.show_manage_channels(query, context)
        
        elif query.data.startswith("remove_button_"):
            button_id = query.data.replace("remove_button_", "")
            buttons_collection.delete_one({'button_id': button_id})
            # Also remove related comments
            comments_collection.delete_many({'button_id': button_id})
            await query.message.edit_text("✅ Button removed successfully!")
            await self.show_manage_buttons(query, context)
        
        elif query.data == "manage_channels":
            await self.show_manage_channels(query, context)
        
        elif query.data == "manage_buttons":
            await self.show_manage_buttons(query, context)
        
        elif query.data == "show_buttons_for_comments":
            await self.show_buttons_for_comments(query, context)
        
        elif query.data == "view_stats":
            await self.show_view_stats(query, context)
        
        elif query.data == "toggle_bot":
            settings = settings_collection.find_one()
            new_status = not settings['bot_status']
            settings_collection.update_one({}, {'$set': {'bot_status': new_status}})
            self.bot_status = new_status
            await query.message.edit_text(f"Bot turned {'ON' if new_status else 'OFF'}!")
            await self.show_admin_panel(query, context)
        
        elif query.data == "back_to_main":
            await self.show_main_menu_from_callback(query, context)

def main():
    """Start the bot."""
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    bot = CommentBot()
    
    # Add conversation handler for adding channels/buttons
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bot.button_callback, pattern="^(add_channel|add_button|set_over_message)$"),
            CallbackQueryHandler(bot.button_callback, pattern="^add_comments_"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)
        ],
        states={
            WAITING_FOR_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)],
            WAITING_FOR_BUTTON_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)],
            WAITING_FOR_COMMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)],
            WAITING_FOR_OVER_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)]
        },
        fallbacks=[CommandHandler('start', bot.start)]
    )
    
    # Add handlers
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CallbackQueryHandler(bot.button_callback, pattern="^(?!remove_(channel|button)_).*"))
    application.add_handler(CallbackQueryHandler(bot.handle_channel_removal, pattern="^(remove_(channel|button)_|manage_channels|manage_buttons|show_buttons_for_comments|view_stats|toggle_bot|back_to_main)$"))
    application.add_handler(conv_handler)
    
    # Start bot
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
