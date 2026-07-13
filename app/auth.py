"""Simple single-password gate for the dashboard and control endpoints.

Auth is ON only when DASHBOARD_PASSWORD is set. Localhost is always allowed
(the systemd timers curl localhost). Health is public for monitoring. The
cookie holds a token derived from the password + a per-install secret, so
changing the password invalidates old sessions."""
import hashlib
import hmac
import json
import secrets

from . import config, db

BACKUP_COUNT = 10

COOKIE = "magpie_auth"
# Public: the login page, health (monitoring), and the timer-triggered action
# endpoints (harmless to trigger, expose no data — the systemd timers hit these
# through Docker's port map so they can't rely on a localhost exemption).
# Everything else — dashboard, portfolio state, settings (keys!), halt/resume —
# is gated.
PUBLIC_PATHS = {"/login", "/health", "/favicon.ico", "/favicon.svg",
                "/api/cycle", "/api/cycle/retry", "/api/digest", "/api/reconcile",
                "/api/review", "/api/universe/refresh", "/api/backup"}


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


# ---- optional TOTP second factor (checked at /login, before the cookie) ----
# A valid session cookie already means "both factors passed", so is_authed and
# the cookie are unchanged; 2FA only adds a step at login. Requires a password
# (the first factor) to be meaningful. Secret lives in the settings table.

def totp_is_enabled(conn) -> bool:
    return db.get_setting(conn, "totp_enabled") == "1" and bool(_totp_secret(conn))


def _totp_secret(conn) -> str:
    return db.get_setting(conn, "totp_secret") or ""


def new_totp_secret(conn) -> str:
    """Mint a fresh secret (not active until a code confirms it)."""
    import pyotp
    sec = pyotp.random_base32()
    db.set_setting(conn, "totp_secret", sec)
    db.set_setting(conn, "totp_enabled", "0")
    return sec


def totp_uri(conn, secret: str | None = None) -> str:
    import pyotp
    sec = secret or _totp_secret(conn)
    label = config.DASHBOARD_USER or "magpie"
    return pyotp.TOTP(sec).provisioning_uri(name=label, issuer_name="Magpie")


def check_totp(conn, code: str) -> bool:
    import pyotp
    sec = _totp_secret(conn)
    code = str(code or "").strip().replace(" ", "")
    if not sec or not code.isdigit():
        return False
    return pyotp.TOTP(sec).verify(code, valid_window=1)  # ±30s clock tolerance


def enable_totp(conn, code: str) -> bool:
    if check_totp(conn, code):
        db.set_setting(conn, "totp_enabled", "1")
        return True
    return False


def disable_totp(conn) -> None:
    db.set_setting(conn, "totp_enabled", "0")
    db.set_setting(conn, "totp_secret", "")
    db.set_setting(conn, "totp_backup_codes", "[]")


# ---- single-use backup recovery codes (accepted in place of a TOTP code) ----
# Stored hashed (HMAC with the server secret); the plaintext is shown once at
# generation and never again. Using one consumes it.

def _hash_code(conn, code: str) -> str:
    norm = str(code or "").strip().lower().replace("-", "").replace(" ", "")
    return hmac.new(_server_secret(conn).encode(), norm.encode(), hashlib.sha256).hexdigest()


def generate_backup_codes(conn) -> list[str]:
    """Mint a fresh set (invalidating any old set) and return the plaintext once."""
    codes, hashes = [], []
    for _ in range(BACKUP_COUNT):
        raw = secrets.token_hex(4)              # 8 hex chars of entropy
        pretty = raw[:4] + "-" + raw[4:]
        codes.append(pretty)
        hashes.append(_hash_code(conn, pretty))
    db.set_setting(conn, "totp_backup_codes", json.dumps(hashes))
    return codes


def backup_codes_remaining(conn) -> int:
    try:
        return len(json.loads(db.get_setting(conn, "totp_backup_codes", "[]") or "[]"))
    except (ValueError, TypeError):
        return 0


def consume_backup_code(conn, code: str) -> bool:
    """Verify + burn a backup code. A 6-digit TOTP never matches (different length)."""
    norm = str(code or "").strip().lower().replace("-", "").replace(" ", "")
    if not norm:
        return False
    target = _hash_code(conn, norm)
    try:
        hashes = json.loads(db.get_setting(conn, "totp_backup_codes", "[]") or "[]")
    except (ValueError, TypeError):
        hashes = []
    match = next((h for h in hashes if hmac.compare_digest(h, target)), None)
    if match is None:
        return False
    hashes.remove(match)
    db.set_setting(conn, "totp_backup_codes", json.dumps(hashes))
    return True


def totp_qr_svg(uri: str) -> str:
    """Inline SVG QR (pure-python, no PIL, no external requests → CSP-safe)."""
    import io

    import qrcode
    import qrcode.image.svg
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()
