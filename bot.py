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

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '').split(',')))
MONGO_URI = os.environ.get('MONGO_URI')

# Conversation states for admin actions
WAITING_USER_ID = 1

# ---------- MongoDB Connection ----------
connected = False
try:
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=30000)
    client.admin.command('ping')
    connected = True
    print("✅ MongoDB connected")
except Exception as e:
    try:
        client = MongoClient(MONGO_URI, tlsAllowInvalidCertificates=True, serverSelectionTimeoutMS=30000)
        client.admin.command('ping')
        connected = True
        print("✅ MongoDB connected (fallback)")
    except Exception as e2:
        print(f"❌ MongoDB failed: {e2}")

if connected:
    db = client['comment_bot']
    users = db['users']
    buttons = db['buttons']
    comments = db['comments']
    settings = db['settings']
    pending = db['pending']
    if not settings.find_one():
        settings.insert_one({'bot_status': True, 'over_message': 'No more comments available for this app.'})
else:
    class Dummy:
        def find_one(self,*a,**k): return None
        def find(self,*a,**k): return []
        def insert_one(self,*a,**k): raise Exception("Database offline")
        def update_one(self,*a,**k): raise Exception("Database offline")
        def delete_one(self,*a,**k): raise Exception("Database offline")
        def count_documents(self,*a,**k): return 0
    users = buttons = comments = settings = pending = Dummy()

# ---------- Bot Class ----------
class Bot:
    def __init__(self):
        self.load_settings()

    def load_settings(self):
        if connected:
            s = settings.find_one()
            self.bot_on = s.get('bot_status', True) if s else True
            self.over_msg = s.get('over_message', 'No more comments available for this app.') if s else 'No more comments available for this app.'
        else:
            self.bot_on = True
            self.over_msg = 'No more comments available for this app.'

    # ========== START ==========
    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        uid = user.id

        if uid in ADMIN_IDS:
            return await self.admin_panel(update, ctx)

        if not self.bot_on:
            return await update.message.reply_text("❌ No Apps Available For Comment")

        u = users.find_one({'user_id': uid}) if connected else None
        if u and u.get('approved'):
            btns = list(buttons.find()) if connected else []
            if not btns:
                return await update.message.reply_text("📭 No apps available yet.")
            keyboard = [[InlineKeyboardButton(b['button_name'], callback_data=f"btn_{b['button_id']}")] for b in btns]
            await update.message.reply_text(
                "🌟 Welcome To Comment Provider Bot By Zahid\n\nPlease select an app:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        elif u and u.get('rejected'):
            await update.message.reply_text("❌ Sorry, your approval was rejected. Contact @DTXZAHID")
        elif u and u.get('pending'):
            await update.message.reply_text("⏳ Your approval is still pending. Please wait.")
        else:
            await self.request_approval_prompt(update, ctx)

    async def request_approval_prompt(self, update, ctx):
        keyboard = [
            [InlineKeyboardButton("✅ Request Approval", callback_data="ask_approval")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
        await update.message.reply_text(
            "🔐 You need admin approval to use this bot.\n\nDo you want to send a request?",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ========== APPROVAL ==========
    async def ask_approval(self, query, ctx):
        if not connected:
            await query.message.edit_text("❌ Database offline. Cannot request approval.")
            return
        user = query.from_user
        uid = user.id
        if pending.find_one({'user_id': uid}):
            await query.message.edit_text("⏳ You already have a pending request. Please wait.")
            return
        if users.find_one({'user_id': uid, 'approved': True}):
            await query.message.edit_text("✅ You are already approved! Send /start")
            return
        pending.insert_one({'user_id': uid, 'username': user.username, 'first_name': user.first_name, 'date': datetime.now()})
        users.update_one({'user_id': uid}, {'$set': {'pending': True}}, upsert=True)
        for admin in ADMIN_IDS:
            try:
                kb = [[InlineKeyboardButton("✅ Approve", callback_data=f"app_{uid}"),
                       InlineKeyboardButton("❌ Reject", callback_data=f"rej_{uid}")]]
                await ctx.bot.send_message(admin, f"🔔 New approval request from {user.first_name} (@{user.username})", reply_markup=InlineKeyboardMarkup(kb))
            except:
                pass
        await query.message.edit_text("✅ Request sent! You'll be notified once approved.")

    async def handle_approval(self, query, ctx):
        if query.from_user.id not in ADMIN_IDS:
            return await query.answer("⛔ Unauthorized")
        data = query.data
        uid = int(data.split('_')[1])
        if data.startswith('app'):
            users.update_one({'user_id': uid}, {'$set': {'approved': True, 'pending': False, 'rejected': False}}, upsert=True)
            pending.delete_one({'user_id': uid})
            try:
                await ctx.bot.send_message(uid, "✅ Welcome! You are approved. Send /start to use the bot.")
            except:
                pass
            await query.message.edit_text(query.message.text + "\n\n✅ Approved")
        else:
            users.update_one({'user_id': uid}, {'$set': {'approved': False, 'pending': False, 'rejected': True}}, upsert=True)
            pending.delete_one({'user_id': uid})
            try:
                await ctx.bot.send_message(uid, "❌ Your request was rejected. Contact @DTXZAHID")
            except:
                pass
            await query.message.edit_text(query.message.text + "\n\n❌ Rejected")

    # ========== ADMIN: Allow User ==========
    async def allow_user_start(self, query, ctx):
        ctx.user_data['action'] = 'allow_user'
        await query.message.reply_text("📝 Send the user ID of the user you want to allow:")
        return WAITING_USER_ID

    async def allow_user_id(self, update, ctx):
        try:
            uid = int(update.message.text)
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Please send a numeric ID.")
            return WAITING_USER_ID
        users.update_one({'user_id': uid}, {'$set': {'approved': True, 'rejected': False, 'pending': False}}, upsert=True)
        pending.delete_one({'user_id': uid})
        try:
            await ctx.bot.send_message(uid, "✅ You have been allowed to use the bot. Send /start to begin.")
        except:
            pass
        await update.message.reply_text(f"✅ User {uid} has been allowed.")
        ctx.user_data.pop('action', None)
        await self.admin_panel(update, ctx)
        return ConversationHandler.END

    # ========== ADMIN: Ban User ==========
    async def ban_user_start(self, query, ctx):
        ctx.user_data['action'] = 'ban_user'
        await query.message.reply_text("📝 Send the user ID of the user you want to ban:")
        return WAITING_USER_ID

    async def ban_user_id(self, update, ctx):
        try:
            uid = int(update.message.text)
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Please send a numeric ID.")
            return WAITING_USER_ID
        users.update_one({'user_id': uid}, {'$set': {'approved': False, 'rejected': True, 'pending': False}}, upsert=True)
        pending.delete_one({'user_id': uid})
        try:
            await ctx.bot.send_message(uid, "❌ You have been banned from using this bot. Contact @DTXZAHID")
        except:
            pass
        await update.message.reply_text(f"✅ User {uid} has been banned.")
        ctx.user_data.pop('action', None)
        await self.admin_panel(update, ctx)
        return ConversationHandler.END

    # ========== ADMIN: Comment Users ==========
    async def menu_comment_users(self, query):
        if not connected:
            await query.message.edit_text("❌ Database offline.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return
        btns = list(buttons.find())
        if not btns:
            await query.message.edit_text("No buttons yet.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return
        kb = [[InlineKeyboardButton(b['button_name'], callback_data=f"show_users_{b['button_id']}")] for b in btns]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin")])
        await query.message.edit_text("Select an app to see which users took comments:", reply_markup=InlineKeyboardMarkup(kb))

    async def show_comment_users(self, query, btn_id):
        btn = buttons.find_one({'button_id': btn_id})
        if not btn:
            await query.message.edit_text("Button not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu_comment_users")]]))
            return
        # Get all used comments for this button, sorted by used_date (most recent last)
        used_comments = list(comments.find({'button_id': btn_id, 'used': True}).sort('used_date', 1))
        if not used_comments:
            await query.message.edit_text(f"No users have taken comments for {btn['button_name']} yet.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu_comment_users")]]))
            return
        # Build message
        msg = f"📝 Users who took comments for *{btn['button_name']}*:\n\n"
        for idx, c in enumerate(used_comments, 1):
            uid = c.get('used_by')
            name = c.get('user_name', 'Unknown')
            username = c.get('user_username', '')
            username_str = f"@{username}" if username else "No username"
            msg += f"{idx}. {name} ({username_str}) – ID: `{uid}`\n"
            # Avoid message too long – if exceeds 4000 chars, truncate and add note
            if len(msg) > 3800:
                msg += "\n... (list truncated)"
                break
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_comment_users")]])
        await query.message.edit_text(msg, parse_mode='Markdown', reply_markup=reply_markup)

    # ========== COMMENT FLOW ==========
    async def confirm_comment(self, query, btn_id):
        kb = [
            [InlineKeyboardButton("✅ I Agree", callback_data=f"agree_{btn_id}")],
            [InlineKeyboardButton("❌ No", callback_data="main_menu")]
        ]
        await query.message.edit_text(
            "⚠️ Do you really want this app comment?\n"
            "Note: If you don't do 2-3 apps you may be banned.",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    async def give_comment(self, query, btn_id):
        if not connected:
            await query.message.edit_text("❌ Database offline.")
            return
        uid = query.from_user.id

        # Check if user already got a comment for this button
        already = comments.find_one({'button_id': btn_id, 'used': True, 'used_by': uid})
        if already:
            await query.message.edit_text(
                "🤷‍♂️ Already Given A Comment If You Want More Then Dm @DTXZAHID",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]])
            )
            return

        com = comments.find_one_and_delete({'button_id': btn_id, 'used': False}, sort=[('_id', 1)])
        if com:
            # Save user info in the comment document for later reference
            user = query.from_user
            comments.update_one({'_id': com['_id']}, {'$set': {
                'used': True,
                'used_by': uid,
                'used_date': datetime.now(),
                'user_name': user.first_name,
                'user_username': user.username
            }})
            await query.message.edit_text(
                f"✅ Here is your comment – tap and hold to copy:\n\n<code>{com['comment']}</code>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]])
            )
        else:
            self.load_settings()
            await query.message.edit_text(
                f"😕 {self.over_msg}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]])
            )

    # ========== ADMIN PANEL ==========
    async def admin_panel(self, update, ctx, edit=False):
        if not connected:
            text = "⚠️ Database offline. Admin panel limited."
            kb = [[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]
        else:
            s = settings.find_one() or {}
            status = "ON ✅" if s.get('bot_status', True) else "OFF ❌"
            kb = [
                [InlineKeyboardButton(f"🤖 Turn {'OFF' if s.get('bot_status') else 'ON'}", callback_data="toggle")],
                [InlineKeyboardButton("🔘 Buttons", callback_data="menu_buttons")],
                [InlineKeyboardButton("➕ Add Comments", callback_data="menu_add_comments"), InlineKeyboardButton("📊 Stats", callback_data="menu_stats")],
                [InlineKeyboardButton("✏️ Over Message", callback_data="menu_overmsg")],
                [InlineKeyboardButton("👥 Allow User", callback_data="allow_user"), InlineKeyboardButton("🚫 Ban User", callback_data="ban_user")],
                [InlineKeyboardButton("📝 Comment Users", callback_data="menu_comment_users")],
                [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
            ]
            text = f"⚙️ Admin Panel\n\nBot Status: {status}"
        if edit:
            await update.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

    # ========== BUTTONS ==========
    async def menu_buttons(self, query):
        if not connected:
            await query.message.edit_text("❌ Database offline.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return
        btns = list(buttons.find())
        txt = "🔘 Buttons:\n" + ("\n".join([f"• {b['button_name']}" for b in btns]) if btns else "No buttons.")
        kb = [
            [InlineKeyboardButton("➕ Add", callback_data="add_button")],
            [InlineKeyboardButton("❌ Remove", callback_data="remove_button")],
            [InlineKeyboardButton("🔙 Back", callback_data="admin")]
        ]
        await query.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb))

    async def add_button_start(self, query, ctx):
        ctx.user_data['action'] = 'add_button'
        await query.message.reply_text("📝 Send the button name:")

    async def remove_button(self, query):
        if not connected:
            await query.message.edit_text("❌ Database offline.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return
        btns = list(buttons.find())
        if not btns:
            await query.message.edit_text("No buttons to remove.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu_buttons")]]))
            return
        kb = [[InlineKeyboardButton(f"❌ Remove {b['button_name']}", callback_data=f"delbtn_{b['button_id']}")] for b in btns]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu_buttons")])
        await query.message.edit_text("Select button to remove:", reply_markup=InlineKeyboardMarkup(kb))

    async def delete_button(self, query, btn_id):
        buttons.delete_one({'button_id': btn_id})
        comments.delete_many({'button_id': btn_id})
        await query.message.edit_text("✅ Button removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_buttons")]]))

    # ========== ADD COMMENTS ==========
    async def menu_add_comments(self, query):
        if not connected:
            await query.message.edit_text("❌ Database offline.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return
        btns = list(buttons.find())
        if not btns:
            await query.message.edit_text("No buttons yet. Add one first.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return
        kb = [[InlineKeyboardButton(b['button_name'], callback_data=f"selbtn_{b['button_id']}")] for b in btns]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin")])
        await query.message.edit_text("Select button to add comments to:", reply_markup=InlineKeyboardMarkup(kb))

    async def select_button_for_comments(self, query, ctx):
        ctx.user_data['com_btn'] = query.data.replace("selbtn_", "")
        ctx.user_data['action'] = 'add_comments'
        await query.message.reply_text(
            "📝 Send comments separated by commas:\n"
            "Example: Great app!, Awesome!, Love it!"
        )

    # ========== STATS ==========
    async def menu_stats(self, query):
        if not connected:
            await query.message.edit_text("❌ Database offline.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return
        btns = list(buttons.find())
        if not btns:
            await query.message.edit_text("No buttons.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return
        kb = [[InlineKeyboardButton(b['button_name'], callback_data=f"stat_{b['button_id']}")] for b in btns]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin")])
        await query.message.edit_text("Select button for stats:", reply_markup=InlineKeyboardMarkup(kb))

    async def show_stats(self, query, btn_id):
        total = comments.count_documents({'button_id': btn_id})
        used = comments.count_documents({'button_id': btn_id, 'used': True})
        btn = buttons.find_one({'button_id': btn_id})
        name = btn['button_name'] if btn else "?"
        await query.message.edit_text(
            f"📊 Stats for: {name}\n\n"
            f"Total Comments: {total}\n"
            f"Used Comments: {used}\n"
            f"Remaining: {total - used}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_stats")]])
        )

    # ========== OVER MESSAGE ==========
    async def menu_overmsg(self, query, ctx):
        ctx.user_data['action'] = 'over_msg'
        await query.message.reply_text("📝 Send the new 'out of comments' message:")

    # ========== TOGGLE BOT ==========
    async def toggle_bot(self, query):
        if not connected:
            await query.message.edit_text("❌ Database offline.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="admin")]]))
            return
        s = settings.find_one() or {}
        new = not s.get('bot_status', True)
        settings.update_one({}, {'$set': {'bot_status': new}}, upsert=True)
        self.bot_on = new
        await query.message.edit_text(f"✅ Bot turned {'ON' if new else 'OFF'}!")
        await self.admin_panel(query, None, edit=True)

    # ========== MESSAGE HANDLER ==========
    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text
        action = ctx.user_data.get('action')

        if user_id not in ADMIN_IDS:
            await self.start(update, ctx)
            return

        if action == 'add_button':
            if not connected:
                await update.message.reply_text("❌ Database offline.")
                ctx.user_data.pop('action', None)
                await self.admin_panel(update, ctx)
                return
            btn_id = str(datetime.now().timestamp()).replace('.', '')
            buttons.insert_one({'button_id': btn_id, 'button_name': text})
            await update.message.reply_text("✅ Button added!")
            ctx.user_data.pop('action', None)
            await self.admin_panel(update, ctx)
        elif action == 'add_comments':
            if not connected:
                await update.message.reply_text("❌ Database offline.")
                ctx.user_data.pop('action', None)
                ctx.user_data.pop('com_btn', None)
                await self.admin_panel(update, ctx)
                return
            btn_id = ctx.user_data.get('com_btn')
            coms = [c.strip() for c in text.split(',') if c.strip()]
            for c in coms:
                comments.insert_one({'button_id': btn_id, 'comment': c, 'used': False, 'added_date': datetime.now()})
            await update.message.reply_text(f"✅ Added {len(coms)} comments!")
            ctx.user_data.pop('action', None)
            ctx.user_data.pop('com_btn', None)
            await self.admin_panel(update, ctx)
        elif action == 'over_msg':
            if not connected:
                await update.message.reply_text("❌ Database offline.")
                ctx.user_data.pop('action', None)
                await self.admin_panel(update, ctx)
                return
            settings.update_one({}, {'$set': {'over_message': text}}, upsert=True)
            self.over_msg = text
            await update.message.reply_text("✅ Over message updated!")
            ctx.user_data.pop('action', None)
            await self.admin_panel(update, ctx)
        else:
            await self.start(update, ctx)

    # ========== CALLBACK HANDLER ==========
    async def callback_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        data = q.data

        # Public callbacks
        if data == "ask_approval":
            await self.ask_approval(q, ctx)
            return
        if data == "cancel":
            await q.message.edit_text("❌ Cancelled.")
            return
        if data.startswith("btn_"):
            await self.confirm_comment(q, data[4:])
            return
        if data.startswith("agree_"):
            await self.give_comment(q, data[6:])
            return
        if data == "main_menu":
            if not connected:
                await q.message.edit_text("❌ Database offline.")
                return
            btns = list(buttons.find())
            if not btns:
                await q.message.edit_text("📭 No apps available yet.")
                return
            kb = [[InlineKeyboardButton(b['button_name'], callback_data=f"btn_{b['button_id']}")] for b in btns]
            await q.message.edit_text(
                "🌟 Welcome To Comment Provider Bot By Zahid\n\nPlease select an app:",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        # Admin only beyond
        if q.from_user.id not in ADMIN_IDS:
            await q.message.reply_text("⛔ Unauthorized")
            return

        if data == "admin":
            await self.admin_panel(q, ctx, edit=True)
            return
        if data == "toggle":
            await self.toggle_bot(q)
            return
        if data.startswith("app_") or data.startswith("rej_"):
            await self.handle_approval(q, ctx)
            return

        # Allow/Ban user
        if data == "allow_user":
            await self.allow_user_start(q, ctx)
            return
        if data == "ban_user":
            await self.ban_user_start(q, ctx)
            return

        # Comment Users
        if data == "menu_comment_users":
            await self.menu_comment_users(q)
            return
        if data.startswith("show_users_"):
            btn_id = data[11:]  # after "show_users_"
            await self.show_comment_users(q, btn_id)
            return

        # Buttons
        if data == "menu_buttons":
            await self.menu_buttons(q)
            return
        if data == "add_button":
            await self.add_button_start(q, ctx)
            return
        if data == "remove_button":
            await self.remove_button(q)
            return
        if data.startswith("delbtn_"):
            await self.delete_button(q, data[7:])
            return

        # Add comments
        if data == "menu_add_comments":
            await self.menu_add_comments(q)
            return
        if data.startswith("selbtn_"):
            await self.select_button_for_comments(q, ctx)
            return

        # Stats
        if data == "menu_stats":
            await self.menu_stats(q)
            return
        if data.startswith("stat_"):
            await self.show_stats(q, data[5:])
            return

        # Over message
        if data == "menu_overmsg":
            await self.menu_overmsg(q, ctx)
            return

# ========== CONVERSATION HANDLERS ==========
async def cancel_conversation(update, ctx):
    await update.message.reply_text("❌ Operation cancelled.")
    ctx.user_data.pop('action', None)
    return ConversationHandler.END

# ========== MAIN ==========
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    bot = Bot()

    # Conversation handlers for allow/ban
    allow_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bot.allow_user_start, pattern="^allow_user$")],
        states={
            WAITING_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.allow_user_id)]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)]
    )
    ban_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(bot.ban_user_start, pattern="^ban_user$")],
        states={
            WAITING_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.ban_user_id)]
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)]
    )

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("admin", bot.admin_panel))
    app.add_handler(CallbackQueryHandler(bot.callback_handler))
    app.add_handler(allow_conv)
    app.add_handler(ban_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
