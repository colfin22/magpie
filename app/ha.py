"""Notifications — fans a message out to every configured channel.

Home Assistant push is the original; Pushover, Pushbullet, Discord, Telegram and
ntfy are optional extras. Each channel fires only when its own config is present,
so you can enable one, several or none, and one failing channel never blocks the
rest. (Module is named `ha` for historical reasons — it's the app-wide notify
dispatcher; every `ha.notify(...)` call reaches all channels.)"""
import html
import logging

import httpx

from . import config

LOGGER = logging.getLogger(__name__)
_T = 30  # per-channel timeout

# ---------- each channel's OWN limit (#62) ----------
#
# A single global NOTIFY_CHARS=1000 was picked against Pushover's limit and applied to
# one message (the trade push). But notify() fans out to SIX services, each with its own
# ceiling, and the DIGEST -- the longest thing the bot sends, and the one actually read
# every day -- was never clipped at all. A message too long for a channel is rejected
# outright, and a rejected message was only ever a log line: a digest that never arrives
# looks exactly like a quiet day.
#
# Limits below are from each service's published API documentation (Sept 2024 versions),
# NOT from live probing -- only Home Assistant is configured on this install, so the other
# five cannot be tested from here. They are deliberately conservative: clipping a little
# early costs a few words, guessing high costs the whole message.
#
#   telegram    sendMessage `text`            4096 chars   (Bot API)
#   discord     webhook embed `description`   4096 chars   (embed limits)
#   ntfy        message body                  4096 bytes   (server default)
#   pushbullet  push `body`                   not published -> conservative
#   pushover    `message` 1024, `title` 250   (documented)
#   home assistant  no documented limit; large payloads collapse in the Android shade
LIMITS = {
    "ha":         (10_000, 255),
    "pushover":   (1024, 250),
    "pushbullet": (4000, 250),
    "discord":    (4096, 256),
    "telegram":   (4000, 256),   # under 4096 to leave room for the tags and the click URL
    "ntfy":       (3800, 250),   # bytes, not chars — leave headroom for multi-byte prose
}


def clip(text: str, limit: int) -> str:
    """Trim to a channel's limit at a WORD boundary, with an ellipsis so it is VISIBLY
    truncated. A message cut mid-word does not look shortened — it looks broken."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (cut or t[:limit - 1]) + "…"


def _fit(channel: str, title: str, message: str) -> tuple[str, str]:
    msg_max, title_max = LIMITS[channel]
    return clip(title, title_max), clip(message, msg_max)


# what did NOT get through on the last send — surfaced on /health so a dropped
# notification is visible somewhere a person actually looks (#62)
LAST_FAILURES: list[dict] = []


def _click() -> str:
    return config.HA_NOTIFY_CLICK_URL or ""


def _ha(title, message):
    if not (config.HA_URL and config.HA_TOKEN and config.HA_NOTIFY_SERVICE):
        return None
    title, message = _fit("ha", title, message)
    domain, name = config.HA_NOTIFY_SERVICE.split(".")
    payload = {"title": title, "message": message}
    if _click():  # clickAction = Android, url = iOS
        payload["data"] = {"clickAction": _click(), "url": _click()}
    r = httpx.post(f"{config.HA_URL}/api/services/{domain}/{name}",
                   headers={"Authorization": f"Bearer {config.HA_TOKEN}"},
                   json=payload, timeout=_T)
    r.raise_for_status()
    return True


def _pushover(title, message):
    if not (config.PUSHOVER_TOKEN and config.PUSHOVER_USER):
        return None
    title, message = _fit("pushover", title, message)
    data = {"token": config.PUSHOVER_TOKEN, "user": config.PUSHOVER_USER,
            "title": title, "message": message}
    if _click():
        data["url"] = _click()
    r = httpx.post("https://api.pushover.net/1/messages.json", data=data, timeout=_T)
    r.raise_for_status()
    return True


def _pushbullet(title, message):
    if not config.PUSHBULLET_TOKEN:
        return None
    title, message = _fit("pushbullet", title, message)
    body = ({"type": "link", "title": title, "body": message, "url": _click()}
            if _click() else {"type": "note", "title": title, "body": message})
    r = httpx.post("https://api.pushbullet.com/v2/pushes",
                   headers={"Access-Token": config.PUSHBULLET_TOKEN}, json=body, timeout=_T)
    r.raise_for_status()
    return True


def _discord(title, message):
    if not config.DISCORD_WEBHOOK_URL:
        return None
    title, message = _fit("discord", title, message)
    embed = {"title": title, "description": message, "color": 0x4CD97B}
    if _click():
        embed["url"] = _click()  # makes the embed title a clickable link
    r = httpx.post(config.DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=_T)
    r.raise_for_status()
    return True


def _telegram(title, message):
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return None
    title, message = _fit("telegram", title, message)
    # HTML mode, with EVERY interpolated value ESCAPED (#75, PR #76).
    #
    # Legacy Markdown rejected the WHOLE message with a 400 on an unbalanced *, _, ` or [
    # — and these messages carry free-form model prose ("EMA_20 crossed the EMA_50" has an
    # odd number of underscores). So the pushes most worth receiving — a real trade, a
    # dead arm, the monthly review — were exactly the ones that could not be sent.
    #
    # HTML mode ALONE does not fix that: Telegram 400s on an unescaped <, > or & just as
    # readily, and trading prose is full of them ("RSI < 30", "risk & reward"). It only
    # swaps which characters are fatal. The escaping is the part that actually closes the
    # failure mode — then the formatting is free.
    #
    # A notification must never be made unsendable by its own contents.
    text = f"<b>{html.escape(title)}</b>\n{html.escape(message)}"
    if _click():
        text += f"\n{html.escape(_click())}"
    r = httpx.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                   json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                         "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=_T)
    r.raise_for_status()
    return True


def _ntfy(title, message):
    if not config.NTFY_TOPIC:
        return None
    title, message = _fit("ntfy", title, message)
    server = (config.NTFY_SERVER or "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode("ascii", "ignore").decode()}  # ntfy headers are ASCII
    if _click():
        headers["Click"] = _click()
    r = httpx.post(f"{server}/{config.NTFY_TOPIC}", data=message.encode("utf-8"),
                   headers=headers, timeout=_T)
    r.raise_for_status()
    return True


CHANNELS = [("home assistant", _ha), ("pushover", _pushover), ("pushbullet", _pushbullet),
            ("discord", _discord), ("telegram", _telegram), ("ntfy", _ntfy)]


def notify(title: str, message: str) -> bool:
    """Send to every configured channel. True if at least one delivered."""
    sent = []
    failed = []
    for name, fn in CHANNELS:
        try:
            if fn(title, message):
                sent.append(name)
        except Exception as e:  # noqa: BLE001 - one bad channel must not block the others
            LOGGER.error("notify via %s failed: %s", name, e)
            failed.append({"channel": name, "title": title, "error": str(e)[:200]})
    # Remember what did NOT get through, so /health can show it (#62). Catching per-channel
    # failures is right — one bad channel must not block the others — but the consequence
    # was that a rejected message was a log line and NOTHING else. No delivery, no warning,
    # no sign anything was lost. A digest that never arrives looks exactly like a quiet day.
    LAST_FAILURES.clear()
    LAST_FAILURES.extend(failed)
    if sent:
        LOGGER.info("notify sent via %s: %s", ", ".join(sent), title)
        return True
    # A notification nobody received is an event in itself (#75). This used to be logged
    # at INFO and every caller discards the return value — so the bot could move real
    # money, or announce that it was failing, into a void.
    LOGGER.error("NOTIFY REACHED NOBODY: %r — no channel configured, or every one failed",
                 title)
    return False
