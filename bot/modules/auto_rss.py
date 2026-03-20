"""
auto_rss.py - /mode command with duplicate detection

COMMANDS:
  /mode          - Start collecting
  /mode stop     - Stop collecting, begin downloading
  /mode cancel   - Cancel current + stop queue
  /mode status   - Show queue status
  /mode config   - Change duplicate handling: ask/skip/allow
"""

import re
import json
from asyncio import sleep, create_task, CancelledError, Event
from types import SimpleNamespace
from datetime import datetime

from pyrogram import filters
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from .. import LOGGER, bot_loop, task_dict, task_dict_lock
from ..core.config_manager import Config
from ..core.telegram_manager import TgClient
from ..helper.mirror_leech_utils.download_utils.qbit_download import add_qb_torrent
from ..helper.listeners.task_listener import TaskListener
from ..helper.telegram_helper.message_utils import send_message, edit_message
from ..helper.ext_utils.bot_utils import new_task

# ============================================================
# SETTINGS
# ============================================================
INACTIVITY_TIMEOUT = 300
METADATA_TIMEOUT   = 300
HISTORY_FILE       = "automode_history.json"
CONFIG_FILE        = "automode_config.json"
# ============================================================

_mode_active  = False
_queue        = []
_processing   = False
_current_mid  = None
_cancel_event = Event()
_notify_msg   = None
_pending_duplicates = {}  # {message_id: (title, magnet)}

MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[^\s]+", re.IGNORECASE)


def _load_history():
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except:
        return []


def _save_history(history):
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history[-1000:], f)  # keep last 1000
    except Exception as e:
        LOGGER.error(f"[AutoMode] save history error: {e}")


def _load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except:
        return {"duplicate_mode": "ask"}  # default


def _save_config(config):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f)
    except Exception as e:
        LOGGER.error(f"[AutoMode] save config error: {e}")


def _is_duplicate(magnet: str):
    history = _load_history()
    return magnet in history


def _add_to_history(magnet: str):
    history = _load_history()
    if magnet not in history:
        history.append(magnet)
        _save_history(history)


async def _notify(text: str):
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
            await _notify(f"❌ <b>Failed:</b> <code>{self.name}</code>\n{e}")
            return False
        try:
            await add_qb_torrent(self, path, ratio=None, seed_time=None)
        except Exception as e:
            LOGGER.error(f"[AutoMode] add_qb_torrent error: {e}")
            await _notify(f"❌ <b>qBittorrent error:</b> <code>{self.name}</code>\n{e}")
            return False
        return True


async def _watch_inactivity(mid: int, title: str):
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

        if state == "metaDL":
            metadata_for += check_interval
            if metadata_for >= METADATA_TIMEOUT:
                LOGGER.warning(f"[AutoMode] Metadata timeout: {title}")
                return True
            continue

        metadata_for = 0

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
        f"⚙️ <b>AutoMode:</b> {total} item(s)\n"
        f"📲 DM | ⏱️ {INACTIVITY_TIMEOUT//60}min inactive | {METADATA_TIMEOUT//60}min metadata"
    )

    count = 0
    while _queue:
        if _cancel_event.is_set():
            break

        title, magnet = _queue.pop(0)
        count += 1
        LOGGER.info(f"[AutoMode] ({count}/{total}) Starting: {title}")

        await _notify(f"📥 <b>({count}/{total})</b>\n<code>{title}</code>\n⏳ {len(_queue)} remaining")

        task = AutoModeTask(magnet, title)
        _current_mid = task.mid

        started = await task.run()

        if not started:
            await _notify(f"⏭️ Skipping...")
            await sleep(3)
            continue

        registered = False
        for _ in range(10):
            await sleep(0.5)
            async with task_dict_lock:
                if _current_mid in task_dict:
                    registered = True
                    break

        if not registered:
            LOGGER.error(f"[AutoMode] Task never registered: {title}")
            await _notify(f"❌ <b>Failed:</b> <code>{title}</code>\nqBittorrent rejected. Skipping...")
            await sleep(3)
            continue

        LOGGER.info(f"[AutoMode] Registered mid={_current_mid}: {title}")

        watcher = create_task(_watch_inactivity(_current_mid, title))

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
                # Add to history on successful completion
                _add_to_history(magnet)
                break

            if watcher.done():
                timed_out = False
                try:
                    timed_out = watcher.result()
                except:
                    pass
                if timed_out:
                    reason = "No progress"
                    async with task_dict_lock:
                        task = task_dict.get(_current_mid)
                    if task:
                        try:
                            await task.update()
                            if task._info.state == "metaDL":
                                reason = "Dead magnet"
                        except:
                            pass
                    await _notify(f"⏱️ <b>Timeout:</b> {reason}\n<code>{title}</code>\nSkipping...")
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
        await _notify("🛑 <b>Cancelled.</b>")
    else:
        await _notify(f"✅ <b>Complete!</b> {count}/{total} processed.\nCheck your DM.")


@new_task
async def mode_command(client, message):
    global _mode_active, _queue, _processing

    parts   = message.text.strip().split()
    cmd_arg = parts[1].lower() if len(parts) > 1 else ""

    if cmd_arg == "cancel":
        if not _processing:
            await send_message(message, "⚠️ Nothing is processing.")
            return
        _cancel_event.set()
        await _cancel_mid(_current_mid)
        await send_message(message, "🛑 Cancelling...")

    elif cmd_arg == "config":
        config = _load_config()
        current = config.get("duplicate_mode", "ask")
        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"{'✅ ' if current == 'ask' else ''}Ask me",
                    callback_data="automode_dup_ask"
                ),
                InlineKeyboardButton(
                    f"{'✅ ' if current == 'skip' else ''}Auto-skip",
                    callback_data="automode_dup_skip"
                ),
                InlineKeyboardButton(
                    f"{'✅ ' if current == 'allow' else ''}Always download",
                    callback_data="automode_dup_allow"
                )
            ]
        ])
        await send_message(
            message,
            f"⚙️ <b>Duplicate handling:</b>\n\n"
            f"<b>Ask me</b> - Prompt with Yes/No buttons\n"
            f"<b>Auto-skip</b> - Skip duplicates silently\n"
            f"<b>Always download</b> - Ignore duplicates\n\n"
            f"Current: <code>{current}</code>",
            buttons
        )

    elif cmd_arg == "status":
        if _mode_active:
            lines = "\n".join(f"{i+1}. {t}" for i, (t, _) in enumerate(_queue))
            await send_message(
                message,
                f"📋 <b>Collecting</b>\nItems: {len(_queue)}\n\n{lines or 'None yet'}"
            )
        elif _processing:
            await send_message(message, f"⚙️ <b>Processing</b>\nRemaining: {len(_queue)}")
        else:
            await send_message(message, "💤 Idle. Send /mode to start.")

    elif cmd_arg == "stop":
        if not _mode_active:
            await send_message(message, "⚠️ Not collecting.")
            return
        _mode_active = False
        if not _queue:
            await send_message(message, "⚠️ No items collected.")
            return
        lines = "\n".join(f"{i+1}. {t}" for i, (t, _) in enumerate(_queue))
        await send_message(
            message,
            f"🛑 <b>Stopped collecting.</b>\n📋 {len(_queue)} item(s):\n\n{lines}\n\n⏳ Starting..."
        )
        bot_loop.create_task(_process_queue(message))

    else:
        if _processing:
            await send_message(message, "⚠️ Already processing! Use /mode cancel first.")
            return
        _mode_active = True
        _queue.clear()
        config = _load_config()
        dup_mode = config.get("duplicate_mode", "ask")
        await send_message(
            message,
            f"✅ <b>AutoMode ON!</b>\n\n"
            f"Forward messages with magnets.\n"
            f"Send <code>/mode stop</code> when done.\n\n"
            f"⚙️ One at a time | 📲 DM\n"
            f"⏱️ {INACTIVITY_TIMEOUT//60}min inactive | {METADATA_TIMEOUT//60}min metadata\n"
            f"🔁 Duplicates: <code>{dup_mode}</code>"
        )


@new_task
async def collect_forwarded(client, message):
    global _pending_duplicates

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

    config = _load_config()
    dup_mode = config.get("duplicate_mode", "ask")

    for magnet in magnets:
        is_dup = _is_duplicate(magnet)

        if is_dup and dup_mode == "skip":
            # Auto-skip silently
            LOGGER.info(f"[AutoMode] Skipped duplicate: {title}")
            await message.reply(
                f"⏭️ <b>Duplicate (skipped)</b>\n<code>{title}</code>",
                quote=True
            )
            continue

        elif is_dup and dup_mode == "ask":
            # Ask user
            buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes", callback_data=f"automode_yes_{message.id}"),
                    InlineKeyboardButton("❌ No", callback_data=f"automode_no_{message.id}")
                ]
            ])
            _pending_duplicates[message.id] = (title, magnet)
            await message.reply(
                f"🔁 <b>Already downloaded before:</b>\n<code>{title}</code>\n\nDownload again?",
                reply_markup=buttons,
                quote=True
            )
            continue

        # Either not a duplicate OR dup_mode == "allow"
        _queue.append((title, magnet))
        LOGGER.info(f"[AutoMode] Collected ({len(_queue)}): {title}")
        await message.reply(
            f"📌 <b>Added!</b> ({len(_queue)} total)\n<code>{title}</code>",
            quote=True
        )


@new_task
async def handle_duplicate_response(client, query):
    global _pending_duplicates

    data = query.data
    msg_id = int(data.split("_")[-1])
    action = data.split("_")[1]  # "yes" or "no"

    if msg_id not in _pending_duplicates:
        await query.answer("⚠️ Expired or already answered.", show_alert=True)
        return

    title, magnet = _pending_duplicates.pop(msg_id)

    if action == "yes":
        _queue.append((title, magnet))
        LOGGER.info(f"[AutoMode] User approved duplicate ({len(_queue)}): {title}")
        await edit_message(
            query.message,
            f"✅ <b>Added again!</b> ({len(_queue)} total)\n<code>{title}</code>"
        )
    else:
        LOGGER.info(f"[AutoMode] User declined duplicate: {title}")
        await edit_message(query.message, f"❌ <b>Skipped</b>\n<code>{title}</code>")

    await query.answer()


@new_task
async def handle_config_change(client, query):
    data = query.data
    new_mode = data.split("_")[-1]  # "ask", "skip", or "allow"

    config = _load_config()
    config["duplicate_mode"] = new_mode
    _save_config(config)

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'✅ ' if new_mode == 'ask' else ''}Ask me",
                callback_data="automode_dup_ask"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if new_mode == 'skip' else ''}Auto-skip",
                callback_data="automode_dup_skip"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if new_mode == 'allow' else ''}Always download",
                callback_data="automode_dup_allow"
            )
        ]
    ])

    await edit_message(
        query.message,
        f"⚙️ <b>Duplicate handling:</b>\n\n"
        f"<b>Ask me</b> - Prompt with Yes/No buttons\n"
        f"<b>Auto-skip</b> - Skip duplicates silently\n"
        f"<b>Always download</b> - Ignore duplicates\n\n"
        f"Current: <code>{new_mode}</code>",
        buttons
    )
    await query.answer(f"✅ Set to: {new_mode}")


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

    # Callback handlers for duplicate yes/no and config changes
    TgClient.bot.add_handler(
        CallbackQueryHandler(
            handle_duplicate_response,
            filters=filters.regex(r"^automode_(yes|no)_\d+$") & filters.user(Config.OWNER_ID)
        )
    )

    TgClient.bot.add_handler(
        CallbackQueryHandler(
            handle_config_change,
            filters=filters.regex(r"^automode_dup_(ask|skip|allow)$") & filters.user(Config.OWNER_ID)
        )
    )

    LOGGER.info(f"[AutoMode] Ready! Inactive={INACTIVITY_TIMEOUT}s | Metadata={METADATA_TIMEOUT}s")
