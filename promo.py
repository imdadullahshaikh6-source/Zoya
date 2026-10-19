import asyncio
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked, PeerIdInvalid
from config import OWNER_ID  # Apne config se OWNER_ID import karein

# ===================================================================
# DATABASE FUNCTIONS (Inhe apne database ke hisaab se change karein)
# ===================================================================
# Agar aapke paas yeh functions pehle se hain toh inhe hata dein, 
# warna apne database (MongoDB) ke hisaab se inhe likhein.

async def get_all_users():
    # Yahan apne database se saare users ki IDs ki list return karein
    # Example: return [12345678, 98765432, ...]
    return [] 

async def get_all_groups():
    # Yahan apne database se saare groups ki IDs ki list return karein
    # Example: return [-10012345678, -10098765432, ...]
    return []

# ===================================================================
# BACKGROUND BROADCAST ENGINE
# ===================================================================
async def broadcast_engine(client, message, target_type):
    try:
        # Targets (Users ya Groups) fetch karna
        if target_type == "users":
            targets = await get_all_users()
            title = "Users (DMs)"
        elif target_type == "gc":
            targets = await get_all_groups()
            title = "Groups"
        else: # promo-all
            users = await get_all_users()
            groups = await get_all_groups()
            targets = users + groups
            title = "All (Users + Groups)"

        if not targets:
            return await message.reply_text("❌ Database mein koi target nahi mila.")

        # Status Message
        status_msg = await message.reply_text(f"🚀 **Broadcasting Started** to {len(targets)} {title}...")
        
        success = 0
        failed = 0

        # Forward Loop
        for target_id in targets:
            try:
                # Message ko Forward karna
                await message.reply_to_message.forward(chat_id=target_id)
                success += 1
                await asyncio.sleep(0.1) # FloodWait se bachne ke liye chhota delay
                
            except FloodWait as e:
                # Agar Telegram block kare (FloodWait), toh wait karo
                await asyncio.sleep(e.value)
                try:
                    await message.reply_to_message.forward(chat_id=target_id)
                    success += 1
                except:
                    failed += 1
            except (InputUserDeactivated, UserIsBlocked, PeerIdInvalid):
                failed += 1
            except Exception as e:
                print(f"Error forwarding to {target_id}: {e}")
                failed += 1

            # Live Status Update (Har 20 messages ke baad)
            if (success + failed) % 20 == 0:
                try:
                    await status_msg.edit(f"⏳ **Broadcasting...**\n\nDone: {success + failed}/{len(targets)}\n✅ Success: {success}\n❌ Failed: {failed}")
                except:
                    pass

        # Final Report
        report = (
            f"✅ **Broadcast Completed!**\n\n"
            f"📊 **Total Targets:** {len(targets)}\n"
            f"✅ **Successfully Forwarded:** {success}\n"
            f"❌ **Failed (Blocked/Deleted):** {failed}\n"
            f"🎯 **Type:** {title}"
        )
        await status_msg.edit(report)

    except Exception as e:
        await message.reply_text(f"❌ **Broadcast Error:** {e}")

# ===================================================================
# COMMAND HANDLERS (Sirf Owner ke liye)
# ===================================================================
# Yahan filters.user(OWNER_ID) laga hua hai. 
# Sirf aap hi is command ko chala payenge, koi aur nahi.

@Client.on_message(filters.command(["promo-users", "promo_users"], prefixes=["/", "."]) & filters.user(OWNER_ID))
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
    await message.reply_text("✅ **All (Users + Groups) Broadcast Started in Background!**")
