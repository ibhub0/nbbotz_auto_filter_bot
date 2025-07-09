import logging
import time
import re
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from pyrogram.errors.exceptions.bad_request_400 import (
    ChannelInvalid, 
    ChatAdminRequired, 
    UsernameInvalid, 
    UsernameNotModified
)
from info import ADMINS, INDEX_REQ_CHANNEL as LOG_CHANNEL
from database.ia_filterdb import save_file
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from utils import temp, get_readable_time
from math import ceil

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Global lock for concurrent operations
lock = asyncio.Lock()

async def validate_chat_id(chat_id):
    """Validate and convert chat ID to proper format"""
    if chat_id.isnumeric():
        if not str(chat_id).startswith('-100'):
            chat_id = int(f"-100{chat_id}")
        else:
            chat_id = int(chat_id)
    return chat_id

async def get_chat_link(bot, chat_id):
    """Get invite link for a chat"""
    try:
        if isinstance(chat_id, int):
            return (await bot.create_chat_invite_link(chat_id)).invite_link
        return f"@{chat_id}"
    except Exception as e:
        logger.error(f"Error getting chat link: {e}")
        return "N/A"

@Client.on_callback_query(filters.regex(r'^index'))
async def index_files_handler(bot, query):
    """Handle indexing callback queries"""
    try:
        if query.data.startswith('index_cancel'):
            temp.CANCEL = True
            await query.answer("Cancelling Indexing")
            return

        _, action, chat_id, lst_msg_id, from_user = query.data.split("#", 4)
        
        if action == 'reject':
            await query.message.delete()
            await bot.send_message(
                int(from_user),
                f'Your Submission for indexing {chat_id} has been declined by our moderators.',
                reply_to_message_id=int(lst_msg_id)
            )
            return

        if lock.locked():
            await query.answer('Wait until previous process completes.', show_alert=True)
            return

        await query.answer('Processing...⏳', show_alert=True)
        msg = await query.message.edit(
            "Starting Indexing...",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
            )
        )

        try:
            chat_id = await validate_chat_id(chat_id)
            await index_files_to_db(int(lst_msg_id), chat_id, msg, bot)
            
            if int(from_user) not in ADMINS:
                await bot.send_message(
                    int(from_user),
                    f'Your Submission for indexing {chat_id} has been accepted and will be added soon.',
                    reply_to_message_id=int(lst_msg_id)
                )
                
        except Exception as e:
            logger.error(f"Error during indexing: {e}")
            await msg.edit(
                f"❌ Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton('Close', callback_data='close_data')]]
                )
            )

    except Exception as e:
        logger.error(f"Error in index_files_handler: {e}")

@Client.on_message(
    (filters.forwarded | (filters.regex(r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$") & filters.text)) & 
    filters.private & 
    filters.incoming
)
async def handle_index_request(bot, message):
    """Handle incoming index requests"""
    try:
        chat_id = None
        last_msg_id = None
        
        if message.text:
            match = re.match(
                r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$", 
                message.text
            )
            if not match:
                return await message.reply('Invalid link format')
            
            chat_id = match.group(4)
            last_msg_id = int(match.group(5))
            
        elif (message.forward_from_chat and 
              message.forward_from_chat.type == enums.ChatType.CHANNEL):
            last_msg_id = message.forward_from_message_id
            chat_id = message.forward_from_chat.username or message.forward_from_chat.id
        
        if not chat_id or not last_msg_id:
            return await message.reply('Invalid request')

        try:
            chat_id = await validate_chat_id(str(chat_id))
            await bot.get_chat(chat_id)
            test_msg = await bot.get_messages(chat_id, last_msg_id)
            
            if test_msg.empty:
                return await message.reply('Message not found or I am not an admin')

        except ChannelInvalid:
            return await message.reply('Make me an admin to index files')
        except (UsernameInvalid, UsernameNotModified):
            return await message.reply('Invalid chat specified')
        except Exception as e:
            logger.error(f"Chat verification error: {e}")
            return await message.reply(f'Error: {str(e)}')

        if message.from_user.id in ADMINS:
            buttons = [
                [InlineKeyboardButton('Yes', callback_data=f'index#accept#{chat_id}#{last_msg_id}#{message.from_user.id}')],
                [InlineKeyboardButton('Close', callback_data='close_data')]
            ]
            reply_markup = InlineKeyboardMarkup(buttons)
            return await message.reply(
                f'Index this channel/group?\n\n'
                f'Chat: <code>{chat_id}</code>\n'
                f'Last Message ID: <code>{last_msg_id}</code>',
                reply_markup=reply_markup
            )

        invite_link = await get_chat_link(bot, chat_id)
        buttons = [
            [InlineKeyboardButton('Accept Index', callback_data=f'index#accept#{chat_id}#{last_msg_id}#{message.from_user.id}')],
            [InlineKeyboardButton('Reject Index', callback_data=f'index#reject#{chat_id}#{message.id}#{message.from_user.id}')]
        ]
        
        await bot.send_message(
            LOG_CHANNEL,
            f'#IndexRequest\n\n'
            f'From: {message.from_user.mention} (<code>{message.from_user.id}</code>)\n'
            f'Chat: <code>{chat_id}</code>\n'
            f'Last Msg ID: <code>{last_msg_id}</code>\n'
            f'InviteLink: {invite_link}',
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        await message.reply('Request submitted. Waiting for moderator approval.')

    except Exception as e:
        logger.error(f"Error in handle_index_request: {e}")
        await message.reply('An error occurred processing your request')

@Client.on_message(filters.command('setskip') & filters.user(ADMINS))
async def set_skip_number(bot, message):
    """Set skip number command handler"""
    try:
        if ' ' in message.text:
            _, skip = message.text.split(maxsplit=1)
            try:
                temp.CURRENT = int(skip)
                await message.reply(f"SKIP number set to {temp.CURRENT}")
            except ValueError:
                await message.reply("Skip number must be an integer")
        else:
            await message.reply("Usage: /setskip <number>")
    except Exception as e:
        logger.error(f"Error in set_skip_number: {e}")
        await message.reply("An error occurred")

def get_progress_bar(percentage, length=15):
    """Generate visual progress bar"""
    filled = min(length, max(0, int(length * percentage / 100)))
    return '🟩' * filled + '⬜' * (length - filled)

async def index_files_to_db(lst_msg_id, chat_id, status_msg, bot):
    """Main indexing function"""
    stats = {
        'total': 0,
        'saved': 0,
        'duplicate': 0,
        'deleted': 0,
        'no_media': 0,
        'unsupported': 0,
        'errors': 0
    }
    
    BATCH_SIZE = 200
    start_time = time.time()
    
    async with lock:
        try:
            current = temp.CURRENT
            temp.CANCEL = False
            
            total_messages = max(0, lst_msg_id - current)
            if total_messages <= 0:
                await status_msg.edit("Nothing to index")
                return

            batches = ceil(total_messages / BATCH_SIZE)
            batch_times = []

            async def update_progress(current_batch):
                elapsed = time.time() - start_time
                processed = current - temp.CURRENT
                percentage = (processed / total_messages) * 100
                
                avg_time = sum(batch_times)/len(batch_times) if batch_times else 1
                eta = (total_messages - processed) / BATCH_SIZE * avg_time
                
                await status_msg.edit(
                    f"📊 Indexing Progress\n"
                    f"{get_progress_bar(percentage)} {percentage:.1f}%\n\n"
                    f"• Total: {total_messages}\n"
                    f"• Processed: {processed}\n"
                    f"• Saved: {stats['saved']}\n"
                    f"• Duplicates: {stats['duplicate']}\n"
                    f"• Errors: {stats['errors']}\n"
                    f"⏱ Elapsed: {get_readable_time(elapsed)}\n"
                    f"⏳ ETA: {get_readable_time(eta)}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
                    )
                )

            for batch in range(batches):
                if temp.CANCEL:
                    break
                    
                batch_start = time.time()
                start = current + 1
                end = min(current + BATCH_SIZE, lst_msg_id)
                message_ids = range(start, end + 1)
                
                try:
                    messages = await asyncio.gather(
                        *[bot.get_messages(chat_id, msg_id) for msg_id in message_ids],
                        return_exceptions=True
                    )
                except Exception as e:
                    logger.error(f"Batch error: {e}")
                    stats['errors'] += BATCH_SIZE
                    current = end
                    continue

                save_tasks = []
                for msg in messages:
                    current += 1
                    
                    if isinstance(msg, Exception) or msg.empty:
                        stats['deleted'] += 1
                        continue
                        
                    if not msg.media:
                        stats['no_media'] += 1
                        continue
                        
                    if msg.media not in (
                        enums.MessageMediaType.VIDEO, 
                        enums.MessageMediaType.AUDIO, 
                        enums.MessageMediaType.DOCUMENT
                    ):
                        stats['unsupported'] += 1
                        continue
                        
                    try:
                        media = getattr(msg, msg.media.value)
                        media.file_type = msg.media.value
                        media.caption = msg.caption
                        save_tasks.append(save_file(media))
                    except Exception as e:
                        logger.error(f"Message processing error: {e}")
                        stats['errors'] += 1
                        continue

                results = await asyncio.gather(*save_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        stats['errors'] += 1
                        logger.error(f"Error saving file: {result}")
                    else:
                        ok, code = result
                        if ok:
                            stats['saved'] += 1
                        elif code == 0:
                            stats['duplicate'] += 1
                        elif code == 2:
                            stats['errors'] += 1

                batch_time = time.time() - batch_start
                batch_times.append(batch_time)
                await update_progress(batch)

            elapsed = time.time() - start_time
            await status_msg.edit(
                f"✅ Indexing Completed!\n"
                f"• Total Messages: <code>{total_messages}</code>\n"
                f"• Saved: <code>{stats['saved']}</code>\n"
                f"• Duplicates: <code>{stats['duplicate']}</code>\n"
                f"• Errors: <code>{stats['errors']}</code>\n"
                f"⏱ Elapsed: <code>{get_readable_time(elapsed)}</code>",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton('Close', callback_data='close_data')]]
                )
            )

        except Exception as e:
            logger.error(f"Indexing error: {e}")
            await status_msg.edit(
                f"❌ Error: {str(e)}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton('Close', callback_data='close_data')]]
                )
            )
