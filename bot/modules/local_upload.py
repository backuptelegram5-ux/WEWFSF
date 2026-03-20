"""
local_upload.py - Upload local VPS files
Uses /status2 to track uploads separately with auto-updating status
"""

import os
import shutil
import time
import asyncio
from pathlib import Path
from pyrogram import filters
from pyrogram.handlers import MessageHandler
from pyrogram.errors import MessageNotModified

from .. import LOGGER
from ..core.config_manager import Config
from ..core.telegram_manager import TgClient
from ..helper.telegram_helper.message_utils import send_message, edit_message
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.status_utils import get_readable_file_size


# Separate tracking for uploads
upload_tasks = {}
status_message = None
status_update_task = None


class UploadTask:
    def __init__(self, name, size):
        self.name = name
        self.size = size
        self.uploaded = 0
        self.start = time.time()
    
    def progress_text(self):
        if self.size == 0:
            return "Unknown size"
        
        pct = (self.uploaded / self.size) * 100
        elapsed = time.time() - self.start
        speed = self.uploaded / elapsed if elapsed > 1 else 0
        remaining = self.size - self.uploaded
        eta = remaining / speed if speed > 0 else 0
        
        # Progress bar
        filled = int(pct / 10)
        bar = "■" * filled + "□" * (10 - filled)
        
        speed_str = get_readable_file_size(speed) + "/s" if speed > 0 else "0B/s"
        
        eta_str = ""
        if eta > 0:
            h = int(eta // 3600)
            m = int((eta % 3600) // 60)
            s = int(eta % 60)
            if h > 0:
                eta_str = f"{h}h {m}m"
            elif m > 0:
                eta_str = f"{m}m {s}s"
            else:
                eta_str = f"{s}s"
        else:
            eta_str = "-"
        
        return (
            f"📤 <b>{self.name}</b>\n"
            f"[{bar}] {pct:.1f}%\n"
            f"Uploaded: {get_readable_file_size(self.uploaded)} / {get_readable_file_size(self.size)}\n"
            f"Speed: {speed_str}\n"
            f"ETA: {eta_str}"
        )


async def update_status_loop():
    """Continuously update status message"""
    global status_message
    
    while True:
        try:
            await asyncio.sleep(3)  # Update every 3 seconds
            
            if not upload_tasks:
                # No active uploads, clear status
                if status_message:
                    try:
                        await status_message.delete()
                    except:
                        pass
                    status_message = None
                continue
            
            # Build status text
            lines = []
            for idx, (mid, task) in enumerate(upload_tasks.items(), 1):
                lines.append(f"{idx}. {task.progress_text()}")
            
            text = f"<b>📤 Upload Status</b>\n\n" + "\n\n".join(lines)
            
            # Update or create status message
            if status_message:
                try:
                    await status_message.edit_text(text)
                except MessageNotModified:
                    pass
                except Exception as e:
                    LOGGER.error(f"[Status] Edit error: {e}")
                    status_message = None
            else:
                # Create new status message
                try:
                    status_message = await TgClient.bot.send_message(
                        chat_id=Config.OWNER_ID,
                        text=text
                    )
                except Exception as e:
                    LOGGER.error(f"[Status] Send error: {e}")
        
        except asyncio.CancelledError:
            break
        except Exception as e:
            LOGGER.error(f"[Status] Loop error: {e}")


async def _progress(current, total, mid):
    """Update progress"""
    if mid in upload_tasks:
        upload_tasks[mid].uploaded = current


async def _upload_file(message, filepath, as_doc=False):
    """Upload single file"""
    global status_update_task
    
    try:
        if not os.path.exists(filepath):
            await send_message(message, f"❌ Not found: {filepath}")
            return False
        
        size = os.path.getsize(filepath)
        name = os.path.basename(filepath)
        mid = message.id
        
        if size > 2097152000:
            await send_message(message, f"❌ Too large: {name} ({size/(1024**3):.1f}GB)")
            return False
        
        # Track it
        upload_tasks[mid] = UploadTask(name, size)
        
        # Start status updater if not running
        if not status_update_task or status_update_task.done():
            status_update_task = asyncio.create_task(update_status_loop())
        
        LOGGER.info(f"[Upload] Starting: {name}")
        
        try:
            ext = Path(filepath).suffix.lower()
            
            if as_doc or ext in ['.zip', '.rar', '.7z', '.tar', '.gz']:
                await TgClient.bot.send_document(
                    chat_id=Config.OWNER_ID,
                    document=filepath,
                    caption=name,
                    progress=_progress,
                    progress_args=(mid,)
                )
            elif ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv']:
                await TgClient.bot.send_video(
                    chat_id=Config.OWNER_ID,
                    video=filepath,
                    caption=name,
                    supports_streaming=True,
                    progress=_progress,
                    progress_args=(mid,)
                )
            elif ext in ['.mp3', '.m4a', '.flac', '.ogg']:
                await TgClient.bot.send_audio(
                    chat_id=Config.OWNER_ID,
                    audio=filepath,
                    caption=name,
                    progress=_progress,
                    progress_args=(mid,)
                )
            else:
                await TgClient.bot.send_document(
                    chat_id=Config.OWNER_ID,
                    document=filepath,
                    caption=name,
                    progress=_progress,
                    progress_args=(mid,)
                )
            
            LOGGER.info(f"[Upload] Done: {name}")
            return True
        
        finally:
            if mid in upload_tasks:
                del upload_tasks[mid]
    
    except Exception as e:
        if message.id in upload_tasks:
            del upload_tasks[message.id]
        LOGGER.error(f"[Upload] Error: {e}")
        return False


@new_task
async def upload_command(client, message):
    """Handle /upload"""
    args = message.text.split()
    
    if len(args) < 2:
        await send_message(
            message,
            "📤 <b>Local Upload</b>\n\n"
            "<code>/upload /mnt/home/path/file.mp4</code>\n"
            "<code>/upload -z /mnt/home/path/folder</code>\n"
            "<code>/upload -doc /mnt/home/path/file</code>\n\n"
            "Status updates automatically!"
        )
        return
    
    compress = "-z" in args
    as_doc = "-doc" in args
    
    path = None
    for arg in reversed(args[1:]):
        if not arg.startswith("-"):
            path = arg
            break
    
    if not path:
        await send_message(message, "❌ No path")
        return
    
    path = os.path.expanduser(path)
    
    if not os.path.exists(path):
        await send_message(message, f"❌ Not found: {path}")
        return
    
    try:
        if os.path.isfile(path):
            await _upload_file(message, path, as_doc)
            await send_message(message, "✅ Done!")
        
        elif os.path.isdir(path):
            if compress:
                await send_message(message, "📦 Zipping...")
                zip_name = f"{os.path.basename(path)}.zip"
                zip_path = f"/tmp/{zip_name}"
                shutil.make_archive(zip_path.replace('.zip', ''), 'zip', path)
                await _upload_file(message, zip_path, True)
                os.remove(zip_path)
                await send_message(message, "✅ Done!")
            else:
                files = list(Path(path).rglob('*'))
                files = [f for f in files if f.is_file()]
                
                if not files:
                    await send_message(message, "❌ Empty")
                    return
                
                await send_message(message, f"📤 {len(files)} files...")
                
                done = 0
                for idx, file in enumerate(files, 1):
                    msg = await send_message(message, f"({idx}/{len(files)}) {file.name}")
                    fake_msg = type('obj', (object,), {'id': msg.id, 'chat': message.chat})()
                    if await _upload_file(fake_msg, str(file), as_doc):
                        done += 1
                
                await send_message(message, f"✅ {done}/{len(files)}")
    
    except Exception as e:
        await send_message(message, f"❌ {e}")


@new_task
async def status2_command(client, message):
    """Show upload status (manual trigger)"""
    if not upload_tasks:
        await send_message(message, "💤 No active uploads")
        return
    
    lines = []
    for idx, (mid, task) in enumerate(upload_tasks.items(), 1):
        lines.append(f"{idx}. {task.progress_text()}")
    
    text = "\n\n".join(lines)
    await send_message(message, f"<b>📤 Upload Status</b>\n\n{text}")


async def start_local_upload():
    """Register commands and start status updater"""
    global status_update_task
    
    TgClient.bot.add_handler(
        MessageHandler(
            upload_command,
            filters=filters.command("upload") & filters.user(Config.OWNER_ID)
        )
    )
    
    TgClient.bot.add_handler(
        MessageHandler(
            status2_command,
            filters=filters.command("status2") & filters.user(Config.OWNER_ID)
        )
    )
    
    # Start the status update loop
    status_update_task = asyncio.create_task(update_status_loop())
    
    LOGGER.info("[Upload] /upload and /status2 registered with auto-update")
