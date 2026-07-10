"""Simple single-password gate for the dashboard and control endpoints.

Auth is ON only when DASHBOARD_PASSWORD is set. Localhost is always allowed
(the systemd timers curl localhost). Health is public for monitoring. The
cookie holds a token derived from the password + a per-install secret, so
changing the password invalidates old sessions."""
import hashlib
import hmac
import secrets

from . import config, db

COOKIE = "magpie_auth"
# Public: the login page, health (monitoring), and the timer-triggered action
# endpoints (harmless to trigger, expose no data — the systemd timers hit these
# through Docker's port map so they can't rely on a localhost exemption).
# Everything else — dashboard, portfolio state, settings (keys!), halt/resume —
# is gated.
PUBLIC_PATHS = {"/login", "/health", "/favicon.ico",
                "/api/cycle", "/api/digest", "/api/reconcile", "/api/review",
                "/api/universe/refresh"}


def _server_secret(conn) -> str:
    s = db.get_setting(conn, "auth_secret")
    if not s:
        s = secrets.token_hex(16)
        db.set_setting(conn, "auth_secret", s)
    return s


def token(conn) -> str:
    return hmac.new(_server_secret(conn).encode(),
                    config.DASHBOARD_PASSWORD.encode(), hashlib.sha256).hexdigest()


def enabled() -> bool:
    return bool(config.DASHBOARD_PASSWORD)


def check_password(conn, attempt: str) -> bool:
    return bool(attempt) and hmac.compare_digest(attempt, config.DASHBOARD_PASSWORD)


def check_login(conn, user: str, password: str) -> bool:
    if config.DASHBOARD_USER and (user or "").strip() != config.DASHBOARD_USER:
        return False
    return check_password(conn, password)


def is_authed(request, conn) -> bool:
    if not enabled():
        return True
    client = request.client.host if request.client else ""
    if client in ("127.0.0.1", "::1", "localhost"):
        return True  # the timers, on the same host
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie, token(conn))
