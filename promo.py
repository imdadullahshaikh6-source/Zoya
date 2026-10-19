import asyncio
import logging
import re
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from telegram.error import Forbidden, BadRequest, RetryAfter
import database as dbase

log = logging.getLogger("promo")

# Aapki Telegram ID
OWNER_ID = 8373739674 

# ===================================================================
# DATABASE SE USERS AUR GROUPS MANGWANE KA FUNCTION
# ===================================================================
async def get_all_users():
    users = []
    async for user in dbase.db.users.find({}, {"_id": 1}):
        users.append(user["_id"])
    return users

async def get_all_groups():
    groups = []
    async for chat in dbase.db.chats.find({"_id": {"$lt": 0}}, {"_id": 1}):
        groups.append(chat["_id"])
    return groups

# ===================================================================
# BACKGROUND BROADCAST ENGINE
# ===================================================================
async def broadcast_engine(update: Update, context: ContextTypes.DEFAULT_TYPE, target_type: str):
    message = update.effective_message
    try:
        if target_type == "users":
            targets = await get_all_users()
            title = "Users (DMs)"
        elif target_type == "gc":
            targets = await get_all_groups()
            title = "Groups"
        else:
            users = await get_all_users()
            groups = await get_all_groups()
            targets = users + groups
            title = "All (Users + Groups)"

        if not targets:
            return await message.reply_text("❌ Database mein koi target nahi mila.")

        status_msg = await message.reply_text(f"🚀 **Broadcasting Started** to {len(targets)} {title}...")
        
        success = 0
        failed = 0

        for target_id in targets:
            try:
                # Forwarding the exact message
                await message.reply_to_message.forward(chat_id=target_id)
                success += 1
                await asyncio.sleep(0.1) # Delay to prevent flood limits
                
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await message.reply_to_message.forward(chat_id=target_id)
                    success += 1
                except:
                    failed += 1
            except (Forbidden, BadRequest):
                # User blocked bot or invalid chat
                failed += 1
            except Exception as e:
                log.error(f"Error forwarding to {target_id}: {e}")
                failed += 1

            # Update status every 50 messages
            if (success + failed) % 50 == 0:
                try:
                    await status_msg.edit_text(f"⏳ **Broadcasting...**\n\nDone: {success + failed}/{len(targets)}\n✅ Success: {success}\n❌ Failed: {failed}")
                except:
                    pass

        # Final report
        report = (
            f"✅ **Broadcast Completed!**\n\n"
            f"📊 **Total Targets:** {len(targets)}\n"
            f"✅ **Successfully Forwarded:** {success}\n"
            f"❌ **Failed (Blocked/Deleted):** {failed}\n"
            f"🎯 **Type:** {title}"
        )
        await status_msg.edit_text(report)

    except Exception as e:
        log.error(f"Broadcast Error: {e}")
        await message.reply_text(f"❌ **Broadcast Error:** {e}")

# ===================================================================
# COMMAND HANDLERS
# ===================================================================
async def promo_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("⚠️ Kisi message ko **reply** karke command dein.")
    
    await update.message.reply_text("✅ **Users Broadcast Started in Background!**")
    asyncio.create_task(broadcast_engine(update, context, "users"))

async def promo_gc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("⚠️ Kisi message ko **reply** karke command dein.")
    
    await update.message.reply_text("✅ **Groups Broadcast Started in Background!**")
    asyncio.create_task(broadcast_engine(update, context, "gc"))

async def promo_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("⚠️ Kisi message ko **reply** karke command dein.")
    
    await update.message.reply_text("✅ **All Broadcast Started in Background!**")
    asyncio.create_task(broadcast_engine(update, context, "all"))

# ===================================================================
# REGISTER FUNCTION
# ===================================================================
def register(app: Application):
    owner_filter = filters.User(OWNER_ID)
    
    # Regex use kiya taaki /promo-users aur .promo-user dono kaam kare
    users_pattern = re.compile(r"^[./]promo-users?$", re.IGNORECASE)
    gc_pattern = re.compile(r"^[./]promo-gc$", re.IGNORECASE)
    all_pattern = re.compile(r"^[./]promo-all$", re.IGNORECASE)

    app.add_handler(MessageHandler(filters.Regex(users_pattern) & owner_filter, promo_users))
    app.add_handler(MessageHandler(filters.Regex(gc_pattern) & owner_filter, promo_gc))
    app.add_handler(MessageHandler(filters.Regex(all_pattern) & owner_filter, promo_all))
