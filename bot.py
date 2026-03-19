import logging
import os
import certifi
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from pymongo import MongoClient
import traceback

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
(CHANNEL_NAME, CHANNEL_LINK, BUTTON_NAME, COMMENTS, OVER_MESSAGE) = range(5)

# Environment variables
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '').split(',')))
MONGO_URI = os.environ.get('MONGO_URI')

# MongoDB connection
try:
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=30000)
    client.admin.command('ping')
    db = client['comment_bot']
    users = db['users']
    channels = db['channels']
    buttons = db['buttons']
    comments = db['comments']
    settings = db['settings']
    pending = db['pending_approvals']
    
    # Default settings
    if not settings.find_one():
        settings.insert_one({'bot_status': True, 'over_message': 'No more comments available.'})
    print("✅ MongoDB connected")
except Exception as e:
    print(f"❌ MongoDB failed: {e}")
    # Dummy collections to prevent crashes
    class Dummy:
        def find_one(self, *a, **k): return None
        def find(self, *a, **k): return []
        def insert_one(self, *a, **k): return None
        def update_one(self, *a, **k): return None
        def delete_one(self, *a, **k): return None
        def count_documents(self, *a, **k): return 0
    users = channels = buttons = comments = settings = pending = Dummy()

class Bot:
    def __init__(self):
        self.load_settings()
    
    def load_settings(self):
        s = settings.find_one()
        self.bot_on = s.get('bot_status', True) if s else True
        self.over_msg = s.get('over_message', 'No more comments available.') if s else 'No more comments available.'

    # ================== START & MAIN MENU ==================
    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id in ADMIN_IDS:
            return await self.admin_panel(update, ctx)
        
        if not self.bot_on:
            return await update.message.reply_text("No Apps Available For Comment")
        
        u = users.find_one({'user_id': user.id})
        if u and u.get('approved'):
            return await self.user_menu(update, ctx)
        elif u and u.get('rejected'):
            return await update.message.reply_text("Sorry, your approval was rejected. Contact @DTXZAHID")
        elif u and u.get('pending'):
            return await update.message.reply_text("Your approval is pending. Please wait.")
        
        # New user: check channels
        ch_list = list(channels.find())
        if not ch_list:
            return await self.ask_approval(update, ctx)
        
        not_joined = []
        for ch in ch_list:
            try:
                member = await ctx.bot.get_chat_member(chat_id=ch['channel_id'], user_id=user.id)
                if member.status not in ['member', 'administrator', 'creator']:
                    not_joined.append(ch)
            except:
                not_joined.append(ch)
        
        if not_joined:
            keyboard = [[InlineKeyboardButton(f"Join {ch['channel_name']}", url=ch['channel_link'])] for ch in not_joined]
            keyboard.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
            await update.message.reply_text("Please join these channels:", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await self.ask_approval(update, ctx)

    async def user_menu(self, update, ctx):
        btns = list(buttons.find())
        if not btns:
            return await update.message.reply_text("No apps available.")
        keyboard = [[InlineKeyboardButton(b['button_name'], callback_data=f"btn_{b['button_id']}")] for b in btns]
        await update.message.reply_text("Select an app:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def ask_approval(self, update, ctx):
        keyboard = [
            [InlineKeyboardButton("✅ Request Approval", callback_data="req_approval")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        await update.message.reply_text("You need admin approval. Send request?", reply_markup=InlineKeyboardMarkup(keyboard))

    # ================== APPROVAL HANDLING ==================
    async def request_approval(self, query, ctx):
        user = query.from_user
        if pending.find_one({'user_id': user.id}):
            return await query.message.edit_text("Request already pending.")
        if users.find_one({'user_id': user.id, 'approved': True}):
            return await query.message.edit_text("You're already approved! Send /start")
        
        # Save pending
        pending.insert_one({'user_id': user.id, 'username': user.username, 'first_name': user.first_name, 'date': datetime.now()})
        users.update_one({'user_id': user.id}, {'$set': {'pending': True}}, upsert=True)
        
        # Notify admins
        for aid in ADMIN_IDS:
            try:
                kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"app_{user.id}"),
                       InlineKeyboardButton("❌ Reject", callback_data=f"rej_{user.id}")]]
                await ctx.bot.send_message(aid, f"New approval request from {user.first_name} (@{user.username})", reply_markup=InlineKeyboardMarkup(kb))
            except:
                pass
        await query.message.edit_text("Request sent! You'll be notified once approved.")

    async def handle_approval(self, query, ctx):
        if query.from_user.id not in ADMIN_IDS:
            return await query.answer("Unauthorized")
        data = query.data
        uid = int(data.split('_')[1])
        if data.startswith('app'):
            users.update_one({'user_id': uid}, {'$set': {'approved': True, 'pending': False, 'rejected': False}}, upsert=True)
            pending.delete_one({'user_id': uid})
            try:
                await ctx.bot.send_message(uid, "✅ Welcome To Comment Provider Bot By Zahid! Send /start")
            except:
                pass
            await query.message.edit_text(query.message.text + "\n✅ Approved")
        else:
            users.update_one({'user_id': uid}, {'$set': {'approved': False, 'pending': False, 'rejected': True}}, upsert=True)
            pending.delete_one({'user_id': uid})
            try:
                await ctx.bot.send_message(uid, "❌ Your approval was rejected. Contact @DTXZAHID")
            except:
                pass
            await query.message.edit_text(query.message.text + "\n❌ Rejected")

    # ================== COMMENT SYSTEM ==================
    async def show_comment_confirm(self, query, btn_id):
        kb = [[InlineKeyboardButton("✅ I Agree", callback_data=f"agree_{btn_id}"),
               InlineKeyboardButton("❌ No", callback_data="back_main")]]
        await query.message.edit_text("Do you really want this app comment?\nNote: If you don't do 2-3 apps you may be banned.", reply_markup=InlineKeyboardMarkup(kb))

    async def give_comment(self, query, btn_id):
        # Atomic: find and mark used
        com = comments.find_one_and_delete({'button_id': btn_id, 'used': False}, sort=[('_id', 1)])
        if com:
            comments.update_one({'_id': com['_id']}, {'$set': {'used': True, 'used_by': query.from_user.id, 'used_date': datetime.now()}})
            await query.message.edit_text(f"Here is your comment:\n\n<code>{com['comment']}</code>", parse_mode='HTML')
        else:
            self.load_settings()
            await query.message.edit_text(self.over_msg)

    # ================== ADMIN PANEL ==================
    async def admin_panel(self, update, ctx, edit=False):
        s = settings.find_one() or {}
        status = "ON ✅" if s.get('bot_status', True) else "OFF ❌"
        kb = [
            [InlineKeyboardButton("🤖 Turn OFF" if s.get('bot_status') else "🤖 Turn ON", callback_data="toggle")],
            [InlineKeyboardButton("📢 Channels", callback_data="man_chan"), InlineKeyboardButton("🔘 Buttons", callback_data="man_btn")],
            [InlineKeyboardButton("➕ Add Comments", callback_data="add_com"), InlineKeyboardButton("📊 Stats", callback_data="stats")],
            [InlineKeyboardButton("✏️ Over Message", callback_data="set_msg"), InlineKeyboardButton("🔙 Main", callback_data="back_main")]
        ]
        text = f"⚙️ Admin Panel\nBot Status: {status}"
        if edit:
            await update.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

    # ================== CONVERSATIONS ==================
    async def add_channel_start(self, query, ctx):
        await query.message.reply_text("Send the channel username or ID:")
        return CHANNEL_NAME

    async def add_channel_name(self, update, ctx):
        ctx.user_data['chan_name'] = update.message.text
        await update.message.reply_text("Now send the invite link:")
        return CHANNEL_LINK

    async def add_channel_link(self, update, ctx):
        name = ctx.user_data.pop('chan_name')
        link = update.message.text
        chan_id = str(datetime.timestamp()).replace('.', '')
        channels.insert_one({'channel_id': chan_id, 'channel_name': name, 'channel_link': link})
        await update.message.reply_text("✅ Channel added!")
        await self.admin_panel(update, ctx)
        return ConversationHandler.END

    async def add_button_start(self, query, ctx):
        await query.message.reply_text("Send the button name:")
        return BUTTON_NAME

    async def add_button_name(self, update, ctx):
        name = update.message.text
        btn_id = str(datetime.timestamp()).replace('.', '')
        buttons.insert_one({'button_id': btn_id, 'button_name': name})
        await update.message.reply_text("✅ Button added!")
        await self.admin_panel(update, ctx)
        return ConversationHandler.END

    async def add_comments_start(self, query, ctx):
        btns = list(buttons.find())
        if not btns:
            await query.message.edit_text("No buttons yet.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return ConversationHandler.END
        kb = [[InlineKeyboardButton(b['button_name'], callback_data=f"selbtn_{b['button_id']}")] for b in btns]
        kb.append([InlineKeyboardButton("Back", callback_data="admin")])
        await query.message.edit_text("Select button:", reply_markup=InlineKeyboardMarkup(kb))
        return COMMENTS

    async def select_button_for_comments(self, query, ctx):
        btn_id = query.data.replace("selbtn_", "")
        ctx.user_data['com_btn'] = btn_id
        await query.message.edit_text("Send comments separated by commas:\nExample: Great!, Awesome!, Love it!")
        return COMMENTS

    async def save_comments(self, update, ctx):
        btn_id = ctx.user_data.pop('com_btn')
        text = update.message.text
        coms = [c.strip() for c in text.split(',') if c.strip()]
        for c in coms:
            comments.insert_one({'button_id': btn_id, 'comment': c, 'used': False, 'added_date': datetime.now()})
        await update.message.reply_text(f"✅ Added {len(coms)} comments!")
        await self.admin_panel(update, ctx)
        return ConversationHandler.END

    async def set_over_msg_start(self, query, ctx):
        await query.message.reply_text("Send the new 'out of comments' message:")
        return OVER_MESSAGE

    async def save_over_msg(self, update, ctx):
        msg = update.message.text
        settings.update_one({}, {'$set': {'over_message': msg}}, upsert=True)
        self.over_msg = msg
        await update.message.reply_text("✅ Over message updated!")
        await self.admin_panel(update, ctx)
        return ConversationHandler.END

    # ================== BUTTON HANDLER ==================
    async def callback_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        data = q.data

        # Public callbacks
        if data == "check_join":
            return await self.start(q, ctx)  # Re-check
        if data == "req_approval":
            return await self.request_approval(q, ctx)
        if data == "cancel":
            return await q.message.edit_text("Cancelled.")
        if data.startswith("btn_"):
            return await self.show_comment_confirm(q, data[4:])
        if data.startswith("agree_"):
            return await self.give_comment(q, data[6:])
        if data == "back_main":
            return await self.user_menu(q, ctx)

        # Admin only below
        if q.from_user.id not in ADMIN_IDS:
            return await q.message.reply_text("Unauthorized")

        if data == "admin":
            return await self.admin_panel(q, ctx, edit=True)
        if data == "toggle":
            s = settings.find_one() or {}
            new = not s.get('bot_status', True)
            settings.update_one({}, {'$set': {'bot_status': new}}, upsert=True)
            self.bot_on = new
            await q.message.edit_text(f"Bot turned {'ON' if new else 'OFF'}!")
            return await self.admin_panel(q, ctx, edit=True)

        if data.startswith("app_") or data.startswith("rej_"):
            return await self.handle_approval(q, ctx)

        # Manage channels
        if data == "man_chan":
            chans = list(channels.find())
            txt = "Channels:\n" + ("\n".join([f"• {c['channel_name']}" for c in chans]) if chans else "None")
            kb = [[InlineKeyboardButton("➕ Add", callback_data="add_chan")],
                  [InlineKeyboardButton("❌ Remove", callback_data="rem_chan")],
                  [InlineKeyboardButton("Back", callback_data="admin")]]
            await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb))
        if data == "add_chan":
            return await self.add_channel_start(q, ctx)
        if data == "rem_chan":
            chans = list(channels.find())
            if not chans:
                await q.message.edit_text("No channels to remove.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="man_chan")]]))
                return
            kb = [[InlineKeyboardButton(f"Remove {c['channel_name']}", callback_data=f"delchan_{c['channel_id']}")] for c in chans]
            kb.append([InlineKeyboardButton("Back", callback_data="man_chan")])
            await q.message.edit_text("Select channel to remove:", reply_markup=InlineKeyboardMarkup(kb))
        if data.startswith("delchan_"):
            cid = data[8:]
            channels.delete_one({'channel_id': cid})
            await q.message.edit_text("Channel removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="man_chan")]]))

        # Manage buttons
        if data == "man_btn":
            btns = list(buttons.find())
            txt = "Buttons:\n" + ("\n".join([f"• {b['button_name']}" for b in btns]) if btns else "None")
            kb = [[InlineKeyboardButton("➕ Add", callback_data="add_btn")],
                  [InlineKeyboardButton("❌ Remove", callback_data="rem_btn")],
                  [InlineKeyboardButton("Back", callback_data="admin")]]
            await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb))
        if data == "add_btn":
            return await self.add_button_start(q, ctx)
        if data == "rem_btn":
            btns = list(buttons.find())
            if not btns:
                await q.message.edit_text("No buttons to remove.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="man_btn")]]))
                return
            kb = [[InlineKeyboardButton(f"Remove {b['button_name']}", callback_data=f"delbtn_{b['button_id']}")] for b in btns]
            kb.append([InlineKeyboardButton("Back", callback_data="man_btn")])
            await q.message.edit_text("Select button to remove:", reply_markup=InlineKeyboardMarkup(kb))
        if data.startswith("delbtn_"):
            bid = data[7:]
            buttons.delete_one({'button_id': bid})
            comments.delete_many({'button_id': bid})
            await q.message.edit_text("Button removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="man_btn")]]))

        # Add comments
        if data == "add_com":
            return await self.add_comments_start(q, ctx)
        if data.startswith("selbtn_"):
            return await self.select_button_for_comments(q, ctx)

        # Stats
        if data == "stats":
            btns = list(buttons.find())
            if not btns:
                await q.message.edit_text("No buttons.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
                return
            txt = "Select button for stats:"
            kb = [[InlineKeyboardButton(b['button_name'], callback_data=f"stat_{b['button_id']}")] for b in btns]
            kb.append([InlineKeyboardButton("Back", callback_data="admin")])
            await q.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb))
        if data.startswith("stat_"):
            bid = data[5:]
            total = comments.count_documents({'button_id': bid})
            used = comments.count_documents({'button_id': bid, 'used': True})
            btn = buttons.find_one({'button_id': bid})
            name = btn['button_name'] if btn else "?"
            await q.message.edit_text(f"📊 {name}\nTotal: {total}\nUsed: {used}\nLeft: {total-used}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="stats")]]))

        # Set over message
        if data == "set_msg":
            return await self.set_over_msg_start(q, ctx)

    # ================== CANCEL ==================
    async def cancel(self, update, ctx):
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    bot = Bot()

    # Conversation handlers
    chan_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bot.add_channel_start, pattern="^add_chan$")],
        states={CHANNEL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.add_channel_name)],
                CHANNEL_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.add_channel_link)]},
        fallbacks=[CommandHandler('cancel', bot.cancel)]
    )
    btn_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bot.add_button_start, pattern="^add_btn$")],
        states={BUTTON_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.add_button_name)]},
        fallbacks=[CommandHandler('cancel', bot.cancel)]
    )
    com_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bot.add_comments_start, pattern="^add_com$")],
        states={COMMENTS: [CallbackQueryHandler(bot.select_button_for_comments, pattern="^selbtn_"),
                           MessageHandler(filters.TEXT & ~filters.COMMAND, bot.save_comments)]},
        fallbacks=[CommandHandler('cancel', bot.cancel)]
    )
    msg_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bot.set_over_msg_start, pattern="^set_msg$")],
        states={OVER_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.save_over_msg)]},
        fallbacks=[CommandHandler('cancel', bot.cancel)]
    )

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("admin", lambda u,c: bot.admin_panel(u,c) if u.effective_user.id in ADMIN_IDS else u.message.reply_text("⛔ Unauthorized")))
    app.add_handler(CallbackQueryHandler(bot.callback_handler))
    app.add_handler(chan_conv)
    app.add_handler(btn_conv)
    app.add_handler(com_conv)
    app.add_handler(msg_conv)

    print("🤖 Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
