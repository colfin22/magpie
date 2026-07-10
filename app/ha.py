"""Optional Home Assistant push notifications (same pattern as the other house apps)."""
import logging

import httpx

from . import config

LOGGER = logging.getLogger(__name__)


def notify(title: str, message: str) -> bool:
    if not (config.HA_URL and config.HA_TOKEN and config.HA_NOTIFY_SERVICE):
        LOGGER.info("notify skipped (HA not configured): %s", title)
        return False
    try:
        domain, name = config.HA_NOTIFY_SERVICE.split(".")
        payload = {"title": title, "message": message}
        if config.HA_NOTIFY_CLICK_URL:
            # tap-to-open the dashboard (clickAction = Android, url = iOS)
            payload["data"] = {"clickAction": config.HA_NOTIFY_CLICK_URL,
                               "url": config.HA_NOTIFY_CLICK_URL}
        r = httpx.post(f"{config.HA_URL}/api/services/{domain}/{name}",
                       headers={"Authorization": f"Bearer {config.HA_TOKEN}"},
                       json=payload, timeout=30)
        r.raise_for_status()
        return True
    except httpx.HTTPError as e:
        LOGGER.error("notify failed: %s", e)
        return False
