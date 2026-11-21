#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║          OTP PANEL BOT  —  V4 EDITION               ║
║   Media Manager · Auto Backup · Logs · Firebase Mgr  ║
║   Dev: ˹Sʜᴏᴡɴ                                       ║
╚══════════════════════════════════════════════════════╝
"""

import os, re, gc, io, sys, time, json, asyncio, logging, signal
from datetime import datetime, timedelta
from typing import Optional
from html import escape

import aiohttp
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

# ═══════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.WARNING,
)

# ═══════════════════════════════════════════════════════
#  PREMIUM EMOJI SUPPORT
# ═══════════════════════════════════════════════════════
PREMIUM_EMOJI_IDS = (
    "5224607267797606837", "5404561694510833322", "6206446249181189526",
    "5206318837489743801", "5235588635885054955", "6206479140040743133",
    "5440621591387980068", "5246772116543512028", "5253742260054409879",
    "5339233635620899144", "6206497372176913599", "6206404510689007446",
    "6204162490515855272", "5372849966689566579", "6206174450765796040",
    "5339286072876614251",
)

_PREMIUM_INDEX = 0
_PTB_INLINE_KEYBOARD_BUTTON = InlineKeyboardButton


def premium_emoji(emoji_id: Optional[str] = None, fallback: str = "⭐️") -> str:
    global _PREMIUM_INDEX
    if emoji_id is None:
        emoji_id = PREMIUM_EMOJI_IDS[_PREMIUM_INDEX % len(PREMIUM_EMOJI_IDS)]
        _PREMIUM_INDEX += 1
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def premium_button(text: str, *args, emoji_id: Optional[str] = None,
                   fallback_emoji: str = "", style: str = None, **kwargs):
    global _PREMIUM_INDEX
    old_emoji_id = kwargs.pop("icon_custom_emoji_id", None)
    if emoji_id is None:
        emoji_id = old_emoji_id
    if emoji_id is None and PREMIUM_EMOJI_IDS:
        emoji_id = PREMIUM_EMOJI_IDS[_PREMIUM_INDEX % len(PREMIUM_EMOJI_IDS)]
        _PREMIUM_INDEX += 1
    if fallback_emoji and not emoji_id:
        text = f"{fallback_emoji} {text}".strip()
    api_kwargs = dict(kwargs.pop("api_kwargs", {}) or {})
    if emoji_id:
        api_kwargs["icon_custom_emoji_id"] = emoji_id
    selected_style = style if style in ("primary", "success", "danger") else "primary"
    api_kwargs["style"] = selected_style
    return _PTB_INLINE_KEYBOARD_BUTTON(text, *args, api_kwargs=api_kwargs, **kwargs)


# ═══════════════════════════════════════════════════════
#  CONFIG  (DEFAULT_DATABASES intentionally empty —
#  add panels via Admin Panel at runtime)
# ═══════════════════════════════════════════════════════
DEFAULT_DATABASES: dict[str, str] = {}   # <-- src clean; add via admin panel

DATABASES: dict[str, str] = {}

POLL_INTERVAL  = 15
SMS_LIMIT      = 10
DB_CONCURRENCY = 50
TOKEN          = ""
BOT_USERNAME   = "@shownpanel_Xbot"
DB_FILE        = "bot_database.json"
DEV_TAG        = "˹Sʜᴏᴡɴ"

# ── Gate settings ────────────────────────────────────
FORCE_JOIN_ENABLED = False
FORCE_JOIN_CHANNELS: list[dict] = []
REFERRAL_ENABLED   = True
REQUIRED_REFERRALS = 2

ADMIN_IDS: set[int] = {8695946179, 8886907325}

# ── Feature toggles (runtime) ───────────────────────
MAINTENANCE_MODE    = False
MAINTENANCE_TEXT    = "🔧 Bot maintenance mode mein hai. Thodi der mein wapas aayega."

# ── Logs GC chat ────────────────────────────────────
LOGS_CHAT_ID: Optional[int] = None   # admin sets this via /setlogschat

# ── Auto-backup ─────────────────────────────────────
BACKUP_CHAT_ID: Optional[int]  = None
BACKUP_INTERVAL_HOURS: int     = 6    # admin sets via admin panel

# ═══════════════════════════════════════════════════════
#  GLOBAL STATE
# ═══════════════════════════════════════════════════════
seen_ids: set[str] = set()
MAX_SEEN_IDS = 50000
first_run: bool = True
_main_app: Optional[Application] = None
_http_session: Optional[aiohttp.ClientSession] = None

all_users:        dict[int, dict] = {}
pending_action:   dict[int, dict] = {}
user_cooldowns:   dict[int, float] = {}
chats_registry:   set[int] = set()
allowed_users:    set[int] = set()
ADMIN_ONLY_TAGS:  set[str] = set()
user_focus:       dict[int, str]  = {}
user_page:        dict[int, int]  = {}
GLOBAL_DEVICE_CACHE: dict[str, list] = {}
pending_kw_confirm:  dict[int, str]  = {}

# ── Media Manager ───────────────────────────────────
# media_config = {
#   "default": {"type": "photo"|"video"|"animation"|None, "file_id": str, "caption": str},
#   "user": {user_id_str: {"type":..., "file_id":..., "caption":...}}
# }
media_config: dict = {
    "default": {"type": None, "file_id": None, "caption": ""},
    "user": {}
}

# ═══════════════════════════════════════════════════════
#  QUOTE-FORMAT REPLY HELPER
#  Every bot reply is wrapped in this style:
#  ❝ First letter big, rest small ❞
# ═══════════════════════════════════════════════════════
def _quote_format(text: str) -> str:
    """Wrap text in Telegram expandable blockquote."""
    if not text:
        return text
    return f"<blockquote expandable>{text}</blockquote>"


async def qreply(msg_or_query, text: str, reply_markup=None,
                 parse_mode: str = "HTML", **kwargs):
    """Send a quote-formatted reply."""
    wrapped = _quote_format(text)
    if hasattr(msg_or_query, "reply_text"):
        return await msg_or_query.reply_text(
            wrapped, reply_markup=reply_markup,
            parse_mode=parse_mode, **kwargs
        )
    # It's a CallbackQuery — edit the message
    return await safe_edit(msg_or_query, wrapped, reply_markup=reply_markup,
                           parse_mode=parse_mode)


# ═══════════════════════════════════════════════════════
#  SAVE / LOAD
# ═══════════════════════════════════════════════════════
def save_data():
    try:
        with open(DB_FILE, "w") as f:
            json.dump({
                "all_users": all_users,
                "DATABASES": DATABASES,
                "allowed_users": list(allowed_users),
                "admin_only_tags": list(ADMIN_ONLY_TAGS),
                "force_join_enabled": FORCE_JOIN_ENABLED,
                "force_join_channels": FORCE_JOIN_CHANNELS,
                "referral_enabled": REFERRAL_ENABLED,
                "required_referrals": REQUIRED_REFERRALS,
                "maintenance_mode": MAINTENANCE_MODE,
                "maintenance_text": MAINTENANCE_TEXT,
                "logs_chat_id": LOGS_CHAT_ID,
                "backup_chat_id": BACKUP_CHAT_ID,
                "backup_interval_hours": BACKUP_INTERVAL_HOURS,
                "media_config": media_config,
            }, f, indent=4)
    except Exception as e:
        tlog(f"Save Error: {e}")


def load_data():
    global all_users, DATABASES, allowed_users, ADMIN_ONLY_TAGS
    global FORCE_JOIN_ENABLED, FORCE_JOIN_CHANNELS, REFERRAL_ENABLED, REQUIRED_REFERRALS
    global MAINTENANCE_MODE, MAINTENANCE_TEXT, LOGS_CHAT_ID
    global BACKUP_CHAT_ID, BACKUP_INTERVAL_HOURS, media_config

    DATABASES = dict(DEFAULT_DATABASES)
    if not os.path.exists(DB_FILE):
        return
    try:
        with open(DB_FILE) as f:
            data = json.load(f)

        for k, v in data.get("all_users", {}).items():
            all_users[int(k)] = v

        saved_dbs = data.get("DATABASES", {})
        if saved_dbs:
            merged = dict(DEFAULT_DATABASES)
            merged.update(saved_dbs)
            DATABASES = merged

        allowed_users   = set(int(u) for u in data.get("allowed_users", []))
        ADMIN_ONLY_TAGS = set(data.get("admin_only_tags", []))

        FORCE_JOIN_ENABLED  = bool(data.get("force_join_enabled", False))
        REFERRAL_ENABLED    = bool(data.get("referral_enabled", True))
        MAINTENANCE_MODE    = bool(data.get("maintenance_mode", False))
        MAINTENANCE_TEXT    = str(data.get("maintenance_text", MAINTENANCE_TEXT))
        LOGS_CHAT_ID        = data.get("logs_chat_id")
        BACKUP_CHAT_ID      = data.get("backup_chat_id")

        try:
            REQUIRED_REFERRALS     = max(0, int(data.get("required_referrals", 2)))
            BACKUP_INTERVAL_HOURS  = max(1, int(data.get("backup_interval_hours", 6)))
        except Exception:
            pass

        # Force join channels
        saved_fc = data.get("force_join_channels")
        FORCE_JOIN_CHANNELS = []
        if isinstance(saved_fc, list):
            for item in saved_fc:
                if isinstance(item, dict):
                    ch = str(item.get("channel", "") or "").strip()
                    url = str(item.get("url", "") or "").strip()
                    if ch:
                        FORCE_JOIN_CHANNELS.append({"channel": ch, "url": url})
        else:
            old_ch = str(data.get("force_join_channel", "") or "").strip()
            if old_ch:
                FORCE_JOIN_CHANNELS.append({"channel": old_ch, "url": str(data.get("force_join_url", "") or "")})

        # Media config
        saved_media = data.get("media_config")
        if isinstance(saved_media, dict):
            media_config = saved_media
            media_config.setdefault("default", {"type": None, "file_id": None, "caption": ""})
            media_config.setdefault("user", {})

    except Exception as e:
        tlog(f"Load Error: {e}")


# ═══════════════════════════════════════════════════════
#  UTILITY
# ═══════════════════════════════════════════════════════
def tlog(msg: str) -> None:
    t = datetime.now().strftime("%I:%M:%S %p")
    print(f"[{t}]  {msg}", flush=True)


def html_escape(text: str) -> str:
    return escape(str(text))


def is_spamming(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return False
    now = time.time()
    if now - user_cooldowns.get(user_id, 0) < 0.5:
        return True
    user_cooldowns[user_id] = now
    return False


def fmt_num(n: str) -> str:
    c = re.sub(r"\D", "", n)
    if c.startswith("91") and len(c) == 12: return f"+{c}"
    if len(c) == 10:  return f"+91{c}"
    if len(c) > 4:    return f"+{c}"
    return c


OTP_PATTERNS = [
    re.compile(r"OTP[^\d]*(\d{4,8})",       re.IGNORECASE),
    re.compile(r"code[^\d]*(\d{4,8})",      re.IGNORECASE),
    re.compile(r"password[^\d]*(\d{4,8})",  re.IGNORECASE),
    re.compile(r"\b(\d{6})\b"),
    re.compile(r"\b(\d{4})\b"),
]


def extract_otp(text: str) -> Optional[str]:
    for pat in OTP_PATTERNS:
        m = pat.search(text)
        if m: return m.group(1)
    return None


def parse_battery(val) -> int:
    if isinstance(val, (int, float)): return int(val)
    if isinstance(val, str):
        d = re.sub(r"\D", "", val)
        return int(d) if d else 0
    return 0


def parse_status_str(val) -> str:
    return "online" if str(val or "").lower() == "online" else "offline"


def parse_status_bool(val) -> str:
    return "online" if val is True else "offline"


def sms_date(sms: dict) -> str:
    ds = sms.get("date") or sms.get("receivedDate") or sms.get("recivedDate")
    if ds: return ds
    if sms.get("timestamp"):
        try:
            ts = float(sms["timestamp"])
            if ts > 1e11: ts /= 1000
            return datetime.fromtimestamp(ts).strftime("%d %b %Y %I:%M %p")
        except: pass
    return "N/A"


def seen_key(device_id: str, k: str) -> str:
    return f"{device_id}/{k}"


async def safe_edit(query, text: str, reply_markup=None, parse_mode: str = "HTML"):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except Exception as e:
        tlog(f"safe_edit: {type(e).__name__}: {e}")
        return False


def find_tag_by_safe(safe_tag: str) -> Optional[str]:
    for tag in DATABASES.keys():
        st = tag.replace(" ", "_").replace(":", "").replace("/", "")[:30]
        if st == safe_tag:
            return tag
    return None


# ═══════════════════════════════════════════════════════
#  LOGS HELPER
# ═══════════════════════════════════════════════════════
async def send_log(text: str) -> None:
    if not LOGS_CHAT_ID or not _main_app:
        return
    try:
        await _main_app.bot.send_message(
            LOGS_CHAT_ID,
            f"<blockquote>{text}</blockquote>",
            parse_mode="HTML"
        )
    except Exception as e:
        tlog(f"Log send failed: {e}")


# ═══════════════════════════════════════════════════════
#  ACCESS CONTROL
# ═══════════════════════════════════════════════════════
def has_access(user_id: int) -> bool:
    if user_id in ADMIN_IDS or user_id in allowed_users:
        return True
    if REFERRAL_ENABLED and referral_count(user_id) >= max(0, int(REQUIRED_REFERRALS)):
        return True
    return False


def grant_access(user_id: int) -> None:
    allowed_users.add(user_id)
    save_data()


def revoke_access(user_id: int) -> None:
    allowed_users.discard(user_id)
    save_data()


def referral_count(user_id: int) -> int:
    refs = all_users.get(user_id, {}).get("referrals", [])
    return len(set(str(x) for x in refs)) if isinstance(refs, list) else 0


def referral_unlocked(user_id: int) -> bool:
    if user_id in ADMIN_IDS or user_id in allowed_users:
        return True
    if not REFERRAL_ENABLED:
        return True
    return referral_count(user_id) >= max(0, int(REQUIRED_REFERRALS))


def build_referral_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME.lstrip('@')}?start=ref_{user_id}"


def register_referrer(user_id: int, referrer_id: Optional[int]) -> bool:
    if not referrer_id or referrer_id == user_id or referrer_id not in all_users:
        return False
    udata = all_users.setdefault(user_id, {})
    if udata.get("referred_by"):
        return False
    udata["referred_by"] = int(referrer_id)
    udata.setdefault("referral_qualified", False)
    save_data()
    return True


def qualify_referral(user_id: int) -> bool:
    udata = all_users.get(user_id, {})
    if not udata.get("referred_by") or udata.get("referral_qualified"):
        return False
    referrer_id = int(udata["referred_by"])
    if referrer_id == user_id or referrer_id not in all_users:
        return False
    ref_data = all_users.setdefault(referrer_id, {})
    refs = ref_data.setdefault("referrals", [])
    if not isinstance(refs, list):
        refs = []
        ref_data["referrals"] = refs
    if str(user_id) not in {str(x) for x in refs}:
        refs.append(user_id)
    udata["referral_qualified"] = True
    save_data()
    return True


def get_force_join_url(channel_data: dict) -> str:
    if not isinstance(channel_data, dict): return ""
    url = str(channel_data.get("url", "") or "").strip()
    if url: return url
    ch = str(channel_data.get("channel", "") or "").strip()
    return f"https://t.me/{ch[1:]}" if ch.startswith("@") else ""


async def is_force_joined(bot, user_id: int) -> bool:
    if not FORCE_JOIN_ENABLED or not FORCE_JOIN_CHANNELS:
        return True
    for cd in FORCE_JOIN_CHANNELS:
        if not isinstance(cd, dict): continue
        ch = str(cd.get("channel", "") or "").strip()
        if not ch: continue
        try:
            member = await bot.get_chat_member(chat_id=ch, user_id=user_id)
            status = str(member.status).lower()
            if status in ("creator", "administrator", "member"):
                continue
            if status == "restricted" and bool(getattr(member, "is_member", False)):
                continue
            return False
        except Exception as e:
            tlog(f"Force join check failed: {e}")
            return False
    return True


async def gate_state(bot, user_id: int) -> tuple[bool, str]:
    if user_id in ADMIN_IDS:
        return True, "ready"
    if FORCE_JOIN_ENABLED and not await is_force_joined(bot, user_id):
        return False, "force_join"
    qualified_now = qualify_referral(user_id)
    if qualified_now:
        referrer_id = all_users.get(user_id, {}).get("referred_by")
        if referrer_id:
            rc = referral_count(int(referrer_id))
            if REFERRAL_ENABLED and rc == max(0, int(REQUIRED_REFERRALS)):
                try:
                    await bot.send_message(
                        int(referrer_id),
                        f"🎉 <b>Referral milestone!</b>\n{rc}/{REQUIRED_REFERRALS} referrals complete.\nBot access unlock ho gaya.",
                        parse_mode="HTML"
                    )
                except: pass
            # Log referral event
            udata = all_users.get(user_id, {})
            uname = udata.get("name", "Unknown")
            ts = datetime.now().strftime("%d %b %Y %I:%M %p")
            await send_log(
                f"🎁 <b>Referral Qualified</b>\n"
                f"User: {html_escape(uname)} (<code>{user_id}</code>)\n"
                f"Referred by: <code>{referrer_id}</code>\n"
                f"Time: {ts}"
            )
    if REFERRAL_ENABLED and not referral_unlocked(user_id):
        return False, "referrals"
    return True, "ready"


# ═══════════════════════════════════════════════════════
#  GATE KEYBOARDS
# ═══════════════════════════════════════════════════════
def force_join_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i, cd in enumerate(FORCE_JOIN_CHANNELS, 1):
        if not isinstance(cd, dict): continue
        ch = str(cd.get("channel", "") or "").strip()
        url = get_force_join_url(cd)
        if not ch or not url: continue
        label = ch if ch.startswith("@") else f"Channel {i}"
        rows.append([premium_button(f"Join {label}", url=url, emoji_id="5084613633418199991", style="primary")])
    rows.append([premium_button("✅ Verify Join", callback_data="verify_join", style="success")])
    return InlineKeyboardMarkup(rows)


def referral_gate_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("My Referral Link", callback_data="my_ref", emoji_id="5084613633418199991", style="primary")],
        [premium_button(f"Referrals {referral_count(user_id)}/{max(0,int(REQUIRED_REFERRALS))}", callback_data="my_ref", style="primary")],
        [premium_button("🔄 Refresh Status", callback_data="gate_refresh", style="primary")],
    ])


async def send_gate_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE, reason: str = None) -> None:
    user_id = update.effective_chat.id
    if reason is None:
        _, reason = await gate_state(ctx.bot, user_id)
    if reason == "force_join":
        ch_lines = []
        for i, cd in enumerate(FORCE_JOIN_CHANNELS, 1):
            if not isinstance(cd, dict): continue
            ch = str(cd.get("channel", "") or "").strip()
            if ch: ch_lines.append(f"{i}. <code>{html_escape(ch)}</code>")
        text = (
            f"🔒 <b>Join Required</b>\n━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(ch_lines or ["No channels set."])
            + "\n\nSabhi channels join karke Verify dabayein.\n━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(text, reply_markup=force_join_keyboard(), parse_mode="HTML")
        return
    if reason == "referrals":
        cnt = referral_count(user_id)
        need = max(0, int(REQUIRED_REFERRALS))
        text = (
            f"🎁 <b>Referral Unlock</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Required: <b>{need}</b> referrals\nProgress: <b>{cnt}/{need}</b>\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(text, reply_markup=referral_gate_keyboard(user_id), parse_mode="HTML")
        return
    await update.message.reply_text(NO_ACCESS_TEXT, parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  FIREBASE HELPERS
# ═══════════════════════════════════════════════════════
_FB_TIMEOUT = aiohttp.ClientTimeout(total=4, connect=2)


async def _get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300, keepalive_timeout=30)
        _http_session = aiohttp.ClientSession(connector=connector, timeout=_FB_TIMEOUT)
    return _http_session


async def fb_get(path: str, base: str) -> Optional[dict]:
    try:
        session = await _get_session()
        url = f"{base}/{path}.json" if path else f"{base}/.json"
        async with session.get(url) as r:
            if r.status != 200: return None
            data = await r.json(content_type=None)
            return data if isinstance(data, dict) else None
    except: return None


async def fb_keys(path: str, base: str) -> list[str]:
    try:
        session = await _get_session()
        url = f"{base}/{path}.json?shallow=true" if path else f"{base}/.json?shallow=true"
        async with session.get(url) as r:
            if r.status != 200: return []
            data = await r.json(content_type=None)
            return list(data.keys()) if isinstance(data, dict) else []
    except: return []


async def fb_patch(path: str, payload: dict, base: str) -> bool:
    try:
        session = await _get_session()
        async with session.patch(f"{base}/{path}.json", json=payload) as r:
            return r.status == 200
    except: return False


async def check_firebase_alive(url: str) -> tuple[bool, int]:
    """Returns (alive, latency_ms)."""
    start = time.time()
    try:
        session = await _get_session()
        async with session.get(
            f"{url}/.json?shallow=true",
            timeout=aiohttp.ClientTimeout(total=3)
        ) as r:
            latency = int((time.time() - start) * 1000)
            return r.status == 200, latency
    except:
        return False, -1


# ═══════════════════════════════════════════════════════
#  DEVICE DATA CLASSES
# ═══════════════════════════════════════════════════════
class Device:
    __slots__ = (
        "id", "name", "status", "battery", "timestamp",
        "numbers", "device_info", "sms_path", "base_url", "db_tag"
    )
    def __init__(self, id, name, status, battery, timestamp, numbers, device_info, sms_path, base_url, db_tag):
        self.id = id; self.name = name; self.status = status
        self.battery = battery; self.timestamp = timestamp; self.numbers = numbers
        self.device_info = device_info; self.sms_path = sms_path
        self.base_url = base_url; self.db_tag = db_tag


async def fetch_db_data(tag: str, url: str) -> list[Device]:
    devices_list, added_set = [], set()
    try:
        root_keys, sim_all, device_info_all, user_data_all, clients_all = await asyncio.gather(
            fb_keys("", url),
            fb_get("All_Users/simDetails", url),
            fb_get("All_Users/Data/DeviceInfo", url),
            fb_get("user_data", url),
            fb_get("clients", url),
        )
        if sim_all and isinstance(sim_all, dict):
            info_all = device_info_all or {}
            for dev_id, sim in sim_all.items():
                if dev_id in added_set: continue
                added_set.add(dev_id)
                info = info_all.get(dev_id) or {}
                nums = []
                for field in ("sim1Number", "sim2Number"):
                    n = (sim or {}).get(field, "")
                    if n and len(re.sub(r"\D", "", str(n))) > 4: nums.append(fmt_num(str(n)))
                model = info.get("DeviceModel") or info.get("Brand") or f"Device-{dev_id[:6]}"
                devices_list.append(Device(
                    id=dev_id, name=model, status=parse_status_str(info.get("Status")),
                    battery=parse_battery(info.get("Battery")),
                    timestamp=int(info.get("currentTimeMillis") or 0),
                    numbers=nums,
                    device_info=f"Model: {model}\nBrand: {info.get('Brand','')}\nDevice ID: {dev_id}",
                    sms_path=f"All_Users/sms/{dev_id}", base_url=url, db_tag=tag
                ))
        if user_data_all and isinstance(user_data_all, dict):
            for dev_id, data in user_data_all.items():
                if dev_id in added_set or not isinstance(data, dict): continue
                added_set.add(dev_id)
                nums = []
                for field in ("numberSim1", "numberSim2", "mobNo"):
                    n = data.get(field, "")
                    if n and len(re.sub(r"\D", "", str(n))) > 4: nums.append(fmt_num(str(n)))
                devices_list.append(Device(
                    id=dev_id, name=data.get("d_name") or f"Device-{dev_id[:6]}",
                    status=parse_status_str(data.get("status")),
                    battery=parse_battery(data.get("battery")),
                    timestamp=int(data.get("timestamp") or 0), numbers=nums,
                    device_info=data.get("Device_info") or f"Device ID: {dev_id}",
                    sms_path=f"user_sms/{dev_id}", base_url=url, db_tag=tag
                ))
        if clients_all and isinstance(clients_all, dict):
            for dev_id, client in clients_all.items():
                if dev_id in added_set or not isinstance(client, dict): continue
                nums = []
                mob = client.get("mobNo") or ""
                if mob and len(re.sub(r"\D", "", mob)) > 5: nums.append(fmt_num(mob))
                elif client.get("sims") and isinstance(client["sims"], list) and client["sims"]:
                    ph = (client["sims"][0] or {}).get("phoneNumber") or ""
                    if ph and len(re.sub(r"\D", "", ph)) > 5: nums.append(fmt_num(ph))
                if not nums and not client.get("modelName"): continue
                added_set.add(dev_id)
                model = client.get("modelName") or f"Device-{dev_id[:6]}"
                devices_list.append(Device(
                    id=dev_id, name=model,
                    status=parse_status_bool(client.get("status")),
                    battery=parse_battery(client.get("battery")),
                    timestamp=0, numbers=nums,
                    device_info=f"Model: {model}\nDevice ID: {dev_id}",
                    sms_path=f"All_Users/sms/{dev_id}", base_url=url, db_tag=tag
                ))
        if root_keys:
            type4_keys = [k for k in root_keys if len(k) == 16 and re.match(r"^[0-9a-fA-F]+$", k)]
            if type4_keys:
                async def fetch_t4(k):
                    return k, await fb_get(f"{k}/deviceInfo", url), await fb_get(f"{k}/simInfo", url), await fb_get(f"{k}/heartbeat", url)
                for k, info, sim, hb in await asyncio.gather(*(fetch_t4(k) for k in type4_keys)):
                    if not isinstance(info, dict) or k in added_set: continue
                    added_set.add(k)
                    nums = []
                    if isinstance(sim, dict):
                        for sv in sim.values():
                            if isinstance(sv, dict):
                                n = sv.get("number", "")
                                if n and len(re.sub(r"\D", "", n)) > 4: nums.append(fmt_num(n))
                    model = info.get("model") or info.get("brand") or f"Device-{k[:6]}"
                    ts = int(hb) if isinstance(hb, (int, float)) else 0
                    status = "online" if ts and (time.time() * 1000 - ts) < 300000 else "offline"
                    devices_list.append(Device(
                        id=k, name=model, status=status, battery=0, timestamp=ts, numbers=nums,
                        device_info=f"Model: {model}\nDevice ID: {k}",
                        sms_path=f"{k}/receivedSms", base_url=url, db_tag=tag
                    ))
    except: pass
    return devices_list


async def get_all_devices(is_admin: bool = True) -> list[Device]:
    unique = {}
    for tag in DATABASES.keys():
        if not is_admin and tag in ADMIN_ONLY_TAGS: continue
        for d in GLOBAL_DEVICE_CACHE.get(tag, []):
            if d.id not in unique: unique[d.id] = d
    devs = list(unique.values())
    devs.sort(key=lambda d: (0 if d.status == "online" else 1, 0 if d.numbers else 1, -d.timestamp))
    return devs


async def get_panel_devices(tag: str, is_admin: bool = True) -> list[Device]:
    if not is_admin and tag in ADMIN_ONLY_TAGS: return []
    devs = list(GLOBAL_DEVICE_CACHE.get(tag, []))
    devs.sort(key=lambda d: (0 if d.status == "online" else 1, 0 if d.numbers else 1, -d.timestamp))
    return devs


async def get_device_sms(device: Device, limit: int = SMS_LIMIT) -> list[dict]:
    data = await fb_get(device.sms_path, device.base_url)
    if not data: return []
    entries = [{"_key": k, **v} for k, v in data.items() if isinstance(v, dict)]
    entries.sort(key=lambda s: int(s.get("timestamp") or 0), reverse=True)
    return entries[:limit]


# ═══════════════════════════════════════════════════════
#  MEDIA MANAGER HELPERS
# ═══════════════════════════════════════════════════════
def get_welcome_media(user_id: int) -> dict:
    """Return per-user media config, falling back to default."""
    user_media = media_config.get("user", {}).get(str(user_id))
    if user_media and user_media.get("type"):
        return user_media
    return media_config.get("default", {"type": None, "file_id": None, "caption": ""})


async def send_welcome(update: Update, user_id: int, text: str, reply_markup=None) -> None:
    """Send welcome message with optional media."""
    mc = get_welcome_media(user_id)
    mtype = mc.get("type")
    file_id = mc.get("file_id")
    caption = mc.get("caption") or ""
    full_text = (caption + "\n\n" + text).strip() if caption else text
    full_text_quoted = _quote_format(full_text)

    try:
        if mtype == "photo" and file_id:
            await update.message.reply_photo(
                photo=file_id, caption=full_text_quoted,
                reply_markup=reply_markup, parse_mode="HTML"
            )
        elif mtype == "video" and file_id:
            await update.message.reply_video(
                video=file_id, caption=full_text_quoted,
                reply_markup=reply_markup, parse_mode="HTML"
            )
        elif mtype == "animation" and file_id:
            await update.message.reply_animation(
                animation=file_id, caption=full_text_quoted,
                reply_markup=reply_markup, parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                full_text_quoted, reply_markup=reply_markup, parse_mode="HTML"
            )
    except Exception as e:
        tlog(f"send_welcome error: {e}")
        try:
            await update.message.reply_text(
                full_text_quoted, reply_markup=reply_markup, parse_mode="HTML"
            )
        except: pass


# ═══════════════════════════════════════════════════════
#  REPLY MENU
# ═══════════════════════════════════════════════════════
def get_reply_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    keys = [
        [KeyboardButton("Devices List", icon_custom_emoji_id="5224607267797606837", style="primary"),
         KeyboardButton("Search Number", icon_custom_emoji_id="5404561694510833322", style="primary")],
        [KeyboardButton("Panel Status", icon_custom_emoji_id="6206446249181189526", style="success"),
         KeyboardButton("Recent OTPs", icon_custom_emoji_id="5206318837489743801", style="success")],
        [KeyboardButton("All Panels", icon_custom_emoji_id="5235588635885054955", style="primary"),
         KeyboardButton("Keyword Search", icon_custom_emoji_id="6206479140040743133", style="primary")],
        [KeyboardButton("Number Search", icon_custom_emoji_id="5440621591387980068", style="success")],
    ]
    if is_admin:
        keys.append([
            KeyboardButton("Bulk Search", icon_custom_emoji_id="5246772116543512028", style="danger"),
            KeyboardButton("Bot Status", icon_custom_emoji_id="5253742260054409879", style="danger"),
        ])
        keys.append([KeyboardButton("Admin Panel", icon_custom_emoji_id="5339233635620899144", style="danger")])
    return ReplyKeyboardMarkup(keyboard=keys, resize_keyboard=True)


# ═══════════════════════════════════════════════════════
#  SMS / OTP UI HELPERS
# ═══════════════════════════════════════════════════════
def device_label(d: Device) -> str:
    return " & ".join(d.numbers) if d.numbers else f"{d.name} ({d.id[:8]})"


def otp_in_date_range(sms: dict) -> bool:
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    tomorrow  = today + timedelta(days=1)
    ds = sms.get("date") or sms.get("receivedDate") or sms.get("recivedDate") or ""
    if ds:
        for fmt in ("%d %b %Y %I:%M %p", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"):
            try:
                return datetime.strptime(ds.strip()[:20], fmt).date() in (yesterday, today, tomorrow)
            except: continue
    if sms.get("timestamp"):
        try:
            ts = float(sms["timestamp"])
            if ts > 1e11: ts /= 1000
            return datetime.fromtimestamp(ts).date() in (yesterday, today, tomorrow)
        except: pass
    return False


def format_sms_block(sms: dict, num_label: str) -> tuple[str, Optional[str]]:
    body   = str(sms.get("body") or sms.get("message") or sms.get("text") or "").strip()
    sender = str(sms.get("sender") or "Unknown").strip()
    otp    = extract_otp(body)
    lines  = []
    if otp:
        lines.append(f"🔑 <b>Otp</b>  <code>{escape(str(otp))}</code>\n")
    lines += [
        f"👤 <b>From:</b> {escape(sender)}",
        f"📅 <b>Date:</b> {escape(sms_date(sms))}",
        f"📱 <b>Number:</b> {escape(num_label)}",
        "",
        "💬 <b>Message</b>",
        "",
        escape(body),
        "",
        "━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines), otp


def auto_forward_msg(sms: dict, num_label: str) -> str:
    body   = sms.get("body") or sms.get("message") or sms.get("text") or ""
    otp    = extract_otp(body)
    sender = escape(str(sms.get("sender", "Unknown")))
    return (
        f"⚡ <b>New OTP Received</b>\n"
        f"{'─'*22}\n"
        f"🔑  <b>OTP</b>     <code>{otp}</code>\n"
        f"📱  <b>Number</b>  {escape(num_label)}\n"
        f"👤  <b>From</b>    {sender}\n"
        f"📅  <b>Date</b>    {sms_date(sms)}\n"
        f"{'─'*22}\n"
        f"<i>{escape(body)}</i>"
    )


# ═══════════════════════════════════════════════════════
#  DEVICE LIST UI
# ═══════════════════════════════════════════════════════
def device_list_header(devices: list[Device], page: int = 0) -> str:
    online   = sum(1 for d in devices if d.status == "online")
    offline  = len(devices) - online
    page_sz  = 6
    total_pg = max(1, (len(devices) + page_sz - 1) // page_sz)
    page     = max(0, min(page, total_pg - 1))
    return (
        f"{premium_emoji('5253742260054409879','📱')} <b>Devices List</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{premium_emoji('5224607267797606837','🟢')} <b>Active:</b> {online}    "
        f"{premium_emoji('6206479140040743133','🔴')} <b>Offline:</b> {offline}\n"
        f"📱 <b>Total:</b> {len(devices)}\n"
        f"📄 <b>Page:</b> {page + 1}/{total_pg}\n"
        f"━━━━━━━━━━━━━━━━━━\nSelect a device:"
    )


def device_list_keyboard(devices: list[Device], page: int = 0) -> InlineKeyboardMarkup:
    page_sz  = 6
    total_pg = max(1, (len(devices) + page_sz - 1) // page_sz)
    page     = max(0, min(page, total_pg - 1))
    start    = page * page_sz
    rows     = []
    for d in devices[start: start + page_sz]:
        eid = "5224607267797606837" if d.status == "online" else "6206479140040743133"
        sty = "success" if d.status == "online" else "danger"
        lbl = html_escape(d.numbers[0]) if d.numbers else html_escape(d.name or "Unknown")[:25]
        rows.append([premium_button(lbl, callback_data=f"sel:{d.id}", emoji_id=eid, style=sty)])
    if not rows:
        rows.append([premium_button("No devices found", callback_data="noop", style="danger")])
    nav = []
    if page > 0:
        nav.append(premium_button("‹ Prev", callback_data=f"pg:{page-1}"))
    nav.append(premium_button(f"{page+1}/{total_pg}", callback_data="noop"))
    if page < total_pg - 1:
        nav.append(premium_button("Next ›", callback_data=f"pg:{page+1}"))
    rows.append(nav)
    rows.append([premium_button("🔄 Refresh", callback_data="home"), premium_button("Active Only", callback_data="online")])
    rows.append([premium_button("All Panels", callback_data="panels"), premium_button("✕ Close", callback_data="close_msg")])
    return InlineKeyboardMarkup(rows)


def online_only_keyboard(devices: list[Device]) -> InlineKeyboardMarkup:
    online = [d for d in devices if d.status == "online"]
    rows   = []
    for d in online[:6]:
        lbl = html_escape(d.numbers[0]) if d.numbers else html_escape(d.name or "Unknown")[:25]
        rows.append([premium_button(f"🟢 {lbl}", callback_data=f"sel:{d.id}")])
    if not rows:
        rows.append([premium_button("😴 No active device", callback_data="noop")])
    rows.append([premium_button("🔄 Refresh", callback_data="online"), premium_button("📋 All", callback_data="pg:0")])
    rows.append([premium_button("✕ Close", callback_data="close_msg")])
    return InlineKeyboardMarkup(rows)


def device_action_keyboard(dev_id: str, back_page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("🔄 Refresh Otp", callback_data=f"sel:{dev_id}"),
         premium_button("📋 Last 10 Sms", callback_data=f"msgs:{dev_id}")],
        [premium_button("ℹ Device Info", callback_data=f"info:{dev_id}"),
         premium_button("📤 Send Sms", callback_data=f"ss:{dev_id}")],
        [premium_button("◀ Back", callback_data=f"pg:{back_page}"),
         premium_button("All Panels", callback_data="panels")],
    ])


def panels_list_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for tag in sorted(DATABASES.keys()):
        if tag in ADMIN_ONLY_TAGS and not is_admin: continue
        cached    = GLOBAL_DEVICE_CACHE.get(tag, [])
        total     = len(cached)
        online    = sum(1 for d in cached if d.status == "online")
        num_count = sum(len(d.numbers) for d in cached if d.numbers)
        bar  = "🟢" if online > 0 else ("🔴" if total > 0 else "⚫")
        safe = tag.replace(" ", "_").replace(":", "").replace("/", "")[:30]
        lbl  = f"{bar} {tag} — {num_count} numbers"
        if is_admin and tag in ADMIN_ONLY_TAGS: lbl = "🔒 " + lbl
        rows.append([premium_button(lbl, callback_data=f"panel:{safe}")])
    rows.append([premium_button("🔄 Refresh", callback_data="panels"),
                 premium_button("✕ Close", callback_data="close_msg")])
    return InlineKeyboardMarkup(rows)


def panel_numbers_text(tag: str, devices: list[Device]) -> str:
    online = sum(1 for d in devices if d.status == "online")
    lines  = [
        f"📡 <b>{tag}</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"🟢 Online: {online}  🔴 Offline: {len(devices)-online}  📱 Total: {len(devices)}",
        f"━━━━━━━━━━━━━━━━━━",
    ]
    if not devices:
        lines.append("📭 Koi device nahi mila.")
    else:
        for i, d in enumerate(devices, 1):
            icon = "🟢" if d.status == "online" else "🔴"
            batt = f"🔋{d.battery}%" if d.battery else ""
            if d.numbers:
                lines.append(f"{i}. {icon} {' | '.join(d.numbers)} {batt}")
            else:
                lines.append(f"{i}. {icon} ⚙️ {d.name[:20]} {batt}")
    return "\n".join(lines)


def panel_numbers_keyboard(tag: str, devices: list[Device]) -> InlineKeyboardMarkup:
    rows = []
    for d in devices[:25]:
        icon = "🟢" if d.status == "online" else "🔴"
        lbl  = f"{icon} {d.numbers[0]}" if d.numbers else f"{icon} ⚙️ {d.name[:20]}"
        if d.numbers and len(d.numbers) > 1: lbl += f" & {d.numbers[1]}"
        rows.append([premium_button(lbl, callback_data=f"sel:{d.id}")])
    safe = tag.replace(" ", "_").replace(":", "").replace("/", "")[:30]
    rows.append([premium_button("🔄 Refresh", callback_data=f"panelref:{safe}"),
                 premium_button("◀ Back", callback_data="panels")])
    return InlineKeyboardMarkup(rows)


def bulksearch_panels_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for tag in sorted(DATABASES.keys()):
        cached = GLOBAL_DEVICE_CACHE.get(tag, [])
        online = sum(1 for d in cached if d.status == "online")
        bar    = "🟢" if online > 0 else "🔴"
        safe   = tag.replace(" ", "_").replace(":", "").replace("/", "")[:30]
        rows.append([premium_button(f"{bar} {tag} — {online} online", callback_data=f"bulksearch_panel:{safe}")])
    rows.append([premium_button("✕ Close", callback_data="close_msg")])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════
#  ADMIN PANEL UI
# ═══════════════════════════════════════════════════════
NO_ACCESS_TEXT = (
    "🚫 <b>Access Denied</b>\n━━━━━━━━━━━━━━━━━━\n"
    "Aapko is bot use karne ki ijazat nahi hai.\n"
    "Admin se contact karein.\n━━━━━━━━━━━━━━━━━━"
)


def admin_panel_text() -> str:
    fj   = "On" if FORCE_JOIN_ENABLED else "Off"
    rg   = "On" if REFERRAL_ENABLED   else "Off"
    mn   = "On" if MAINTENANCE_MODE   else "Off"
    logs = str(LOGS_CHAT_ID)   if LOGS_CHAT_ID   else "Not set"
    bkp  = str(BACKUP_CHAT_ID) if BACKUP_CHAT_ID else "Not set"
    return (
        f"{premium_emoji()} <b>Admin Panel</b>  <code>V4</code>\n"
        f"{'─'*22}\n"
        f"👥  <b>Total Users</b>    <code>{len(all_users)}</code>\n"
        f"✅  <b>Allowed</b>        <code>{len(allowed_users)}</code>\n"
        f"📡  <b>Active Chats</b>   <code>{len(chats_registry)}</code>\n"
        f"🗄  <b>Connected DBs</b>  <code>{len(DATABASES)}</code>\n"
        f"{'─'*22}\n"
        f"🔒  <b>Force Join</b>     <b>{fj}</b>  ({len(FORCE_JOIN_CHANNELS)} channels)\n"
        f"🎁  <b>Referral Gate</b>  <b>{rg}</b>  (need {REQUIRED_REFERRALS})\n"
        f"🔧  <b>Maintenance</b>    <b>{mn}</b>\n"
        f"📋  <b>Logs Chat</b>      <code>{logs}</code>\n"
        f"💾  <b>Backup Chat</b>    <code>{bkp}</code>  (every {BACKUP_INTERVAL_HOURS}h)\n"
        f"{'─'*22}\n"
        f"<i>Dev: {DEV_TAG}</i>"
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("🗄 Firebase Manager",    callback_data="admin_firebase_mgr",  style="primary")],
        [premium_button("✅ Grant Access",         callback_data="admin_grant_access",  style="success"),
         premium_button("❌ Revoke Access",        callback_data="admin_revoke_access", style="danger")],
        [premium_button("👥 User List",            callback_data="admin_users",         style="primary"),
         premium_button("📋 Access List",          callback_data="admin_access_list",   style="primary")],
        [premium_button("🔒 Force Join",           callback_data="admin_force_join",    style="primary"),
         premium_button("🎁 Referral Gate",        callback_data="admin_referral",      style="success")],
        [premium_button("🔧 Maintenance",          callback_data="admin_maintenance",   style="danger"),
         premium_button("🖼 Media Manager",        callback_data="admin_media_mgr",     style="primary")],
        [premium_button("📋 Set Logs Chat",        callback_data="admin_set_logs",      style="primary"),
         premium_button("💾 Backup Manager",       callback_data="admin_backup_mgr",    style="primary")],
        [premium_button("👤 Manage Admins",        callback_data="admin_manage_admins", style="danger"),
         premium_button("🔒 Panel Visibility",     callback_data="admin_setadmin_list", style="primary")],
        [premium_button("📢 Broadcast",            callback_data="admin_broadcast",     style="primary"),
         premium_button("🔄 Refresh",              callback_data="admin_refresh",       style="primary")],
        [premium_button("🔁 Restart Bot",          callback_data="admin_restart",       style="danger")],
        [premium_button("✕ Close",                 callback_data="close_msg",           style="danger")],
    ])


# ── Firebase Manager UI ──────────────────────────────
def firebase_mgr_text() -> str:
    total  = len(DATABASES)
    online_count = sum(
        1 for t, devs in GLOBAL_DEVICE_CACHE.items()
        if t in DATABASES and any(d.status == "online" for d in devs)
    )
    return (
        f"🗄 <b>Firebase Manager</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📡 Total Dbs   : <b>{total}</b>\n"
        f"🟢 Active Dbs  : <b>{online_count}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Options below:"
    )


def firebase_mgr_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("➕ Add Firebase (URL/Bulk)",  callback_data="admin_add_firebase", style="success")],
        [premium_button("➕ Add Firebase (.txt file)", callback_data="admin_add_firebase_txt", style="success")],
        [premium_button("🗑 Remove Firebase",           callback_data="admin_remove_firebase", style="danger")],
        [premium_button("🧹 Remove Dead Firebase",      callback_data="admin_remove_dead",     style="danger")],
        [premium_button("📋 List All Firebase",         callback_data="admin_list_firebase",   style="primary")],
        [premium_button("◀ Back to Admin",             callback_data="admin_refresh",          style="primary")],
    ])


def firebase_list_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    tags = sorted(DATABASES.keys())
    page_sz = 15
    total_pg = max(1, (len(tags) + page_sz - 1) // page_sz)
    page = max(0, min(page, total_pg - 1))
    rows = []
    for tag in tags[page * page_sz:(page + 1) * page_sz]:
        cached = GLOBAL_DEVICE_CACHE.get(tag, [])
        online = sum(1 for d in cached if d.status == "online")
        bar = "🟢" if online > 0 else "🔴"
        rows.append([premium_button(
            f"{bar} {tag} ({len(cached)} devs / {online} online)",
            callback_data=f"fb_detail:{tag.replace(' ','_').replace(':','').replace('/','')[:30]}"
        )])
    nav = []
    if page > 0:    nav.append(premium_button("‹ Prev", callback_data=f"fb_list_pg:{page-1}"))
    nav.append(premium_button(f"{page+1}/{total_pg}", callback_data="noop"))
    if page < total_pg - 1: nav.append(premium_button("Next ›", callback_data=f"fb_list_pg:{page+1}"))
    rows.append(nav)
    rows.append([premium_button("◀ Back", callback_data="admin_firebase_mgr")])
    return InlineKeyboardMarkup(rows)


def firebase_remove_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    tags = sorted(DATABASES.keys())
    page_sz = 10
    total_pg = max(1, (len(tags) + page_sz - 1) // page_sz)
    page = max(0, min(page, total_pg - 1))
    rows = []
    for tag in tags[page * page_sz:(page + 1) * page_sz]:
        safe = tag.replace(" ", "_").replace(":", "").replace("/", "")[:30]
        rows.append([premium_button(f"🗑 {tag}", callback_data=f"fb_remove:{safe}", style="danger")])
    nav = []
    if page > 0:    nav.append(premium_button("‹ Prev", callback_data=f"fb_rem_pg:{page-1}"))
    nav.append(premium_button(f"{page+1}/{total_pg}", callback_data="noop"))
    if page < total_pg - 1: nav.append(premium_button("Next ›", callback_data=f"fb_rem_pg:{page+1}"))
    rows.append(nav)
    rows.append([premium_button("◀ Back", callback_data="admin_firebase_mgr")])
    return InlineKeyboardMarkup(rows)


# ── Maintenance UI ───────────────────────────────────
def maintenance_text() -> str:
    state = "On" if MAINTENANCE_MODE else "Off"
    return (
        f"🔧 <b>Maintenance Mode</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Status  : <b>{state}</b>\n\n"
        f"<b>Current text:</b>\n"
        f"{escape(MAINTENANCE_TEXT)}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


def maintenance_keyboard() -> InlineKeyboardMarkup:
    toggle_lbl = "🔴 Disable" if MAINTENANCE_MODE else "🟢 Enable"
    toggle_sty = "danger" if MAINTENANCE_MODE else "success"
    return InlineKeyboardMarkup([
        [premium_button(toggle_lbl + " Maintenance", callback_data="admin_mn_toggle", style=toggle_sty)],
        [premium_button("✏️ Set Maintenance Text",   callback_data="admin_mn_text",   style="primary")],
        [premium_button("◀ Back to Admin",           callback_data="admin_refresh",   style="primary")],
    ])


# ── Media Manager UI ─────────────────────────────────
def media_mgr_text() -> str:
    mc = media_config.get("default", {})
    mtype = mc.get("type") or "None"
    custom_count = len(media_config.get("user", {}))
    return (
        f"🖼 <b>Media Manager</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Default media : <b>{mtype}</b>\n"
        f"Custom users  : <b>{custom_count}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Default caption:\n"
        f"{escape(mc.get('caption','') or 'None')}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


def media_mgr_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("🖼 Set Default Media",          callback_data="media_set_default",    style="primary")],
        [premium_button("✏️ Set Default Caption",        callback_data="media_set_caption",    style="primary")],
        [premium_button("🗑 Clear Default Media",        callback_data="media_clear_default",  style="danger")],
        [premium_button("👤 Set Media for User",         callback_data="media_set_user",       style="primary")],
        [premium_button("🗑 Clear User Media",           callback_data="media_clear_user",     style="danger")],
        [premium_button("📋 List User Media",            callback_data="media_list_user",      style="primary")],
        [premium_button("◀ Back to Admin",              callback_data="admin_refresh",         style="primary")],
    ])


# ── Backup Manager UI ────────────────────────────────
def backup_mgr_text() -> str:
    bkp  = str(BACKUP_CHAT_ID) if BACKUP_CHAT_ID else "Not set"
    return (
        f"💾 <b>Auto Backup Manager</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Backup chat   : <code>{bkp}</code>\n"
        f"Interval      : every <b>{BACKUP_INTERVAL_HOURS}</b> hours\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


def backup_mgr_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("📌 Set Backup Chat",          callback_data="admin_set_backup_chat",     style="primary")],
        [premium_button("⏱ Set Backup Interval",       callback_data="admin_set_backup_interval", style="primary")],
        [premium_button("💾 Manual Backup Now",         callback_data="admin_backup_now",          style="success")],
        [premium_button("◀ Back to Admin",             callback_data="admin_refresh",              style="primary")],
    ])


# ── Admin management UI ──────────────────────────────
def manage_admins_text() -> str:
    return (
        f"👤 <b>Manage Admins</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Current admins: <b>{len(ADMIN_IDS)}</b>\n"
        + "\n".join(f"• <code>{aid}</code>" for aid in ADMIN_IDS)
        + "\n━━━━━━━━━━━━━━━━━━"
    )


def manage_admins_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("➕ Add Admin",    callback_data="admin_add_admin",    style="success")],
        [premium_button("➖ Remove Admin", callback_data="admin_remove_admin", style="danger")],
        [premium_button("◀ Back",         callback_data="admin_refresh",      style="primary")],
    ])


# ── Force join UI ────────────────────────────────────
def admin_force_join_text() -> str:
    state = "On" if FORCE_JOIN_ENABLED else "Off"
    lines = [
        f"🔒 <b>Force Join Settings</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"Status   : <b>{state}</b>",
        f"Channels : <b>{len(FORCE_JOIN_CHANNELS)}</b>",
        ""
    ]
    for i, cd in enumerate(FORCE_JOIN_CHANNELS, 1):
        ch  = html_escape(str(cd.get("channel","") or ""))
        url = str(cd.get("url","") or "")
        lines.append(f"<b>{i}.</b> <code>{ch}</code> — {'Custom url' if url else 'Auto url'}")
    if not FORCE_JOIN_CHANNELS:
        lines.append("❌ No channels configured.")
    lines += ["", "━━━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)


def admin_force_join_keyboard() -> InlineKeyboardMarkup:
    state = "Disable" if FORCE_JOIN_ENABLED else "Enable"
    sty   = "danger"  if FORCE_JOIN_ENABLED else "success"
    return InlineKeyboardMarkup([
        [premium_button(f"{state} Force Join",  callback_data="admin_fj_toggle",  style=sty)],
        [premium_button("➕ Add Channel",        callback_data="admin_fj_add",     style="primary")],
        [premium_button("📋 List Channels",      callback_data="admin_fj_list",    style="primary"),
         premium_button("🗑 Remove Channel",     callback_data="admin_fj_remove",  style="danger")],
        [premium_button("◀ Back",               callback_data="admin_refresh",    style="primary")],
    ])


# ── Referral UI ──────────────────────────────────────
def admin_referral_text() -> str:
    state = "On" if REFERRAL_ENABLED else "Off"
    total = sum(referral_count(uid) for uid in all_users)
    return (
        f"🎁 <b>Referral Gate Settings</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Status   : <b>{state}</b>\n"
        f"Required : <b>{REQUIRED_REFERRALS}</b> referrals\n"
        f"Total qualified: <b>{total}</b>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


def admin_referral_keyboard() -> InlineKeyboardMarkup:
    state = "Disable" if REFERRAL_ENABLED else "Enable"
    sty   = "danger"  if REFERRAL_ENABLED else "success"
    return InlineKeyboardMarkup([
        [premium_button(f"{state} Referral Gate",   callback_data="admin_ref_toggle",   style=sty)],
        [premium_button("Set Required Referrals",   callback_data="admin_ref_required", style="primary")],
        [premium_button("Referral Stats",           callback_data="admin_ref_stats",    style="primary")],
        [premium_button("◀ Back",                  callback_data="admin_refresh",      style="primary")],
    ])


def admin_setadmin_text() -> str:
    locked = len(ADMIN_ONLY_TAGS)
    total  = len(DATABASES)
    return (
        f"🔒 <b>Panel Visibility</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔒 Hidden: {locked}  🔓 Visible: {total-locked}  📡 Total: {total}\n"
        f"━━━━━━━━━━━━━━━━━━\nClick panel to toggle admin-only:"
    )


def admin_setadmin_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for tag in sorted(DATABASES.keys()):
        safe = tag.replace(" ", "_").replace(":", "").replace("/", "")[:30]
        lbl  = f"🔒 {tag} (Admin Only)" if tag in ADMIN_ONLY_TAGS else f"🔓 {tag}"
        rows.append([premium_button(lbl, callback_data=f"toggleadmin:{safe}")])
    rows.append([premium_button("◀ Back", callback_data="admin_refresh"),
                 premium_button("✕ Close", callback_data="close_msg")])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════
#  BACKUP HELPER
# ═══════════════════════════════════════════════════════
async def do_backup(requester_chat: Optional[int] = None) -> None:
    if not _main_app: return
    target = BACKUP_CHAT_ID or requester_chat
    if not target: return
    save_data()
    try:
        with open(DB_FILE, "rb") as f:
            bio = io.BytesIO(f.read())
        bio.name = f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        await _main_app.bot.send_document(
            target, document=bio,
            caption=f"💾 Auto backup — {datetime.now().strftime('%d %b %Y %I:%M %p')}\n"
                    f"DBs: {len(DATABASES)} | Users: {len(all_users)}",
        )
    except Exception as e:
        tlog(f"Backup error: {e}")


async def auto_backup_loop() -> None:
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)
        await do_backup()


async def auto_save_loop() -> None:
    while True:
        await asyncio.sleep(60)
        save_data()


# ═══════════════════════════════════════════════════════
#  PARSE BULK PANEL URLS
# ═══════════════════════════════════════════════════════
def parse_bulk_panel_urls(text: str) -> tuple[int, int, int, int, dict]:
    success = dupes = conflicts = invalid = 0
    existing_urls = {str(u).strip().rstrip("/").lower() for u in DATABASES.values() if u}
    existing_tags = set(DATABASES.keys())
    newly_added   = {}
    idx = 1

    def next_tag() -> str:
        nonlocal idx
        while f"TAG{idx}" in existing_tags:
            idx += 1
        t = f"TAG{idx}"
        existing_tags.add(t)
        return t

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"): continue
        if "=" in line:
            tag, url = line.split("=", 1)
            tag, url = tag.strip(), url.strip()
            if not tag or not url: invalid += 1; continue
        else:
            url, tag = line, next_tag()
        if not (url.startswith("https://") or url.startswith("http://")):
            invalid += 1; continue
        norm = url.rstrip("/")
        if norm.lower() in existing_urls:
            dupes += 1; continue
        if tag in DATABASES or tag in newly_added:
            conflicts += 1; continue
        DATABASES[tag] = norm
        newly_added[tag] = norm
        existing_urls.add(norm.lower())
        success += 1

    return success, dupes, conflicts, invalid, newly_added


async def handle_bulk_panel_import(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                   text: str, source_label: str = "message") -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return
    success, dupes, conflicts, invalid, newly_added = parse_bulk_panel_urls(text)
    save_data()
    pending_action.pop(chat_id, None)
    await update.message.reply_text(
        _quote_format(
            f"✅ <b>Panel Import Complete</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📄 Source     : <code>{html_escape(source_label)}</code>\n"
            f"✅ Added      : <code>{success}</code>\n"
            f"🔁 Duplicates : <code>{dupes}</code>\n"
            f"⚠️ Conflicts  : <code>{conflicts}</code>\n"
            f"❌ Invalid    : <code>{invalid}</code>\n"
            f"📡 Total Dbs  : <code>{len(DATABASES)}</code>\n"
            f"━━━━━━━━━━━━━━━━━━"
        ), parse_mode="HTML"
    )
    if not newly_added: return
    await update.message.reply_text(
        _quote_format(f"🔄 <b>{len(newly_added)}</b> naye panels fetch ho rahe hain..."),
        parse_mode="HTML"
    )
    results = await asyncio.gather(*(fetch_db_data(t, u) for t, u in newly_added.items()), return_exceptions=True)
    for tag, result in zip(newly_added.keys(), results):
        GLOBAL_DEVICE_CACHE[tag] = [] if isinstance(result, Exception) else result
        devs = GLOBAL_DEVICE_CACHE[tag]
        online = sum(1 for d in devs if d.status == "online")
        lines  = [f"📡 <b>{html_escape(tag)}</b> — {len(devs)} devices, {online} online\n━━━━━━━━━━━━━━━━━━"]
        for i, d in enumerate(sorted(devs, key=lambda x: (0 if x.status=="online" else 1)), 1):
            icon = "🟢" if d.status == "online" else "🔴"
            batt = f" 🔋{d.battery}%" if d.battery else ""
            lines.append(f"{i}. {icon} {' | '.join(d.numbers) if d.numbers else '⚙️ '+d.name[:20]}{batt}")
        lines.append(f"━━━━━━━━━━━━━━━━━━\n📲 Total Numbers: {sum(len(d.numbers) for d in devs if d.numbers)}")
        msg = "\n".join(lines)
        await update.message.reply_text(_quote_format(msg[:4000]), parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  KEYWORD SEARCH
# ═══════════════════════════════════════════════════════
async def _run_full_keyword_search(chat_id: int, keyword: str, is_admin: bool,
                                    send_to: int, ctx) -> None:
    esc_kw   = html_escape(keyword)
    wait_msg = await ctx.bot.send_message(
        send_to,
        _quote_format(f"🔎 Full search chal raha hai — keyword <code>{esc_kw}</code>...\n⏳ Wait karein..."),
        parse_mode="HTML"
    )
    devices = await get_all_devices(is_admin)
    devices_with_nums = [d for d in devices if d.numbers]
    kw_sem = asyncio.Semaphore(30)

    async def _fetch(d):
        async with kw_sem: return await get_device_sms(d, limit=SMS_LIMIT)

    sms_results = await asyncio.gather(*(_fetch(d) for d in devices_with_nums), return_exceptions=True)
    no_kw = []
    no_sms_skipped = kw_matched = 0

    for d, smss in zip(devices_with_nums, sms_results):
        if isinstance(smss, Exception) or not smss:
            no_sms_skipped += 1; continue
        found = any(keyword in (s.get("body") or s.get("message") or s.get("text") or "").lower() for s in smss)
        if found:
            kw_matched += 1
        else:
            latest = smss[0]
            preview = (latest.get("body") or latest.get("message") or latest.get("text") or "")[:80]
            no_kw.append((d, latest, preview))

    try: await wait_msg.delete()
    except: pass

    total_checked = len(devices_with_nums)
    found_count   = len(no_kw)

    if not no_kw:
        await ctx.bot.send_message(
            send_to,
            _quote_format(
                f"🔎 <b>Keyword Search: <code>{esc_kw}</code></b>\n━━━━━━━━━━━━━━━━━━\n"
                f"📱 Checked: {total_checked}  ✅ Keyword mila (skip): {kw_matched}  ⏭ No sms: {no_sms_skipped}\n"
                f"━━━━━━━━━━━━━━━━━━\n✅ Sabhi numbers mein keyword mila."
            ), parse_mode="HTML"
        )
        return

    header = (
        f"🔎 <b>Keyword Search: <code>{esc_kw}</code></b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📱 Checked: {total_checked}  ✅ Skip (matched): {kw_matched}  ⏭ No sms: {no_sms_skipped}\n"
        f"🎯 Keyword Nahi Mila: <b>{found_count}</b> numbers\n━━━━━━━━━━━━━━━━━━\n"
    )
    lines, rows = [], []
    for i, (d, latest_sms, preview) in enumerate(no_kw[:30], 1):
        icon    = "🟢" if d.status == "online" else "🔴"
        num_str = d.numbers[0] if d.numbers else d.name
        dt      = sms_date(latest_sms)
        lines.append(f"{i}. {icon} {html_escape(num_str)}  [{html_escape(d.db_tag)}]  {dt}\n    💬 {html_escape(preview)}...")
        if i <= 15:
            rows.append([premium_button(f"{icon} {num_str}", callback_data=f"sel:{d.id}")])

    full_text = _quote_format(header + "\n".join(lines))
    if len(full_text) > 4000: full_text = full_text[:3990] + "\n..."
    rows.append([premium_button("✕ Close", callback_data="close_msg")])
    await ctx.bot.send_message(send_to, full_text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")

    all_nums   = [d.numbers[0] if d.numbers else d.name for d, _, __ in no_kw]
    file_bytes = (
        f"Keyword Search: {keyword}\n"
        f"Checked: {total_checked} | Mila+Skip: {kw_matched} | No sms: {no_sms_skipped}\n"
        f"Keyword Nahi Mila: {found_count} numbers\n{'='*40}\n"
        + "\n".join(all_nums)
    ).encode("utf-8")
    bio       = io.BytesIO(file_bytes)
    bio.name  = f"keyword_{keyword[:20]}.txt"
    await ctx.bot.send_document(
        send_to, document=bio, filename=bio.name,
        caption=_quote_format(f"📄 <code>{esc_kw}</code> — {found_count} numbers (keyword nahi mila)"),
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════
#  /START COMMAND
# ═══════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user    = update.effective_user
    is_admin = (chat_id in ADMIN_IDS)

    if all_users.get(chat_id, {}).get("banned"):
        await update.message.reply_text(_quote_format("🚫 Aap banned hain."), parse_mode="HTML")
        return

    is_new = chat_id not in all_users
    if is_new:
        all_users[chat_id] = {
            "name": user.full_name if user else "Unknown",
            "username": (user.username or "") if user else "",
            "joined_at": datetime.now().strftime("%d %b %Y %I:%M %p"),
            "banned": False,
            "referrals": [],
            "referral_qualified": False,
        }
    else:
        all_users[chat_id].setdefault("referrals", [])
        all_users[chat_id].setdefault("referral_qualified", False)

    if user:
        all_users[chat_id]["name"]     = user.full_name
        all_users[chat_id]["username"] = user.username or ""

    # Referral deep link
    if ctx.args:
        payload = str(ctx.args[0]).strip()
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload[4:])
                if register_referrer(chat_id, referrer_id):
                    await send_log(
                        f"🔗 <b>Referral Start</b>\n"
                        f"User: {html_escape(user.full_name if user else 'Unknown')} (<code>{chat_id}</code>)\n"
                        f"Referrer: <code>{referrer_id}</code>\n"
                        f"Time: {datetime.now().strftime('%d %b %Y %I:%M %p')}"
                    )
            except ValueError: pass

    chats_registry.add(chat_id)
    save_data()

    # Log new joins
    if is_new:
        uname = f"@{user.username}" if user and user.username else "No username"
        await send_log(
            f"👤 <b>New User Joined</b>\n"
            f"Name     : {html_escape(user.full_name if user else 'Unknown')}\n"
            f"Username : {uname}\n"
            f"User Id  : <code>{chat_id}</code>\n"
            f"Time     : {datetime.now().strftime('%d %b %Y %I:%M %p')}"
        )

    if is_admin:
        total_devices = sum(len(v) for v in GLOBAL_DEVICE_CACHE.values())
        online_devices = sum(
            sum(1 for d in v if d.status == "online")
            for v in GLOBAL_DEVICE_CACHE.values()
        )
        await send_welcome(
            update, chat_id,
            f"{premium_emoji()} <b>OTP Panel</b>  <code>V4</code>  {premium_emoji()}\n"
            f"{'─'*22}\n"
            f"👋 <b>{user.first_name if user else 'Admin'}</b>  ·  Admin\n"
            f"{'─'*22}\n"
            f"🗄  <b>DBs</b>      <code>{len(DATABASES)}</code>\n"
            f"📱  <b>Devices</b>  <code>{total_devices}</code>  "
            f"🟢 <code>{online_devices}</code>  🔴 <code>{total_devices - online_devices}</code>\n"
            f"👥  <b>Users</b>    <code>{len(all_users)}</code>\n"
            f"{'─'*22}\n"
            f"<i>Dev: {DEV_TAG}</i>",
            reply_markup=get_reply_menu(True)
        )
        return

    # Maintenance mode (non-admins blocked)
    if MAINTENANCE_MODE:
        await update.message.reply_text(
            _quote_format(MAINTENANCE_TEXT), parse_mode="HTML"
        )
        return

    ready, reason = await gate_state(ctx.bot, chat_id)
    if not ready:
        await send_gate_message(update, ctx, reason)
        return

    if not has_access(chat_id):
        await update.message.reply_text(
            _quote_format(
                f"👋 Hello, <b>{user.first_name if user else 'User'}</b>!\n\n"
                "🚫 <b>Access Required</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"Your Id: <code>{chat_id}</code>\n"
                "Admin se contact karein."
            ), parse_mode="HTML"
        )
        return

    total_devices = sum(len(v) for v in GLOBAL_DEVICE_CACHE.values())
    online_devices = sum(
        sum(1 for d in v if d.status == "online")
        for v in GLOBAL_DEVICE_CACHE.values()
    )
    await send_welcome(
        update, chat_id,
        f"{premium_emoji()} <b>OTP Panel</b>  <code>V4</code>  {premium_emoji()}\n"
        f"{'─'*22}\n"
        f"👋 <b>{user.first_name if user else 'User'}</b>  ·  Welcome\n"
        f"{'─'*22}\n"
        f"📡  <b>Panels</b>   <code>{len(DATABASES)}</code>\n"
        f"🟢  <b>Online</b>   <code>{online_devices}</code>  /  <code>{total_devices}</code>\n"
        f"{'─'*22}\n"
        f"<i>Dev: {DEV_TAG}</i>",
        reply_markup=get_reply_menu(is_admin)
    )


# ═══════════════════════════════════════════════════════
#  OTHER COMMANDS
# ═══════════════════════════════════════════════════════
async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return
    if not ctx.args:
        await update.message.reply_text(_quote_format("Usage: /grant <user_id>"), parse_mode="HTML"); return
    try:
        tid = int(ctx.args[0])
        grant_access(tid)
        name = all_users.get(tid, {}).get("name", "Unknown")
        await update.message.reply_text(_quote_format(f"✅ Access granted to {name} ({tid})"), parse_mode="HTML")
        try:
            await ctx.bot.send_message(tid, _quote_format("✅ <b>Access Granted!</b>\n\nBot use karne ki ijazat mil gayi.\n/start karein."), parse_mode="HTML")
        except: pass
    except ValueError:
        await update.message.reply_text(_quote_format("❌ Invalid user id."), parse_mode="HTML")


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return
    if not ctx.args:
        await update.message.reply_text(_quote_format("Usage: /revoke <user_id>"), parse_mode="HTML"); return
    try:
        tid = int(ctx.args[0])
        revoke_access(tid)
        await update.message.reply_text(_quote_format(f"❌ Access revoked for user {tid}"), parse_mode="HTML")
    except ValueError:
        await update.message.reply_text(_quote_format("❌ Invalid user id."), parse_mode="HTML")


async def cmd_setlogschat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global LOGS_CHAT_ID
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return
    LOGS_CHAT_ID = chat_id
    save_data()
    await update.message.reply_text(
        _quote_format(f"✅ Logs chat set to this chat: <code>{chat_id}</code>"), parse_mode="HTML"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        await update.message.reply_text(_quote_format("🚫 Admin only.")); return
    await update.message.reply_text(_quote_format("🔄 Checking db health..."), parse_mode="HTML")

    async def ping(tag: str, url: str) -> dict:
        alive, lat = await check_firebase_alive(url)
        cached = GLOBAL_DEVICE_CACHE.get(tag, [])
        return {"tag": tag, "alive": alive, "latency": lat,
                "total": len(cached), "online": sum(1 for d in cached if d.status == "online")}

    results = await asyncio.gather(*(ping(t, u) for t, u in DATABASES.items()))
    h, rem  = divmod(int(time.time() - _bot_start_time), 3600); m, s = divmod(rem, 60)
    lines   = [
        "📊 <b>Bot Status</b>", "━━━━━━━━━━━━━━━━━━",
        f"⏱ Uptime : <code>{h}h {m}m {s}s</code>",
        f"🗄 Dbs Alive : <code>{sum(1 for r in results if r['alive'])}/{len(results)}</code>",
        f"📱 Total Devices : <code>{sum(r['total'] for r in results)}</code>",
        f"🟢 Online : <code>{sum(r['online'] for r in results)}</code>",
        f"✅ Allowed Users : <code>{len(allowed_users)}</code>",
        "━━━━━━━━━━━━━━━━━━", "<b>Db Health:</b>",
    ]
    for r in sorted(results, key=lambda x: x["tag"]):
        lat = f"{r['latency']}ms" if r['latency'] >= 0 else "timeout"
        lines.append(f"{'🟢' if r['alive'] else '🔴'} <b>{r['tag']}</b> {lat} | {r['total']} devs | {r['online']} online")
    await update.message.reply_text(_quote_format("\n".join(lines)), parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ═══════════════════════════════════════════════════════
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global FORCE_JOIN_ENABLED, FORCE_JOIN_CHANNELS, REFERRAL_ENABLED, REQUIRED_REFERRALS
    global MAINTENANCE_MODE, MAINTENANCE_TEXT, LOGS_CHAT_ID, BACKUP_CHAT_ID, BACKUP_INTERVAL_HOURS
    global ADMIN_IDS

    query    = update.callback_query
    data     = query.data or ""
    chat_id  = query.message.chat_id
    is_admin = (chat_id in ADMIN_IDS)

    if all_users.get(chat_id, {}).get("banned") or is_spamming(chat_id):
        await query.answer(); return

    # ── Gate callbacks ───────────────────────────────────────────────
    if data == "verify_join":
        await query.answer("Checking membership…")
        ready, reason = await gate_state(ctx.bot, chat_id)
        if reason == "force_join":
            await safe_edit(query, _quote_format("🔒 <b>Join Required</b>\n━━━━━━━━━━━━━━━━━━\nSabhi channels join karke Verify dabayein."), reply_markup=force_join_keyboard()); return
        if reason == "referrals":
            await safe_edit(query, _quote_format(f"🎁 <b>Referral Unlock</b>\n━━━━━━━━━━━━━━━━━━\nProgress: <b>{referral_count(chat_id)}/{REQUIRED_REFERRALS}</b>"), reply_markup=referral_gate_keyboard(chat_id)); return
        if ready and has_access(chat_id):
            await safe_edit(query, _quote_format("✅ <b>Verified!</b>\n/start karke bot menu open karein."))
        else:
            await safe_edit(query, _quote_format(f"✅ Gate complete.\nYour Id: <code>{chat_id}</code>\nAdmin access required."))
        return

    if data == "gate_refresh":
        await query.answer("Refreshing…")
        ready, reason = await gate_state(ctx.bot, chat_id)
        if reason == "force_join":
            await safe_edit(query, _quote_format("🔒 <b>Join Required</b>"), reply_markup=force_join_keyboard())
        elif reason == "referrals":
            await safe_edit(query, _quote_format(f"🎁 Progress: <b>{referral_count(chat_id)}/{REQUIRED_REFERRALS}</b>"), reply_markup=referral_gate_keyboard(chat_id))
        elif ready and has_access(chat_id):
            await safe_edit(query, _quote_format("✅ <b>Unlocked</b>\n/start karein."))
        else:
            await safe_edit(query, _quote_format(f"✅ Gate complete.\nYour Id: <code>{chat_id}</code>"))
        return

    if data == "my_ref":
        await query.answer()
        link = build_referral_link(chat_id)
        await safe_edit(query, _quote_format(
            f"🎁 <b>My Referrals</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Qualified: <b>{referral_count(chat_id)}/{REQUIRED_REFERRALS}</b>\n\n"
            f"Your referral link:\n<code>{html_escape(link)}</code>"
        ), reply_markup=referral_gate_keyboard(chat_id))
        return

    if data == "noop":    await query.answer(); return
    if data == "close_msg":
        await query.answer()
        try: await query.message.delete()
        except: pass
        return

    # ── Non-admin gate ───────────────────────────────────────────────
    if not is_admin:
        if MAINTENANCE_MODE:
            await query.answer("🔧 Maintenance mode.", show_alert=True); return
        ready, reason = await gate_state(ctx.bot, chat_id)
        if not ready:
            await query.answer("🔒 Pehle access requirements complete karein.", show_alert=True); return
    if not has_access(chat_id):
        await query.answer("🚫 Access nahi hai!", show_alert=True); return

    await query.answer()

    # ── Navigation ───────────────────────────────────────────────────
    if data in ("home",) or data.startswith("pg:"):
        user_focus.pop(chat_id, None)
        pg = 0
        try: pg = int(data.split(":")[1])
        except: pass
        user_page[chat_id] = pg
        devices = await get_all_devices(is_admin)
        await safe_edit(query, _quote_format(device_list_header(devices, pg)), reply_markup=device_list_keyboard(devices, pg))
        return

    if data == "panels":
        user_focus.pop(chat_id, None)
        vis = [t for t in DATABASES if is_admin or t not in ADMIN_ONLY_TAGS]
        td  = sum(len(GLOBAL_DEVICE_CACHE.get(t, [])) for t in vis)
        go  = sum(sum(1 for d in GLOBAL_DEVICE_CACHE.get(t,[]) if d.status=="online") for t in vis)
        hdr = (
            f"{premium_emoji()} <b>All Panels</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📡 Panels: {len(vis)}  |  📱 Devices: {td}\n"
            f"🟢 Online: {go}  🔴 Offline: {td-go}\n━━━━━━━━━━━━━━━━━━\nPanel click karo → numbers dikhenge:"
        )
        await safe_edit(query, _quote_format(hdr), reply_markup=panels_list_keyboard(is_admin=is_admin))
        return

    if data == "online":
        devices = await get_all_devices(is_admin)
        await safe_edit(query, _quote_format("🟢 Online Numbers"), reply_markup=online_only_keyboard(devices))
        return

    if data.startswith("panel:"):
        safe_tag = data[6:]
        orig = find_tag_by_safe(safe_tag)
        if not orig: await safe_edit(query, _quote_format("❌ Panel nahi mila.")); return
        if orig in ADMIN_ONLY_TAGS and not is_admin:
            await query.answer("🚫 Access nahi hai!", show_alert=True); return
        devs = await get_panel_devices(orig)
        await safe_edit(query, _quote_format(panel_numbers_text(orig, devs)), reply_markup=panel_numbers_keyboard(orig, devs))
        return

    if data.startswith("panelref:"):
        safe_tag = data[9:]
        orig = find_tag_by_safe(safe_tag)
        if not orig: await safe_edit(query, _quote_format("❌ Panel nahi mila.")); return
        url = DATABASES.get(orig, "")
        if url: GLOBAL_DEVICE_CACHE[orig] = await fetch_db_data(orig, url)
        devs = await get_panel_devices(orig)
        await safe_edit(query, _quote_format(panel_numbers_text(orig, devs)), reply_markup=panel_numbers_keyboard(orig, devs))
        return

    if data.startswith("sel:"):
        dev_id  = data[4:]
        devices = await get_all_devices(is_admin)
        device  = next((d for d in devices if d.id == dev_id), None)
        if not device: return
        if not is_admin and device.db_tag in ADMIN_ONLY_TAGS:
            await query.answer("🚫 Access nahi hai!", show_alert=True); return
        user_focus.pop(chat_id, None)
        back_pg = user_page.get(chat_id, 0)
        latest_otp_line = ""
        try:
            recent = await get_device_sms(device, limit=SMS_LIMIT)
            for s in recent:
                sk = s.get("_key", "")
                if sk: seen_ids.add(seen_key(dev_id, sk))
            if recent:
                body = recent[0].get("body") or recent[0].get("message") or recent[0].get("text") or ""
                otp  = extract_otp(body)
                if otp: latest_otp_line = f"\n🔑 Latest Otp: <code>{otp}</code>\n{sms_date(recent[0])}"
        except: pass
        status_icon = "🟢 Online" if device.status == "online" else "🔴 Offline"
        batt_line   = f"\n🔋  <b>Battery</b>  <code>{device.battery}%</code>" if device.battery else ""
        text = (
            f"{premium_emoji()} <b>Device Terminal</b>\n"
            f"{'─'*22}\n"
            f"📱  <b>Number</b>  <code>{escape(device_label(device))}</code>\n"
            f"🔌  <b>Status</b>  {status_icon}{batt_line}\n"
            f"🗄  <b>Panel</b>   <code>{escape(device.db_tag)}</code>"
            f"{latest_otp_line}\n"
            f"{'─'*22}\n"
            f"🔄 Refresh dabao naya OTP dekhne ke liye."
        )
        await safe_edit(query, _quote_format(text), reply_markup=device_action_keyboard(dev_id, back_pg))
        return

    if data.startswith("msgs:"):
        dev_id  = data[5:]
        devices = await get_all_devices(is_admin)
        device  = next((d for d in devices if d.id == dev_id), None)
        if not device: return
        if not is_admin and device.db_tag in ADMIN_ONLY_TAGS:
            await query.answer("🚫 Access nahi hai!", show_alert=True); return
        smss = await get_device_sms(device)
        if not smss:
            await safe_edit(query, _quote_format("📭 No sms found."),
                            reply_markup=InlineKeyboardMarkup([[premium_button("◀ Back", callback_data=f"sel:{dev_id}")]])); return
        body_parts, otp_buttons = [], []
        for sms in smss:
            block, otp = format_sms_block(sms, device_label(device))
            body_parts.append(block)
            if otp: otp_buttons.append([premium_button(f"📋 Copy Otp: {otp}", callback_data=f"cp:{otp}")])
        otp_buttons.append([premium_button("◀ Back to Terminal", callback_data=f"sel:{dev_id}")])
        full_text = _quote_format("\n━━━━━━━━━━━━━━━━━━\n\n".join(body_parts)[:4000])
        await safe_edit(query, full_text, reply_markup=InlineKeyboardMarkup(otp_buttons))
        return

    if data.startswith("info:"):
        dev_id  = data[5:]
        devices = await get_all_devices(is_admin)
        device  = next((d for d in devices if d.id == dev_id), None)
        if not device: return
        await query.message.reply_text(
            _quote_format(f"📱 Device Info:\n\n{device.device_info}\n🔋 Battery: {device.battery}%\n📶 Network: {device.status}"),
            parse_mode="HTML"
        )
        return

    if data.startswith("ss:"):
        pending_action[chat_id] = {"action": "execute_ss", "dev_id": data[3:]}
        await query.message.reply_text(_quote_format("💬 Format:\n<code>TARGET_NUMBER:MESSAGE_BODY</code>"), parse_mode="HTML")
        return

    if data.startswith("cp:"):
        await query.answer(f"OTP: {data[3:]}", show_alert=True); return

    # ── Keyword confirm / cancel ─────────────────────────────────────
    if data.startswith("kw_confirm:"):
        tid = int(data[11:])
        if chat_id != tid: await query.answer("Not yours!", show_alert=True); return
        keyword = pending_kw_confirm.pop(chat_id, None)
        if not keyword: await query.answer("Session expired.", show_alert=True); return
        await query.answer("✅ Poora search shuru...")
        try: await query.message.delete()
        except: pass
        await _run_full_keyword_search(chat_id, keyword, is_admin, query.message.chat_id, ctx)
        return

    if data.startswith("kw_cancel:"):
        tid = int(data[10:])
        if chat_id != tid: await query.answer(); return
        pending_kw_confirm.pop(chat_id, None)
        await query.answer("❌ Search cancel.", show_alert=True)
        try: await query.message.delete()
        except: pass
        return

    # ── Bulk search ──────────────────────────────────────────────────
    if data.startswith("bulksearch_panel:") and is_admin:
        safe_tag = data[17:]
        orig     = find_tag_by_safe(safe_tag)
        if not orig: await safe_edit(query, _quote_format("❌ Panel nahi mila.")); return
        devs    = await get_panel_devices(orig, is_admin=True)
        online_devs = [d for d in devs if d.status == "online" and d.numbers]
        if not online_devs:
            await safe_edit(query, _quote_format(f"📡 <b>{orig}</b>\n\n😴 Koi online device nahi hai."),
                            reply_markup=InlineKeyboardMarkup([[premium_button("◀ Back", callback_data="bulksearch_back")]])); return
        try: await query.edit_message_text(_quote_format(f"⏳ <b>{orig}</b> panel check ho raha hai...\n{len(online_devs)} online devices..."), parse_mode="HTML")
        except: pass

        sms_tasks = await asyncio.gather(*(get_device_sms(d, limit=20) for d in online_devs))
        today     = datetime.now().date()
        yesterday = today - timedelta(days=1)
        tomorrow  = today + timedelta(days=1)
        matched   = []
        for d, smss in zip(online_devs, sms_tasks):
            for sms in smss:
                body = sms.get("body") or sms.get("message") or sms.get("text") or ""
                otp  = extract_otp(body)
                if otp and otp_in_date_range(sms):
                    matched.append((d, otp, sms_date(sms))); break

        if not matched:
            result_text = (
                f"📡 <b>{orig}</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"🟢 Online checked: {len(online_devs)}\n"
                f"📅 Range: {yesterday} → {tomorrow}\n━━━━━━━━━━━━━━━━━━\n"
                f"❌ Kisi bhi number pe aaj/kal/parson ka otp nahi mila."
            )
            await safe_edit(query, _quote_format(result_text),
                            reply_markup=InlineKeyboardMarkup([[premium_button("◀ Back", callback_data="bulksearch_back"), premium_button("✕ Close", callback_data="close_msg")]])); return

        lines = [
            f"✅ <b>Bulk Search — {orig}</b>", f"━━━━━━━━━━━━━━━━━━",
            f"🟢 Online checked: {len(online_devs)}",
            f"🎯 Otp mila: {len(matched)}", f"📅 Range: {yesterday} / {today} / {tomorrow}",
            f"━━━━━━━━━━━━━━━━━━",
        ]
        num_only = []
        for i, (d, otp, dt) in enumerate(matched, 1):
            ns = d.numbers[0] if d.numbers else d.name
            lines.append(f"{i}. 🟢 {ns}  🔑 {otp}  📅 {dt}")
            num_only.append(ns)
        lines += [f"━━━━━━━━━━━━━━━━━━", f"📋 <b>Numbers only:</b>", "\n".join(num_only)]
        full_text = "\n".join(lines)
        rows = []
        for d, otp, dt in matched[:20]:
            lbl = f"🟢 {d.numbers[0] if d.numbers else d.name}  🔑 {otp}"
            rows.append([premium_button(lbl, callback_data=f"sel:{d.id}")])
        rows.append([premium_button("◀ Back", callback_data="bulksearch_back"), premium_button("✕ Close", callback_data="close_msg")])
        await safe_edit(query, _quote_format(full_text[:4000]), reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "bulksearch_back" and is_admin:
        await safe_edit(query, _quote_format(
            f"{premium_emoji()} <b>Bulk Search</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Panel select karo — sirf online numbers jinka otp aaj/kal/parson aaya ho.\n━━━━━━━━━━━━━━━━━━"
        ), reply_markup=bulksearch_panels_keyboard())
        return

    # ── Firebase list page navigation ────────────────────────────────
    if data.startswith("fb_list_pg:") and is_admin:
        try: pg = int(data[11:])
        except: pg = 0
        await safe_edit(query, _quote_format("📋 <b>All Firebase Dbs</b>"), reply_markup=firebase_list_keyboard(pg))
        return

    if data.startswith("fb_rem_pg:") and is_admin:
        try: pg = int(data[10:])
        except: pg = 0
        await safe_edit(query, _quote_format("🗑 <b>Remove Firebase</b>\nSelect to remove:"), reply_markup=firebase_remove_keyboard(pg))
        return

    if data.startswith("fb_detail:") and is_admin:
        safe_tag = data[10:]
        orig = find_tag_by_safe(safe_tag)
        if not orig: await query.answer("Not found", show_alert=True); return
        url   = DATABASES.get(orig, "N/A")
        devs  = GLOBAL_DEVICE_CACHE.get(orig, [])
        alive, lat = await check_firebase_alive(url)
        lat_str = f"{lat}ms" if lat >= 0 else "timeout"
        text  = (
            f"🗄 <b>Firebase Detail</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Tag    : <code>{html_escape(orig)}</code>\n"
            f"Url    : <code>{html_escape(url)}</code>\n"
            f"Status : {'🟢 Alive' if alive else '🔴 Dead'}  ({lat_str})\n"
            f"Devices: {len(devs)}\n"
            f"Online : {sum(1 for d in devs if d.status=='online')}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        await safe_edit(query, _quote_format(text),
                        reply_markup=InlineKeyboardMarkup([[
                            premium_button("🗑 Remove", callback_data=f"fb_remove:{safe_tag}", style="danger"),
                            premium_button("◀ Back",    callback_data="admin_list_firebase",   style="primary")
                        ]]))
        return

    if data.startswith("fb_remove:") and is_admin:
        safe_tag = data[10:]
        orig = find_tag_by_safe(safe_tag)
        if not orig: await query.answer("Not found", show_alert=True); return
        DATABASES.pop(orig, None)
        GLOBAL_DEVICE_CACHE.pop(orig, None)
        ADMIN_ONLY_TAGS.discard(orig)
        save_data()
        await query.answer(f"✅ {orig} removed!", show_alert=True)
        await safe_edit(query, _quote_format(firebase_mgr_text()), reply_markup=firebase_mgr_keyboard())
        return

    # ── Admin panel callbacks ────────────────────────────────────────
    if data == "admin_refresh" and is_admin:
        await safe_edit(query, _quote_format(admin_panel_text()), reply_markup=admin_keyboard()); return

    if data == "admin_firebase_mgr" and is_admin:
        await safe_edit(query, _quote_format(firebase_mgr_text()), reply_markup=firebase_mgr_keyboard()); return

    if data == "admin_add_firebase" and is_admin:
        pending_action[chat_id] = {"action": "add_bulk_db"}
        await query.message.reply_text(_quote_format(
            "📝 Format:\n<code>TAG=https://your-url.firebaseio.com</code>\nYa sirf URL (auto tag milega).\nMax 20 ek sath.", parse_mode="HTML"
        ), parse_mode="HTML"); return

    if data == "admin_add_firebase_txt" and is_admin:
        pending_action[chat_id] = {"action": "add_bulk_db_txt"}
        await query.message.reply_text(_quote_format("📂 .txt file bhejein jisme Firebase URLs hain."), parse_mode="HTML"); return

    if data == "admin_remove_firebase" and is_admin:
        if not DATABASES:
            await query.answer("No databases!", show_alert=True); return
        await safe_edit(query, _quote_format("🗑 <b>Remove Firebase</b>\nSelect to remove:"), reply_markup=firebase_remove_keyboard())
        return

    if data == "admin_list_firebase" and is_admin:
        await safe_edit(query, _quote_format("📋 <b>All Firebase Dbs</b>"), reply_markup=firebase_list_keyboard())
        return

    if data == "admin_remove_dead" and is_admin:
        try: await query.edit_message_text(_quote_format("🧹 Checking dead Firebase..."), parse_mode="HTML")
        except: pass
        dead_tags = []
        for tag, url in list(DATABASES.items()):
            alive, _ = await check_firebase_alive(url)
            if not alive: dead_tags.append(tag)
        for t in dead_tags:
            DATABASES.pop(t, None)
            GLOBAL_DEVICE_CACHE.pop(t, None)
            ADMIN_ONLY_TAGS.discard(t)
        save_data()
        await safe_edit(query, _quote_format(
            f"🧹 Dead Firebase Removed\n━━━━━━━━━━━━━━━━━━\n"
            f"Removed: <b>{len(dead_tags)}</b>\n"
            + ("\n".join(f"• {t}" for t in dead_tags) or "None")
            + f"\n\nRemaining: <b>{len(DATABASES)}</b>"
        ), reply_markup=firebase_mgr_keyboard()); return

    if data == "admin_maintenance" and is_admin:
        await safe_edit(query, _quote_format(maintenance_text()), reply_markup=maintenance_keyboard()); return

    if data == "admin_mn_toggle" and is_admin:
        MAINTENANCE_MODE = not MAINTENANCE_MODE
        save_data()
        await safe_edit(query, _quote_format(maintenance_text()), reply_markup=maintenance_keyboard()); return

    if data == "admin_mn_text" and is_admin:
        pending_action[chat_id] = {"action": "set_maintenance_text"}
        await query.message.reply_text(_quote_format("✏️ Maintenance mode ka text likho:"), parse_mode="HTML"); return

    if data == "admin_media_mgr" and is_admin:
        await safe_edit(query, _quote_format(media_mgr_text()), reply_markup=media_mgr_keyboard()); return

    if data == "media_set_default" and is_admin:
        pending_action[chat_id] = {"action": "media_set_default"}
        await query.message.reply_text(_quote_format("🖼 Default welcome media bhejein (Photo, Video, ya Gif/Animation).\nCaption media ke sath likhein ya alag se set kar sakte hain."), parse_mode="HTML"); return

    if data == "media_set_caption" and is_admin:
        pending_action[chat_id] = {"action": "media_set_caption"}
        await query.message.reply_text(_quote_format("✏️ Default caption likho (HTML format allowed):"), parse_mode="HTML"); return

    if data == "media_clear_default" and is_admin:
        media_config["default"] = {"type": None, "file_id": None, "caption": ""}
        save_data()
        await query.answer("✅ Default media cleared!", show_alert=True)
        await safe_edit(query, _quote_format(media_mgr_text()), reply_markup=media_mgr_keyboard()); return

    if data == "media_set_user" and is_admin:
        pending_action[chat_id] = {"action": "media_set_user_id"}
        await query.message.reply_text(_quote_format("👤 Jis user ke liye media set karna hai uska User Id bhejein:"), parse_mode="HTML"); return

    if data == "media_clear_user" and is_admin:
        pending_action[chat_id] = {"action": "media_clear_user_id"}
        await query.message.reply_text(_quote_format("🗑 Jis user ka media clear karna hai uska User Id bhejein:"), parse_mode="HTML"); return

    if data == "media_list_user" and is_admin:
        user_media = media_config.get("user", {})
        if not user_media:
            await query.answer("No custom user media set.", show_alert=True); return
        lines = ["👤 <b>User-Specific Media</b>", "━━━━━━━━━━━━━━━━━━"]
        for uid, mc in list(user_media.items())[:20]:
            uname = all_users.get(int(uid), {}).get("name", "Unknown")
            lines.append(f"• {html_escape(uname)} (<code>{uid}</code>) — {mc.get('type','none')}")
        await safe_edit(query, _quote_format("\n".join(lines)), reply_markup=media_mgr_keyboard()); return

    if data == "admin_set_logs" and is_admin:
        pending_action[chat_id] = {"action": "set_logs_chat"}
        await query.message.reply_text(_quote_format("📋 Logs bhejne wale chat ka Chat Id bhejein (ya is chat ke liye /setlogschat use karein):"), parse_mode="HTML"); return

    if data == "admin_backup_mgr" and is_admin:
        await safe_edit(query, _quote_format(backup_mgr_text()), reply_markup=backup_mgr_keyboard()); return

    if data == "admin_set_backup_chat" and is_admin:
        pending_action[chat_id] = {"action": "set_backup_chat"}
        await query.message.reply_text(_quote_format("💾 Backup bhejne wale chat ka Chat Id bhejein:"), parse_mode="HTML"); return

    if data == "admin_set_backup_interval" and is_admin:
        pending_action[chat_id] = {"action": "set_backup_interval"}
        await query.message.reply_text(_quote_format(f"⏱ Backup interval set karein (hours mein). Current: <b>{BACKUP_INTERVAL_HOURS}</b>h\nExample: <code>6</code>"), parse_mode="HTML"); return

    if data == "admin_backup_now" and is_admin:
        await query.answer("💾 Backup chal raha hai...")
        await do_backup(requester_chat=chat_id)
        await query.message.reply_text(_quote_format("✅ Backup bhej diya!"), parse_mode="HTML"); return

    if data == "admin_manage_admins" and is_admin:
        await safe_edit(query, _quote_format(manage_admins_text()), reply_markup=manage_admins_keyboard()); return

    if data == "admin_add_admin" and is_admin:
        pending_action[chat_id] = {"action": "add_admin"}
        await query.message.reply_text(_quote_format("➕ Naye admin ka User Id bhejein:"), parse_mode="HTML"); return

    if data == "admin_remove_admin" and is_admin:
        pending_action[chat_id] = {"action": "remove_admin"}
        await query.message.reply_text(_quote_format(
            "➖ Remove karne wale admin ka User Id bhejein:\n"
            + "\n".join(f"• <code>{a}</code>" for a in ADMIN_IDS)
        ), parse_mode="HTML"); return

    if data == "admin_force_join" and is_admin:
        await safe_edit(query, _quote_format(admin_force_join_text()), reply_markup=admin_force_join_keyboard()); return

    if data == "admin_fj_toggle" and is_admin:
        FORCE_JOIN_ENABLED = not FORCE_JOIN_ENABLED
        save_data()
        await safe_edit(query, _quote_format(admin_force_join_text()), reply_markup=admin_force_join_keyboard()); return

    if data == "admin_fj_add" and is_admin:
        pending_action[chat_id] = {"action": "add_force_join_channel"}
        await query.message.reply_text(_quote_format(
            "📢 <b>Add Force Join Channel</b>\n"
            "Public: <code>@channelusername</code>\n"
            "Private: <code>-100xxx | https://t.me/+xxx</code>"
        ), parse_mode="HTML"); return

    if data == "admin_fj_list" and is_admin:
        lines = ["📋 <b>Force Join Channels</b>", "━━━━━━━━━━━━━━━━━━"]
        if not FORCE_JOIN_CHANNELS:
            lines.append("❌ No channels.")
        else:
            for i, cd in enumerate(FORCE_JOIN_CHANNELS, 1):
                ch  = html_escape(str(cd.get("channel","") or ""))
                url = str(cd.get("url","") or "")
                lines.append(f"<b>{i}.</b> <code>{ch}</code>\n   🔗 {html_escape(url or get_force_join_url(cd))}")
        await safe_edit(query, _quote_format("\n".join(lines)), reply_markup=admin_force_join_keyboard()); return

    if data == "admin_fj_remove" and is_admin:
        if not FORCE_JOIN_CHANNELS:
            await query.answer("No channels!", show_alert=True); return
        rows = []
        for i, cd in enumerate(FORCE_JOIN_CHANNELS):
            ch = str(cd.get("channel","") or "")
            if ch:
                rows.append([premium_button(f"Remove {i+1}. {ch}", callback_data=f"admin_fj_del:{i}", style="danger")])
        rows.append([premium_button("◀ Back", callback_data="admin_force_join", style="primary")])
        await safe_edit(query, _quote_format("🗑 Remove Force Join Channel:"), reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("admin_fj_del:") and is_admin:
        try: idx = int(data.split(":",1)[1])
        except: await query.answer("Invalid.", show_alert=True); return
        if 0 <= idx < len(FORCE_JOIN_CHANNELS):
            removed = FORCE_JOIN_CHANNELS.pop(idx)
            save_data()
            await query.answer(f"✅ Removed {removed.get('channel','')}", show_alert=True)
        await safe_edit(query, _quote_format(admin_force_join_text()), reply_markup=admin_force_join_keyboard()); return

    if data == "admin_referral" and is_admin:
        await safe_edit(query, _quote_format(admin_referral_text()), reply_markup=admin_referral_keyboard()); return

    if data == "admin_ref_toggle" and is_admin:
        REFERRAL_ENABLED = not REFERRAL_ENABLED
        save_data()
        await safe_edit(query, _quote_format(admin_referral_text()), reply_markup=admin_referral_keyboard()); return

    if data == "admin_ref_required" and is_admin:
        pending_action[chat_id] = {"action": "set_required_referrals"}
        await query.message.reply_text(_quote_format(f"👥 Required referrals bhejein. Current: <b>{REQUIRED_REFERRALS}</b>"), parse_mode="HTML"); return

    if data == "admin_ref_stats" and is_admin:
        top   = sorted(((uid, referral_count(uid)) for uid in all_users), key=lambda x: x[1], reverse=True)
        total = sum(c for _, c in top)
        lines = ["🎁 <b>Referral Stats</b>", "━━━━━━━━━━━━━━━━━━", f"Total qualified: <b>{total}</b>"]
        for uid, cnt in top[:20]:
            if cnt <= 0: continue
            name = html_escape(all_users.get(uid,{}).get("name","Unknown"))
            lines.append(f"• {name} — <code>{uid}</code> — <b>{cnt}</b>")
        await safe_edit(query, _quote_format("\n".join(lines)), reply_markup=admin_referral_keyboard()); return

    if data == "admin_setadmin_list" and is_admin:
        await safe_edit(query, _quote_format(admin_setadmin_text()), reply_markup=admin_setadmin_keyboard()); return

    if data.startswith("toggleadmin:") and is_admin:
        safe_tag = data[12:]
        orig = find_tag_by_safe(safe_tag)
        if not orig: await query.answer("Panel not found!", show_alert=True); return
        if orig in ADMIN_ONLY_TAGS:
            ADMIN_ONLY_TAGS.discard(orig)
            await query.answer(f"🔓 '{orig}' now visible!", show_alert=True)
        else:
            ADMIN_ONLY_TAGS.add(orig)
            await query.answer(f"🔒 '{orig}' admin-only!", show_alert=True)
        save_data()
        await safe_edit(query, _quote_format(admin_setadmin_text()), reply_markup=admin_setadmin_keyboard()); return

    if data == "admin_grant_access" and is_admin:
        pending_action[chat_id] = {"action": "grant_access_pipe"}
        await query.message.reply_text(_quote_format("✅ Grant karne wale ka User Id bhejein:"), parse_mode="HTML"); return

    if data == "admin_revoke_access" and is_admin:
        pending_action[chat_id] = {"action": "revoke_access_pipe"}
        await query.message.reply_text(_quote_format("❌ Revoke karne wale ka User Id bhejein:"), parse_mode="HTML"); return

    if data == "admin_access_list" and is_admin:
        if not allowed_users:
            await query.message.reply_text(_quote_format("🔐 Koi allowed user nahi."), parse_mode="HTML"); return
        lines = ["🔐 <b>Allowed Users:</b>"]
        for uid in list(allowed_users)[:30]:
            ud = all_users.get(uid, {})
            lines.append(f"• {html_escape(ud.get('name','Unknown'))} (@{ud.get('username','')}) — <code>{uid}</code>")
        await query.message.reply_text(_quote_format("\n".join(lines)), parse_mode="HTML"); return

    if data == "admin_users" and is_admin:
        lines = [f"👥 <b>Total Users: {len(all_users)}</b>"]
        for cnt, (uid, ud) in enumerate(list(all_users.items())[:30], 1):
            icon = "✅" if has_access(uid) else "🚫"
            lines.append(f"{cnt}. {icon} {html_escape(ud.get('name','Unknown'))} (<code>{uid}</code>)")
        await query.message.reply_text(_quote_format("\n".join(lines)), parse_mode="HTML"); return

    if data == "admin_broadcast" and is_admin:
        pending_action[chat_id] = {"action": "broadcast_msg"}
        await query.message.reply_text(_quote_format("📢 Broadcast message likhke bhejein:"), parse_mode="HTML"); return

    if data == "admin_restart" and is_admin:
        await query.answer("🔁 Restarting...", show_alert=True)
        await query.message.reply_text(_quote_format("🔁 Bot restart ho raha hai..."), parse_mode="HTML")
        tlog("Admin triggered restart.")
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return


# ═══════════════════════════════════════════════════════
#  DOCUMENT HANDLER
# ═══════════════════════════════════════════════════════
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    is_admin = (chat_id in ADMIN_IDS)

    if all_users.get(chat_id, {}).get("banned"): return

    if not is_admin:
        if MAINTENANCE_MODE:
            await update.message.reply_text(_quote_format(MAINTENANCE_TEXT), parse_mode="HTML"); return
        ready, reason = await gate_state(ctx.bot, chat_id)
        if not ready:
            await send_gate_message(update, ctx, reason); return
    if not has_access(chat_id):
        await update.message.reply_text(_quote_format(NO_ACCESS_TEXT), parse_mode="HTML"); return

    doc   = update.message.document
    if not doc: return
    fname = doc.file_name or ""
    mime  = doc.mime_type or ""
    is_txt = fname.lower().endswith(".txt") or "text" in mime.lower()

    # ── Admin .txt firebase import ───────────────────────────────────
    act = pending_action.get(chat_id, {}).get("action", "")
    if is_admin and act in ("add_bulk_db", "add_bulk_db_txt"):
        if not is_txt:
            await update.message.reply_text(_quote_format("⚠️ Sirf .txt file upload karein."), parse_mode="HTML"); return
        if doc.file_size and doc.file_size > 500_000:
            await update.message.reply_text(_quote_format("⚠️ File bahut badi hai (max 500kb)."), parse_mode="HTML"); return
        wait = await update.message.reply_text(_quote_format("📂 File read ho rahi hai..."), parse_mode="HTML")
        try:
            tg_file = await ctx.bot.get_file(doc.file_id)
            buf = io.BytesIO()
            await tg_file.download_to_memory(buf)
            content = buf.getvalue().decode("utf-8-sig", errors="ignore")
        except Exception as e:
            await wait.edit_text(_quote_format(f"❌ File error: {e}")); return
        try: await wait.delete()
        except: pass
        await handle_bulk_panel_import(update, ctx, content, source_label=f"TXT: {fname}")
        return

    # ── Bulk number search from .txt ─────────────────────────────────
    if not is_txt:
        await update.message.reply_text(_quote_format("⚠️ Sirf .txt file bhejein jisme numbers hon."), parse_mode="HTML"); return
    if doc.file_size and doc.file_size > 500_000:
        await update.message.reply_text(_quote_format("⚠️ File bahut badi (max 500kb)."), parse_mode="HTML"); return

    wait = await update.message.reply_text(_quote_format("📂 File padh raha hoon..."), parse_mode="HTML")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        content = buf.getvalue().decode("utf-8", errors="ignore")
    except Exception as e:
        await wait.edit_text(_quote_format(f"❌ File download error: {e}")); return

    raw_queries = re.split(r"[\n,;\s]+", content.strip())
    queries     = list(dict.fromkeys(re.sub(r"\D", "", q) for q in raw_queries if len(re.sub(r"\D", "", q)) >= 8))
    if not queries:
        await wait.edit_text(_quote_format("❌ File mein koi valid number nahi mila.")); return

    await wait.edit_text(_quote_format(f"📂 <b>{len(queries)}</b> numbers mile. Search ho raha hai..."), parse_mode="HTML")
    devices = await get_all_devices(is_admin)
    dev_idx = {}
    for d in devices:
        for n in d.numbers:
            c = re.sub(r"\D", "", n)
            dev_idx.setdefault(c, []).append(d)

    results_found, results_missing = [], []
    for q in queries:
        matched = []
        for sn, devs in dev_idx.items():
            if q in sn or sn in q:
                for d in devs:
                    if d not in matched: matched.append(d)
        if matched: results_found.append((q, matched))
        else:       results_missing.append(q)

    try: await wait.delete()
    except: pass

    fc, mc = len(results_found), len(results_missing)
    summary = (
        f"🎯 <b>File Bulk Search Results</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📋 Total: <b>{len(queries)}</b>  ✅ Mila: <b>{fc}</b>  ❌ Nahi: <b>{mc}</b>\n━━━━━━━━━━━━━━━━━━"
    )
    rows = []
    for q, devs in results_found[:20]:
        d    = devs[0]
        icon = "🟢" if d.status == "online" else "🔴"
        lbl  = d.numbers[0] if d.numbers else d.name
        rows.append([premium_button(f"{icon} {q} → [{d.db_tag}] {lbl}", callback_data=f"sel:{d.id}")])
    rows.append([premium_button("✕ Close", callback_data="close_msg")])
    await update.message.reply_text(_quote_format(summary), reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")

    file_lines = [
        f"=== File Bulk Search ===", f"Total: {len(queries)} | Mila: {fc} | Nahi: {mc}", f"{'='*40}", "",
        "✅ Mila (Found):",
    ]
    for q, devs in results_found:
        d = devs[0]
        file_lines.append(f"  {q}  →  [{d.db_tag}] {d.numbers[0] if d.numbers else d.name}  ({'ONLINE' if d.status=='online' else 'offline'})")
    file_lines += ["", "❌ Nahi Mila:", *[f"  {q}" for q in results_missing]]
    bio      = io.BytesIO("\n".join(file_lines).encode("utf-8"))
    bio.name = "bulk_result.txt"
    await update.message.reply_document(document=bio, filename="bulk_result.txt",
                                         caption=_quote_format(f"📄 Full result — {fc} mila, {mc} nahi mila"),
                                         parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  MEDIA MESSAGE HANDLER (admin sets welcome media)
# ═══════════════════════════════════════════════════════
async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo / video / animation when admin is in media-set pending action."""
    chat_id  = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return

    act = pending_action.get(chat_id, {}).get("action", "")
    msg = update.message

    if act == "media_set_default":
        file_id, mtype = None, None
        if msg.photo:
            file_id, mtype = msg.photo[-1].file_id, "photo"
        elif msg.video:
            file_id, mtype = msg.video.file_id, "video"
        elif msg.animation:
            file_id, mtype = msg.animation.file_id, "animation"
        if not file_id:
            await msg.reply_text(_quote_format("⚠️ Photo, video ya gif bhejein."), parse_mode="HTML"); return
        caption = msg.caption or media_config["default"].get("caption", "")
        media_config["default"] = {"type": mtype, "file_id": file_id, "caption": caption}
        save_data()
        pending_action.pop(chat_id, None)
        await msg.reply_text(_quote_format(f"✅ Default media set! Type: {mtype}"), parse_mode="HTML"); return

    if act == "media_set_user_media":
        target_id = pending_action[chat_id].get("target_id")
        file_id, mtype = None, None
        if msg.photo:
            file_id, mtype = msg.photo[-1].file_id, "photo"
        elif msg.video:
            file_id, mtype = msg.video.file_id, "video"
        elif msg.animation:
            file_id, mtype = msg.animation.file_id, "animation"
        if not file_id:
            await msg.reply_text(_quote_format("⚠️ Photo, video ya gif bhejein."), parse_mode="HTML"); return
        media_config.setdefault("user", {})[str(target_id)] = {
            "type": mtype, "file_id": file_id, "caption": msg.caption or ""
        }
        save_data()
        pending_action.pop(chat_id, None)
        name = all_users.get(int(target_id), {}).get("name", "Unknown")
        await msg.reply_text(_quote_format(f"✅ User {name} ({target_id}) ke liye media set! Type: {mtype}"), parse_mode="HTML"); return


# ═══════════════════════════════════════════════════════
#  TEXT HANDLER
# ═══════════════════════════════════════════════════════
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global DATABASES, FORCE_JOIN_CHANNELS, REQUIRED_REFERRALS
    global MAINTENANCE_MODE, MAINTENANCE_TEXT, LOGS_CHAT_ID
    global BACKUP_CHAT_ID, BACKUP_INTERVAL_HOURS, ADMIN_IDS

    chat_id  = update.effective_chat.id
    text     = (update.message.text or "").strip()
    is_admin = (chat_id in ADMIN_IDS)

    if all_users.get(chat_id, {}).get("banned") or is_spamming(chat_id): return

    if not is_admin:
        if MAINTENANCE_MODE and text not in ("Admin Panel",):
            await update.message.reply_text(_quote_format(MAINTENANCE_TEXT), parse_mode="HTML"); return
        ready, reason = await gate_state(ctx.bot, chat_id)
        if not ready:
            await send_gate_message(update, ctx, reason); return

    # ── Pending action pipeline ──────────────────────────────────────
    if chat_id in pending_action:
        act_data = pending_action.pop(chat_id)
        action   = act_data["action"]

        if action == "set_maintenance_text" and is_admin:
            MAINTENANCE_TEXT = text
            save_data()
            await update.message.reply_text(_quote_format(f"✅ Maintenance text updated:\n\n{escape(text)}"), parse_mode="HTML"); return

        if action == "set_logs_chat" and is_admin:
            try:
                LOGS_CHAT_ID = int(text.strip())
                save_data()
                await update.message.reply_text(_quote_format(f"✅ Logs chat set: <code>{LOGS_CHAT_ID}</code>"), parse_mode="HTML")
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Valid chat id daalein.")); return
            return

        if action == "set_backup_chat" and is_admin:
            try:
                BACKUP_CHAT_ID = int(text.strip())
                save_data()
                await update.message.reply_text(_quote_format(f"✅ Backup chat set: <code>{BACKUP_CHAT_ID}</code>"), parse_mode="HTML")
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Valid chat id daalein.")); return
            return

        if action == "set_backup_interval" and is_admin:
            try:
                BACKUP_INTERVAL_HOURS = max(1, int(text.strip()))
                save_data()
                await update.message.reply_text(_quote_format(f"✅ Backup interval: <b>{BACKUP_INTERVAL_HOURS}</b> hours"), parse_mode="HTML")
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Number daalein (hours).")); return
            return

        if action == "add_admin" and is_admin:
            try:
                new_id = int(text.strip())
                ADMIN_IDS.add(new_id)
                save_data()
                await update.message.reply_text(_quote_format(f"✅ Admin added: <code>{new_id}</code>"), parse_mode="HTML",
                                                 reply_markup=manage_admins_keyboard())
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Valid user id daalein.")); return
            return

        if action == "remove_admin" and is_admin:
            try:
                rem_id = int(text.strip())
                if rem_id in ADMIN_IDS:
                    ADMIN_IDS.discard(rem_id)
                    save_data()
                    await update.message.reply_text(_quote_format(f"✅ Admin removed: <code>{rem_id}</code>"), parse_mode="HTML",
                                                     reply_markup=manage_admins_keyboard())
                else:
                    await update.message.reply_text(_quote_format("❌ Ye id admin mein nahi hai."), parse_mode="HTML")
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Valid user id daalein.")); return
            return

        if action == "media_set_caption" and is_admin:
            media_config["default"]["caption"] = text
            save_data()
            await update.message.reply_text(_quote_format("✅ Default caption updated!"), parse_mode="HTML"); return

        if action == "media_set_user_id" and is_admin:
            try:
                target_id = int(text.strip())
                pending_action[chat_id] = {"action": "media_set_user_media", "target_id": target_id}
                await update.message.reply_text(_quote_format(f"🖼 User <code>{target_id}</code> ke liye media bhejein (Photo/Video/Gif):"), parse_mode="HTML")
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Valid user id daalein.")); return
            return

        if action == "media_clear_user_id" and is_admin:
            try:
                target_id = str(int(text.strip()))
                media_config.get("user", {}).pop(target_id, None)
                save_data()
                await update.message.reply_text(_quote_format(f"✅ User {target_id} ka media cleared!"), parse_mode="HTML")
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Valid user id daalein.")); return
            return

        if action == "add_force_join_channel" and is_admin:
            value = text.strip()
            if "|" in value:
                channel, url = [x.strip() for x in value.split("|", 1)]
            else:
                channel, url = value, ""
            if not (channel.startswith("@") or channel.startswith("-100")):
                await update.message.reply_text(_quote_format("❌ Invalid channel.\nPublic: @channel\nPrivate: -100xxx | https://t.me/+xxx"), parse_mode="HTML"); return
            if channel.startswith("-100") and not url:
                await update.message.reply_text(_quote_format("❌ Private channel ke liye invite url zaroori hai."), parse_mode="HTML"); return
            existing = {str(cd.get("channel","")).strip() for cd in FORCE_JOIN_CHANNELS if isinstance(cd, dict)}
            if channel in existing:
                await update.message.reply_text(_quote_format(f"⚠️ Already added: <code>{html_escape(channel)}</code>"), parse_mode="HTML"); return
            FORCE_JOIN_CHANNELS.append({"channel": channel, "url": url})
            save_data()
            await update.message.reply_text(_quote_format(
                f"✅ Channel added: <code>{html_escape(channel)}</code>\nTotal: <b>{len(FORCE_JOIN_CHANNELS)}</b>"
            ), parse_mode="HTML", reply_markup=admin_force_join_keyboard()); return

        if action == "set_required_referrals" and is_admin:
            try:
                REQUIRED_REFERRALS = max(0, int(text.strip()))
                save_data()
                await update.message.reply_text(_quote_format(f"✅ Required referrals: <b>{REQUIRED_REFERRALS}</b>"), parse_mode="HTML", reply_markup=admin_referral_keyboard())
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Number daalein.")); return
            return

        if action == "search_num_pipe":
            if not has_access(chat_id):
                await update.message.reply_text(_quote_format(NO_ACCESS_TEXT), parse_mode="HTML"); return
            devices = await get_all_devices(is_admin)
            cq      = re.sub(r"\D", "", text)
            found   = [d for d in devices if any(cq in re.sub(r"\D","",n) for n in d.numbers)
                       and (is_admin or d.db_tag not in ADMIN_ONLY_TAGS)]
            if found:
                rows = [[premium_button(f"📱 [{fd.db_tag}] {fd.numbers[0] if fd.numbers else fd.name}", callback_data=f"sel:{fd.id}")] for fd in found[:15]]
                await update.message.reply_text(_quote_format("🎯 Matched Targets:"), reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
            else:
                await update.message.reply_text(_quote_format("❌ Number nahi mila kisi bhi database mein."), parse_mode="HTML")
            return

        if action == "bulk_search_pipe":
            if not has_access(chat_id):
                await update.message.reply_text(_quote_format(NO_ACCESS_TEXT), parse_mode="HTML"); return
            queries = list(dict.fromkeys(re.sub(r"\D","",q) for q in re.split(r"[\n,;\s]+", text.strip()) if len(re.sub(r"\D","",q)) >= 8))
            if not queries:
                await update.message.reply_text(_quote_format("❌ Koi valid number nahi mila (min 8 digits)."), parse_mode="HTML"); return
            devices = await get_all_devices(is_admin)
            dev_idx = {}
            for d in devices:
                for n in d.numbers:
                    c = re.sub(r"\D","",n)
                    dev_idx.setdefault(c, []).append(d)
            wait = await update.message.reply_text(_quote_format(f"🔍 <b>{len(queries)}</b> numbers search ho rahe hain..."), parse_mode="HTML")
            rf, rm = [], []
            for q in queries:
                matched = []
                for sn, devs in dev_idx.items():
                    if q in sn or sn in q:
                        for d in devs:
                            if d not in matched: matched.append(d)
                if matched: rf.append((q, matched))
                else:       rm.append(q)
            try: await wait.delete()
            except: pass
            fc, mc = len(rf), len(rm)
            summary = (
                f"🎯 <b>Bulk Search Results</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"📋 Total: <b>{len(queries)}</b>  ✅ Mila: <b>{fc}</b>  ❌ Nahi: <b>{mc}</b>\n━━━━━━━━━━━━━━━━━━"
            )
            rows = []
            for q, devs in rf[:20]:
                d = devs[0]; icon = "🟢" if d.status=="online" else "🔴"; lbl = d.numbers[0] if d.numbers else d.name
                rows.append([premium_button(f"{icon} {q} → [{d.db_tag}] {lbl}", callback_data=f"sel:{d.id}")])
            rows.append([premium_button("✕ Close", callback_data="close_msg")])
            await update.message.reply_text(_quote_format(summary), reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")
            file_lines = [
                f"=== Bulk Search ===", f"Total: {len(queries)} | Mila: {fc} | Nahi: {mc}", f"{'='*40}", "",
                "✅ Mila:", *[f"  {q}  →  [{d.db_tag}] {d.numbers[0] if d.numbers else d.name}  ({'ONLINE' if d.status=='online' else 'offline'})" for q, devs in rf for d in [devs[0]]],
                "", "❌ Nahi:", *[f"  {q}" for q in rm]
            ]
            bio = io.BytesIO("\n".join(file_lines).encode("utf-8")); bio.name = "bulk_search_result.txt"
            await update.message.reply_document(document=bio, filename="bulk_search_result.txt",
                                                 caption=_quote_format(f"📄 Full result — {fc} mila, {mc} nahi"), parse_mode="HTML")
            return

        if action == "keyword_search_pipe":
            if not has_access(chat_id):
                await update.message.reply_text(_quote_format(NO_ACCESS_TEXT), parse_mode="HTML"); return
            keyword = text.strip().lower()
            if not keyword:
                await update.message.reply_text(_quote_format("❌ Keyword empty hai."), parse_mode="HTML"); return
            esc_kw = html_escape(keyword)
            wait   = await update.message.reply_text(_quote_format(f"🔎 Sample dhundh raha hoon — <code>{esc_kw}</code>..."), parse_mode="HTML")
            devices = await get_all_devices(is_admin)
            devs_num = [d for d in devices if d.numbers][:20]
            kw_sem = asyncio.Semaphore(10)
            async def fsafe(d):
                async with kw_sem: return await get_device_sms(d, limit=SMS_LIMIT)
            prev_results = await asyncio.gather(*(fsafe(d) for d in devs_num), return_exceptions=True)
            sample_sms = sample_dev = sample_body = None
            for d, smss in zip(devs_num, prev_results):
                if isinstance(smss, Exception) or not smss: continue
                for s in smss:
                    b = s.get("body") or s.get("message") or s.get("text") or ""
                    if keyword in b.lower():
                        sample_sms = s; sample_dev = d; sample_body = b; break
                if sample_sms: break
            try: await wait.delete()
            except: pass
            if not sample_sms:
                prev_text = (
                    f"<b>Keyword Preview: <code>{esc_kw}</code></b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ Pehle 20 devices mein keyword nahi mila.\nPhir bhi poora search chalaye?\n━━━━━━━━━━━━━━━━━━"
                )
            else:
                nl = sample_dev.numbers[0] if sample_dev.numbers else sample_dev.name
                highlighted = sample_body.replace(keyword, f"【{keyword}】")[:300]
                prev_text = (
                    f"<b>Keyword Preview: <code>{esc_kw}</code></b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"Number: {html_escape(nl)}\nFrom: {html_escape(sample_sms.get('sender','Unknown'))}\nDate: {sms_date(sample_sms)}\n"
                    f"━━━━━━━━━━━━━━━━━━\n<b>Message:</b>\n{html_escape(highlighted)}{'...' if len(sample_body)>300 else ''}\n━━━━━━━━━━━━━━━━━━\n✅ Confirm karein?"
                )
            pending_kw_confirm[chat_id] = keyword
            confirm_kb = InlineKeyboardMarkup([[
                premium_button("✅ Haan, Poora Search", callback_data=f"kw_confirm:{chat_id}"),
                premium_button("❌ Cancel",             callback_data=f"kw_cancel:{chat_id}"),
            ]])
            await update.message.reply_text(_quote_format(prev_text), reply_markup=confirm_kb, parse_mode="HTML")
            return

        if action == "execute_ss":
            dev_id  = act_data["dev_id"]
            devices = await get_all_devices(is_admin)
            device  = next((d for d in devices if d.id == dev_id), None)
            if not device: return
            try:
                to_n, body_m = text.split(":", 1)
                ok = await fb_patch(
                    f"device_commands/{device.id}",
                    {"sendSms": {"to": to_n.strip(), "body": body_m.strip()},
                     "timestamp": int(time.time()*1000), "status": "pending"},
                    device.base_url
                )
                await update.message.reply_text(_quote_format("✅ Sms sent!" if ok else "❌ Failed."), parse_mode="HTML")
            except:
                await update.message.reply_text(_quote_format("❌ Invalid format. Use: TARGET:MESSAGE"), parse_mode="HTML")
            return

        if action == "add_bulk_db" and is_admin:
            await handle_bulk_panel_import(update, ctx, text, source_label="message"); return

        if action == "grant_access_pipe" and is_admin:
            try:
                tid = int(text.strip())
                grant_access(tid)
                name = all_users.get(tid, {}).get("name", "Unknown")
                await update.message.reply_text(_quote_format(f"✅ Access granted to {name} ({tid})"), parse_mode="HTML")
                try:
                    await ctx.bot.send_message(tid, _quote_format("✅ <b>Access Granted!</b>\n/start karein."), parse_mode="HTML")
                except: pass
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Valid user id daalein.")); return
            return

        if action == "revoke_access_pipe" and is_admin:
            try:
                tid = int(text.strip())
                revoke_access(tid)
                await update.message.reply_text(_quote_format(f"❌ Access revoked for {tid}"), parse_mode="HTML")
            except ValueError:
                await update.message.reply_text(_quote_format("❌ Valid user id daalein.")); return
            return

        if action == "broadcast_msg" and is_admin:
            sc = bc = 0
            for uid in list(all_users.keys()):
                try:
                    await ctx.bot.send_message(int(uid), _quote_format(f"📢 Admin Broadcast\n\n{escape(text)}"), parse_mode="HTML")
                    sc += 1; await asyncio.sleep(0.05)
                except (Forbidden, TelegramError): bc += 1
                except: pass
            await update.message.reply_text(_quote_format(f"✅ Broadcast done!\nSent: {sc} | Blocked: {bc}"), parse_mode="HTML")
            return

        # Unknown/unmatched action — stop here, don't fall into menu handlers
        return

    # ── Menu text buttons ────────────────────────────────────────────
    if text not in ("Admin Panel", "Bot Status") and not has_access(chat_id):
        await update.message.reply_text(_quote_format(NO_ACCESS_TEXT), parse_mode="HTML"); return

    if text in ("Devices List",):
        devices = await get_all_devices(is_admin)
        pg      = user_page.get(chat_id, 0)
        await update.message.reply_text(
            _quote_format(device_list_header(devices, pg)),
            reply_markup=device_list_keyboard(devices, pg), parse_mode="HTML"
        ); return

    if text in ("All Panels",):
        vis = [t for t in DATABASES if is_admin or t not in ADMIN_ONLY_TAGS]
        td  = sum(len(GLOBAL_DEVICE_CACHE.get(t,[])) for t in vis)
        go  = sum(sum(1 for d in GLOBAL_DEVICE_CACHE.get(t,[]) if d.status=="online") for t in vis)
        hdr = (
            f"{premium_emoji()} <b>All Panels</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📡 Panels: {len(vis)}  |  📱 Devices: {td}\n"
            f"🟢 Online: {go}  🔴 Offline: {td-go}\n━━━━━━━━━━━━━━━━━━\nPanel click karo → numbers dikhenge:"
        )
        await update.message.reply_text(_quote_format(hdr), reply_markup=panels_list_keyboard(is_admin=is_admin), parse_mode="HTML"); return

    if text in ("Search Number",):
        pending_action[chat_id] = {"action": "search_num_pipe"}
        await update.message.reply_text(_quote_format("🔍 Number ya text bhejein:"), parse_mode="HTML"); return

    if text in ("Keyword Search",):
        pending_action[chat_id] = {"action": "keyword_search_pipe"}
        await update.message.reply_text(_quote_format(
            "🔎 <b>Keyword Search</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Woh keyword bhejein jo check karna hai.\n"
            "Bot saare numbers ke latest sms check karega.\n"
            "Jis number mein keyword <b>Nahi</b> hoga woh dikhega.\n━━━━━━━━━━━━━━━━━━"
        ), parse_mode="HTML"); return

    if text in ("Bulk Search",) and is_admin:
        await update.message.reply_text(_quote_format(
            "🔍 <b>Bulk Search</b>\n━━━━━━━━━━━━━━━━━━\nPanel select karo — sirf wahi numbers jo online hain aur aaj/kal/parson otp aaya ho.\n━━━━━━━━━━━━━━━━━━"
        ), reply_markup=bulksearch_panels_keyboard(), parse_mode="HTML"); return

    if text in ("Number Search",):
        pending_action[chat_id] = {"action": "bulk_search_pipe"}
        await update.message.reply_text(_quote_format(
            "📋 <b>Bulk Number Search</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Ek ya zyada numbers bhejein (enter/comma se alag):\n\n"
            "<code>9876543210\n9123456789</code>\n━━━━━━━━━━━━━━━━━━\nBot sabhi panels mein search karega."
        ), parse_mode="HTML"); return

    if text in ("Panel Status",):
        lines = ["📊 <b>Panel Status</b>", "━━━━━━━━━━━━━━━━━━"]
        gt = go = 0
        for tag in sorted(DATABASES.keys()):
            if not is_admin and tag in ADMIN_ONLY_TAGS: continue
            cached = GLOBAL_DEVICE_CACHE.get(tag, [])
            total  = len(cached)
            online = sum(1 for d in cached if d.status == "online")
            gt += total; go += online
            bar  = "🟢" if online > 0 else ("🔴" if total > 0 else "⚫")
            nc   = sum(len(d.numbers) for d in cached if d.numbers)
            lines.append(f"{bar} <b>{tag}</b> — Total: <code>{total}</code> | Online: <code>{online}</code> | Numbers: <code>{nc}</code>")
        lines += [f"━━━━━━━━━━━━━━━━━━", f"📱 <b>Grand Total:</b> <code>{gt}</code>  🟢 <b>Online:</b> <code>{go}</code>  🔴 <b>Offline:</b> <code>{gt-go}</code>"]
        await update.message.reply_text(_quote_format("\n".join(lines)), parse_mode="HTML"); return

    if text in ("Recent OTPs",):
        devices = [d for d in await get_all_devices(is_admin) if d.numbers]
        def sms_ts(sm: dict) -> float:
            try:
                ts = float(sm.get("timestamp", 0))
                return ts/1000 if ts > 1e11 else ts
            except: return 0.0
        best: dict[str, tuple] = {}
        sms_tasks = await asyncio.gather(*(get_device_sms(d, limit=10) for d in devices))
        for d, smss in zip(devices, sms_tasks):
            nl = d.numbers[0] if d.numbers else d.id[:10]
            for sm in smss:
                body = sm.get("body") or sm.get("message") or sm.get("text") or ""
                otp  = extract_otp(body)
                if not otp: continue
                ts = sms_ts(sm)
                if nl not in best or ts > best[nl][2]: best[nl] = (otp, sm, ts)
        out = "📋 Recent Otps (Latest Per Number):\n━━━━━━━━━━━━━━━━━━\n"
        if not best:
            out += "📭 Koi fresh otp nahi mila."
        else:
            for nl, (otp, sm, _) in sorted(best.items(), key=lambda x: x[1][2], reverse=True)[:25]:
                out += f"🔑 <code>{otp}</code>  ·  {nl}  ·  {sms_date(sm)}\n"
        await update.message.reply_text(_quote_format(out), parse_mode="HTML"); return

    if text in ("Bot Status",) and is_admin:
        lines = [
            f"⚙️ <b>Bot Status</b>\n{'─'*22}\n"
            f"🛰  <b>Poll</b>      <code>{POLL_INTERVAL}s</code>\n"
            f"📦  <b>SMS Limit</b> <code>{SMS_LIMIT}</code>\n"
            f"🗄  <b>DBs</b>       <code>{len(DATABASES)}</code>\n"
            f"✅  <b>Allowed</b>   <code>{len(allowed_users)}</code>\n"
            f"{'─'*22}"
        ]
        for tag in sorted(DATABASES.keys()):
            cached = GLOBAL_DEVICE_CACHE.get(tag, [])
            cnt    = sum(len(d.numbers) for d in cached if d.numbers)
            lines.append(f"{'🟢' if cnt > 0 else '🔴'} <b>{html_escape(tag)}:</b> <code>{cnt}</code> numbers")
        await update.message.reply_text(_quote_format("\n".join(lines)), parse_mode="HTML"); return

    if text in ("Admin Panel",) and is_admin:
        await update.message.reply_text(_quote_format(admin_panel_text()), reply_markup=admin_keyboard(), parse_mode="HTML"); return


# ═══════════════════════════════════════════════════════
#  OTP AUTO-FORWARD
# ═══════════════════════════════════════════════════════
async def _forward_otp_only(device: Device, sms: dict) -> None:
    body = sms.get("body") or sms.get("message") or sms.get("text") or ""
    if not body: return
    otp = extract_otp(body)
    if not otp: return
    msg_text = _quote_format(auto_forward_msg(sms, device_label(device)))
    rows = [
        [premium_button(f"📋 Copy Otp: {otp}", callback_data=f"cp:{otp}")],
        [premium_button("📩 Last 10 Sms", callback_data=f"msgs:{device.id}"),
         premium_button("ℹ Info", callback_data=f"info:{device.id}")],
    ]
    markup  = InlineKeyboardMarkup(rows)
    if not _main_app: return
    is_admin_device = device.db_tag in ADMIN_ONLY_TAGS
    for cid, dev_id in list(user_focus.items()):
        if dev_id != device.id: continue
        if all_users.get(cid, {}).get("banned"): continue
        if not has_access(cid): continue
        if is_admin_device and cid not in ADMIN_IDS: continue
        try:
            await _main_app.bot.send_message(cid, msg_text, reply_markup=markup, parse_mode="HTML", read_timeout=5, write_timeout=5)
        except: pass


# ═══════════════════════════════════════════════════════
#  POLLING ENGINE
# ═══════════════════════════════════════════════════════
async def seed_seen_ids_for_db(url: str) -> None:
    try:
        r_main, r_user = await asyncio.gather(fb_get("All_Users/sms", url), fb_get("user_sms", url))
        for bulk in (r_main, r_user):
            if isinstance(bulk, dict):
                for dev_id, sms_dict in bulk.items():
                    if isinstance(sms_dict, dict):
                        for k in sms_dict: seen_ids.add(seen_key(dev_id, k))
    except: pass


async def poll_single_db(tag: str, url: str) -> None:
    try:
        r_main, r_user = await asyncio.gather(fb_get("All_Users/sms", url), fb_get("user_sms", url))
        device_map = {d.id: d for d in GLOBAL_DEVICE_CACHE.get(tag, [])}
        for bulk in (r_main, r_user):
            if isinstance(bulk, dict):
                for dev_id, sms_dict in bulk.items():
                    if isinstance(sms_dict, dict) and dev_id in device_map:
                        for k in sms_dict:
                            skey = seen_key(dev_id, k)
                            if skey not in seen_ids:
                                seen_ids.add(skey)
                                if not first_run:
                                    await _forward_otp_only(device_map[dev_id], sms_dict[k])
    except: pass


async def poll_loop(app: Application) -> None:
    global first_run, _main_app, seen_ids
    _main_app = app
    gc_counter = 0
    sem = asyncio.Semaphore(DB_CONCURRENCY)

    async def _fetch_and_cache(tag: str, url: str):
        async with sem: GLOBAL_DEVICE_CACHE[tag] = await fetch_db_data(tag, url)

    async def _poll_one(tag: str, url: str):
        async with sem: await poll_single_db(tag, url)

    while True:
        try:
            db_items = list(DATABASES.items())
            await asyncio.gather(*(_fetch_and_cache(t, u) for t, u in db_items))
            if first_run:
                await asyncio.gather(*(seed_seen_ids_for_db(u) for _, u in db_items))
                first_run = False
                tlog(f"Engine Online — {len(db_items)} DBs | {len(seen_ids)} SMS seeded")
            else:
                await asyncio.gather(*(_poll_one(t, u) for t, u in db_items))
                if len(seen_ids) > MAX_SEEN_IDS:
                    seen_ids = set(list(seen_ids)[-(MAX_SEEN_IDS // 2):])
            gc_counter += 1
            if gc_counter >= 5: gc.collect(); gc_counter = 0
        except Exception as e:
            tlog(f"Poll loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


# ═══════════════════════════════════════════════════════
#  BOT START TIME
# ═══════════════════════════════════════════════════════
_bot_start_time: float = time.time()


# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════
def main() -> None:
    print("═" * 56)
    print("  🤖 OTP PANEL BOT — V4 EDITION")
    print("═" * 56)
    load_data()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("grant",          cmd_grant))
    app.add_handler(CommandHandler("revoke",         cmd_revoke))
    app.add_handler(CommandHandler("setlogschat",    cmd_setlogschat))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.ANIMATION) & ~filters.COMMAND,
        on_media
    ))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def post_init(application: Application):
        asyncio.create_task(poll_loop(application))
        asyncio.create_task(auto_save_loop())
        asyncio.create_task(auto_backup_loop())

    app.post_init = post_init

    print(f"  Token : ...{TOKEN[-10:]}")
    print(f"  Admins: {len(ADMIN_IDS)}")
    print(f"  DBs   : {len(DATABASES)}")
    print("═" * 56)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
