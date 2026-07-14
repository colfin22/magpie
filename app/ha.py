"""Notifications — fans a message out to every configured channel.

Home Assistant push is the original; Pushover, Pushbullet, Discord, Telegram and
ntfy are optional extras. Each channel fires only when its own config is present,
so you can enable one, several or none, and one failing channel never blocks the
rest. (Module is named `ha` for historical reasons — it's the app-wide notify
dispatcher; every `ha.notify(...)` call reaches all channels.)"""
import logging

import httpx

from . import config

LOGGER = logging.getLogger(__name__)
_T = 30  # per-channel timeout


def _click() -> str:
    return config.HA_NOTIFY_CLICK_URL or ""


def _ha(title, message):
    if not (config.HA_URL and config.HA_TOKEN and config.HA_NOTIFY_SERVICE):
        return None
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
    body = ({"type": "link", "title": title, "body": message, "url": _click()}
            if _click() else {"type": "note", "title": title, "body": message})
    r = httpx.post("https://api.pushbullet.com/v2/pushes",
                   headers={"Access-Token": config.PUSHBULLET_TOKEN}, json=body, timeout=_T)
    r.raise_for_status()
    return True


def _discord(title, message):
    if not config.DISCORD_WEBHOOK_URL:
        return None
    embed = {"title": title, "description": message, "color": 0x4CD97B}
    if _click():
        embed["url"] = _click()  # makes the embed title a clickable link
    r = httpx.post(config.DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=_T)
    r.raise_for_status()
    return True


def _telegram(title, message):
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return None
    # PLAIN TEXT, no parse_mode (#75). Legacy Markdown makes Telegram reject the WHOLE
    # message with a 400 on an unbalanced *, _, ` or [ — and these messages carry
    # free-form model prose ("EMA_20 crossed the EMA_50..." has an odd number of
    # underscores). So the pushes most worth receiving — a real trade, a dead arm, the
    # monthly review — were the ones that could not be sent. A notification must never be
    # made unsendable by its own contents.
    text = f"{title}\n{message}"
    if _click():
        text += f"\n{_click()}"
    r = httpx.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                   json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                         "disable_web_page_preview": True}, timeout=_T)
    r.raise_for_status()
    return True


def _ntfy(title, message):
    if not config.NTFY_TOPIC:
        return None
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
    for name, fn in CHANNELS:
        try:
            if fn(title, message):
                sent.append(name)
        except Exception as e:  # noqa: BLE001 - one bad channel must not block the others
            LOGGER.error("notify via %s failed: %s", name, e)
    if sent:
        LOGGER.info("notify sent via %s: %s", ", ".join(sent), title)
        return True
    # A notification nobody received is an event in itself (#75). This used to be logged
    # at INFO and every caller discards the return value — so the bot could move real
    # money, or announce that it was failing, into a void.
    LOGGER.error("NOTIFY REACHED NOBODY: %r — no channel configured, or every one failed",
                 title)
    return False
