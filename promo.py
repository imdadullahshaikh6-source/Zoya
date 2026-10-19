import asyncio
import logging
import os
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked, PeerIdInvalid
from database import db  # Aapki database file se db import kiya

log = logging.getLogger("promo")

# .env se OWNER_ID uthane ka tarika
try:
    OWNER_ID = int(os.getenv("OWNER_ID", 0))
except ValueError:
    OWNER_ID = 0

# ===================================================================
# DATABASE SE USERS AUR GROUPS MANGWANE KA FUNCTION
# ===================================================================
async def get_all_users():
    users = []
    async for user in db.users.find({}, {"_id": 1}):
        users.append(user["_id"])
    return users

async def get_all_groups():
    groups = []
    async for chat in db.chats.find({"_id": {"$lt": 0}}, {"_id": 1}):
        groups.append(chat["_id"])
    return groups

# ===================================================================
# BACKGROUND BROADCAST ENGINE
# ===================================================================
async def broadcast_engine(client, message, target_type):
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
                await message.reply_to_message.forward(chat_id=target_id)
                success += 1
                await asyncio.sleep(0.1)
                
            except FloodWait as e:
                await asyncio.sleep(e.value)
                try:
                    await message.reply_to_message.forward(chat_id=target_id)
                    success += 1
                except:
                    failed += 1
            except (InputUserDeactivated, UserIsBlocked, PeerIdInvalid):
                failed += 1
            except Exception as e:
                log.error(f"Error forwarding to {target_id}: {e}")
                failed += 1

            if (success + failed) % 50 == 0:
                try:
                    await status_msg.edit(f"⏳ **Broadcasting...**\n\nDone: {success + failed}/{len(targets)}\n✅ Success: {success}\n❌ Failed: {failed}")
                except:
                    pass

        report = (
            f"✅ **Broadcast Completed!**\n\n"
            f"📊 **Total Targets:** {len(targets)}\n"
            f"✅ **Successfully Forwarded:** {success}\n"
            f"❌ **Failed (Blocked/Deleted):** {failed}\n"
            f"🎯 **Type:** {title}"
        )
        await status_msg.edit(report)

    except Exception as e:
        log.error(f"Broadcast Error: {e}")
        await message.reply_text(f"❌ **Broadcast Error:** {e}")

# ===================================================================
# COMMAND HANDLERS (Sirf Bot Owner ke liye)
# ===================================================================

@Client.on_message(filters.command(["promo-users", "promo_users", "promo-user"], prefixes=["/", "."]) & filters.user(OWNER_ID))
async def promo_users_handler(client, message):
    if not message.reply_to_message:
        return await message.reply_text("⚠️ Kisi message ko **reply** karke command dein.")
    asyncio.create_task(broadcast_engine(client, message, "users"))
    await message.reply_text("✅ **Users Broadcast Started in Background!**")

@Client.on_message(filters.command(["promo-gc", "promo_gc"], prefixes=["/", "."]) & filters.user(OWNER_ID))
async def promo_gc_handler(client, message):
    if not message.reply_to_message:
        return await message.reply_text("⚠️ Kisi message ko **reply** karke command dein.")
    asyncio.create_task(broadcast_engine(client, message, "gc"))
    await message.reply_text("✅ **Groups Broadcast Started in Background!**")

@Client.on_message(filters.command(["promo-all", "promo_all"], prefixes=["/", "."]) & filters.user(OWNER_ID))
async def promo_all_handler(client, message):
    if not message.reply_to_message:
        return await message.reply_text("⚠️ Kisi message ko **reply** karke command dein.")
    asyncio.create_task(broadcast_engine(client, message, "all"))
    await message.reply_text("✅ **All Broadcast Started in Background!**")
