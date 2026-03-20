"""
auto_rss.py - /mode command
One at a time, inactivity timeout, metadata timeout, non-blocking, cancellable.

COMMANDS:
  /mode          - Start collecting forwarded messages
  /mode stop     - Stop collecting, begin downloading queue
  /mode cancel   - Cancel current download and stop queue
  /mode status   - Show current queue status
"""

import re
from asyncio import sleep, create_task, CancelledError, Event
from types import SimpleNamespace
from datetime import datetime

from pyrogram import filters
from pyrogram.handlers import MessageHandler

from .. import LOGGER, bot_loop, task_dict, task_dict_lock
from ..core.config_manager import Config
from ..core.telegram_manager import TgClient
from ..helper.mirror_leech_utils.download_utils.qbit_download import add_qb_torrent
from ..helper.listeners.task_listener import TaskListener
from ..helper.telegram_helper.message_utils import send_message
from ..helper.ext_utils.bot_utils import new_task

# ============================================================
# SETTINGS
# ============================================================
INACTIVITY_TIMEOUT = 300   # Seconds of 0 speed before cancelling (5 min)
METADATA_TIMEOUT   = 300   # Seconds stuck in [METADATA] before cancelling (5 min)
# ============================================================

_mode_active  = False
_queue        = []
_processing   = False
_current_mid  = None
_cancel_event = Event()
_notify_msg   = None

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[^\s]+", re.IGNORECASE)


async def _notify(text: str):
    """Send update to user"""
    if _notify_msg:
        try:
            await send_message(_notify_msg, text)
        except Exception as e:
            LOGGER.error(f"[AutoMode] notify error: {e}")


def _make_fake_message(magnet: str, title: str):
    chat = SimpleNamespace(
        id=Config.OWNER_ID,
        type=SimpleNamespace(name="PRIVATE"),
    )
    user = SimpleNamespace(
        id=Config.OWNER_ID,
        username=None,
        mention="AutoMode",
        is_bot=False,
    )
    return SimpleNamespace(
        id=int(datetime.now().timestamp() * 1000) % 2147483647,
        text=f"/qbleech -z {magnet}",
        chat=chat,
        from_user=user,
        sender_chat=None,
        reply_to_message=None,
        link=f"https://fake/{int(datetime.now().timestamp())}",
        _client=TgClient.bot,
    )


class AutoModeTask(TaskListener):
    def __init__(self, magnet: str, title: str):
        self.message = _make_fake_message(magnet, title)
        self.client  = TgClient.bot
        super().__init__()
        self.is_qbit        = True
        self.is_leech       = True
        self.compress       = True
        self.is_rss         = True
        self.link           = magnet
        self.name           = title
        self.up_dest        = "pm"
        self.seed           = False
        self.select         = False
        self.extract        = False
        self.join           = False
        self.force_run      = False
        self.force_download = False
        self.force_upload   = False
        self.multi          = 0
        self.folder_name    = ""
        self.rc_flags       = ""
        self.thumb          = ""
        self.split_size     = 0
        self.same_dir       = {}
        self.bulk           = []
        self.multi_tag      = None
        self.options        = ""

    async def run(self):
        path = f"/app/downloads/{self.mid}"
        try:
            await self.before_start()
        except Exception as e:
            LOGGER.error(f"[AutoMode] before_start error: {e}")
            await _notify(f"❌ <b>Failed to start:</b>\n<code>{self.name}</code>\nReason: {e}")
            return False
        try:
            await add_qb_torrent(self, path, ratio=None, seed_time=None)
        except Exception as e:
            LOGGER.error(f"[AutoMode] add_qb_torrent error: {e}")
            await _notify(f"❌ <b>qBittorrent error:</b>\n<code>{self.name}</code>\nReason: {e}")
            return False
        return True


async def _watch_inactivity(mid: int, title: str):
    """
    Returns True if timed out, False if finished normally.
    Separate timeouts for metadata fetching vs actual downloading.
    """
    stalled_for  = 0
    metadata_for = 0
    check_interval = 15

    await sleep(20)

    while True:
        await sleep(check_interval)

        async with task_dict_lock:
            task = task_dict.get(mid)

        if task is None:
            return False

        try:
            await task.update()
            speed = task._info.dlspeed
            state = task._info.state
        except Exception as e:
            LOGGER.warning(f"[AutoMode] watcher update error: {e}")
            stalled_for += check_interval
            continue

        # Check for stuck metadata fetching (dead magnet)
        if state == "metaDL":
            metadata_for += check_interval
            if metadata_for >= METADATA_TIMEOUT:
                LOGGER.warning(f"[AutoMode] Metadata timeout (dead magnet): {title}")
                return True
            continue

        # Reset metadata counter once past that state
        metadata_for = 0

        # Normal download inactivity check
        if speed > 0:
            stalled_for = 0
        elif state in ["checkingResumeData", "checkingDL", "checkingUP"]:
            stalled_for = 0
        elif state in ["uploading", "stalledUP", "forcedUP", "moving"]:
            return False
        else:
            stalled_for += check_interval

        if stalled_for >= INACTIVITY_TIMEOUT:
            LOGGER.warning(f"[AutoMode] Inactivity timeout: {title}")
            return True


async def _cancel_mid(mid):
    async with task_dict_lock:
        task = task_dict.get(mid)
    if task:
        try:
            await task.cancel_task()
        except Exception as e:
            LOGGER.error(f"[AutoMode] cancel error: {e}")


async def _process_queue(trigger_message):
    global _processing, _queue, _current_mid, _notify_msg
    _processing  = True
    _notify_msg  = trigger_message
    _cancel_event.clear()
    total = len(_queue)

    await _notify(
        f"⚙️ <b>AutoMode:</b> Processing {total} item(s)\n"
        f"📲 Uploading to your DM\n"
        f"⏱️ {INACTIVITY_TIMEOUT//60}min inactivity | {METADATA_TIMEOUT//60}min metadata timeout\n"
        f"❌ <code>/mode cancel</code> to stop anytime"
    )

    count = 0
    while _queue:
        if _cancel_event.is_set():
            break

        title, magnet = _queue.pop(0)
        count += 1
        LOGGER.info(f"[AutoMode] ({count}/{total}) Starting: {title}")

        await _notify(
            f"📥 <b>({count}/{total})</b>\n"
            f"<code>{title}</code>\n"
            f"⏳ {len(_queue)} remaining"
        )

        task = AutoModeTask(magnet, title)
        _current_mid = task.mid

        started = await task.run()

        if not started:
            await _notify(f"⏭️ Skipping to next item...")
            await sleep(3)
            continue

        # Wait for task registration
        registered = False
        for _ in range(10):
            await sleep(0.5)
            async with task_dict_lock:
                if _current_mid in task_dict:
                    registered = True
                    break

        if not registered:
            LOGGER.error(f"[AutoMode] Task never registered: {title}")
            await _notify(
                f"❌ <b>Task failed silently:</b>\n<code>{title}</code>\n"
                f"qBittorrent rejected the magnet. Skipping..."
            )
            await sleep(3)
            continue

        LOGGER.info(f"[AutoMode] Task registered mid={_current_mid}: {title}")

        # Start watcher
        watcher = create_task(_watch_inactivity(_current_mid, title))

        # Wait for completion or cancel
        while True:
            await sleep(10)

            if _cancel_event.is_set():
                watcher.cancel()
                await _cancel_mid(_current_mid)
                break

            async with task_dict_lock:
                still_running = _current_mid in task_dict
            if not still_running:
                watcher.cancel()
                break

            if watcher.done():
                timed_out = False
                try:
                    timed_out = watcher.result()
                except Exception:
                    pass
                if timed_out:
                    # Check what kind of timeout
                    reason = "No progress"
                    async with task_dict_lock:
                        task = task_dict.get(_current_mid)
                    if task:
                        try:
                            await task.update()
                            if task._info.state == "metaDL":
                                reason = "Dead magnet (metadata timeout)"
                        except:
                            pass
                    
                    await _notify(
                        f"⏱️ <b>Timed out:</b> {reason}\n"
                        f"<code>{title}</code>\nMoving to next..."
                    )
                    await _cancel_mid(_current_mid)
                break

        try:
            await watcher
        except (CancelledError, Exception):
            pass

        if _cancel_event.is_set():
            break

        await sleep(5)

    _current_mid = None
    _processing  = False

    if _cancel_event.is_set():
        await _notify("🛑 <b>AutoMode cancelled.</b>")
    else:
        await _notify(
            f"✅ <b>AutoMode complete!</b> {count}/{total} item(s) processed.\n"
            f"Check your DM for uploaded files."
        )


@new_task
async def mode_command(client, message):
    global _mode_active, _queue, _processing

    parts   = message.text.strip().split()
    cmd_arg = parts[1].lower() if len(parts) > 1 else ""

    if cmd_arg == "cancel":
        if not _processing:
            await send_message(message, "⚠️ Nothing is currently processing.")
            return
        _cancel_event.set()
        await _cancel_mid(_current_mid)
        await send_message(message, "🛑 Cancelling and stopping queue...")

    elif cmd_arg == "status":
        if _mode_active:
            lines = "\n".join(f"{i+1}. {t}" for i, (t, _) in enumerate(_queue))
            await send_message(
                message,
                f"📋 <b>Collecting mode ON</b>\n"
                f"Items so far: {len(_queue)}\n\n{lines or 'None yet'}"
            )
        elif _processing:
            await send_message(
                message,
                f"⚙️ <b>Processing queue</b>\n"
                f"Remaining: {len(_queue)}\n"
                f"Use /status for download progress"
            )
        else:
            await send_message(message, "💤 AutoMode idle. Send /mode to start.")

    elif cmd_arg == "stop":
        if not _mode_active:
            await send_message(message, "⚠️ AutoMode is not collecting right now.")
            return
        _mode_active = False
        if not _queue:
            await send_message(message, "⚠️ Stopped. No items were collected.")
            return
        lines = "\n".join(f"{i+1}. {t}" for i, (t, _) in enumerate(_queue))
        await send_message(
            message,
            f"🛑 <b>Stopped collecting.</b>\n"
            f"📋 <b>{len(_queue)} item(s):</b>\n\n{lines}\n\n"
            f"⏳ Starting downloads now..."
        )
        bot_loop.create_task(_process_queue(message))

    else:
        if _processing:
            await send_message(message, "⚠️ Already processing! Use /mode cancel first.")
            return
        _mode_active = True
        _queue.clear()
        await send_message(
            message,
            f"✅ <b>AutoMode ON!</b>\n\n"
            f"Forward messages with magnet links.\n"
            f"Send <code>/mode stop</code> when done.\n\n"
            f"⚙️ One at a time  |  📲 DM\n"
            f"⏱️ {INACTIVITY_TIMEOUT//60}min inactive  |  {METADATA_TIMEOUT//60}min metadata timeout"
        )


@new_task
async def collect_forwarded(client, message):
    if not _mode_active:
        return

    text = message.text or message.caption or ""
    if text.startswith("/"):
        return

    magnets = MAGNET_RE.findall(text)
    if not magnets:
        return

    lines = text.strip().split("\n")
    title = "Unknown"
    skip_prefixes = ("magnet:", "🔗", "🆕", "Written by", "Read by", "Format:")
    for line in lines:
        line = line.strip()
        if line and len(line) > 5 and not any(line.startswith(p) for p in skip_prefixes):
            title = line
            break

    for magnet in magnets:
        _queue.append((title, magnet))
    LOGGER.info(f"[AutoMode] Collected ({len(_queue)}): {title}")

    await message.reply(
        f"📌 <b>Added!</b> ({len(_queue)} total)\n<code>{title}</code>",
        quote=True
    )


async def start_auto_rss():
    suffix = Config.CMD_SUFFIX

    TgClient.bot.add_handler(
        MessageHandler(
            mode_command,
            filters=filters.command(f"mode{suffix}") & filters.user(Config.OWNER_ID)
        )
    )

    blocked_cmds = [
        "mirror", "leech", "qbmirror", "qbleech", "ytdl", "ytdlleech",
        "status", "cancel", "cancelall", "bsetting", "usetting", "rss",
        "help", "log", "shell", "exec", "aexec", "restart", "ping",
        "stats", "mode", "forcestart", "sel", "list", "search", "clone",
        "count", "del", "auth", "unauth"
    ]

    TgClient.bot.add_handler(
        MessageHandler(
            collect_forwarded,
            filters=(
                filters.forwarded |
                (filters.text & ~filters.command(blocked_cmds))
            ) & filters.user(Config.OWNER_ID)
        )
    )

    LOGGER.info(
        f"[AutoMode] Ready! Inactive={INACTIVITY_TIMEOUT}s | Metadata={METADATA_TIMEOUT}s | Upload=DM"
    )
