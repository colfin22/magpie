import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app import config, main


@pytest.fixture
def client(monkeypatch):
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    monkeypatch.setattr(config, "DB_PATH", p)
    yield TestClient(main.app, follow_redirects=False)
    os.unlink(p)


def test_no_password_means_open(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "")
    assert client.get("/api/settings").status_code == 200


def test_gated_when_password_set(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "hunter2")
    assert client.get("/health").status_code == 200            # public
    assert client.post("/api/cycle") is not None               # public path (won't 401)
    assert client.post("/api/cycle").status_code != 401
    r = client.get("/api/settings")                            # gated
    assert r.status_code == 401


def test_login_grants_access(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "hunter2")
    bad = client.post("/login", data={"password": "wrong"})
    assert bad.status_code == 302 and "bad=1" in bad.headers["location"]
    ok = client.post("/login", data={"password": "hunter2"})
    assert ok.status_code == 302 and ok.headers["location"] == "/"
    assert "magpie_auth" in ok.cookies or "magpie_auth" in client.cookies
    assert client.get("/api/settings").status_code == 200      # cookie now present
    client.post("/logout")
    assert client.get("/api/settings").status_code == 401      # logged out


def test_totp_enable_verify_disable(client, monkeypatch):
    import pyotp
    from app import auth, db
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "hunter2")
    conn = db.connect()
    secret = auth.new_totp_secret(conn)
    assert auth.totp_is_enabled(conn) is False                  # minted, not yet confirmed
    assert auth.enable_totp(conn, "000000") is False            # wrong code won't activate
    assert auth.enable_totp(conn, pyotp.TOTP(secret).now()) is True
    assert auth.totp_is_enabled(conn) is True
    assert auth.check_totp(conn, pyotp.TOTP(secret).now()) is True
    assert auth.check_totp(conn, "123456") is False
    auth.disable_totp(conn)
    assert auth.totp_is_enabled(conn) is False
    assert auth._totp_secret(conn) == ""
    conn.close()


def test_login_requires_totp_when_enabled(client, monkeypatch):
    import pyotp
    from app import auth, db
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "hunter2")
    conn = db.connect(); secret = auth.new_totp_secret(conn)
    auth.enable_totp(conn, pyotp.TOTP(secret).now()); conn.close()
    # right password, no code -> bounced asking for the code (bad=2), no cookie
    r = client.post("/login", data={"password": "hunter2"})
    assert r.status_code == 302 and "bad=2" in r.headers["location"]
    assert client.get("/api/settings").status_code == 401
    # wrong password never reveals 2FA (bad=1)
    r = client.post("/login", data={"password": "nope", "otp": pyotp.TOTP(secret).now()})
    assert "bad=1" in r.headers["location"]
    # right password + right code -> in
    r = client.post("/login", data={"password": "hunter2", "otp": pyotp.TOTP(secret).now()})
    assert r.status_code == 302 and r.headers["location"] == "/"


def test_2fa_setup_needs_password(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "")
    assert client.post("/api/2fa/setup").status_code == 400     # no first factor -> refuse


def test_2fa_endpoints_end_to_end(client, monkeypatch):
    import pyotp
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "hunter2")
    client.post("/login", data={"password": "hunter2"})         # cookie (2FA not on yet)
    s = client.post("/api/2fa/setup").json()
    assert "svg" in s["qr_svg"].lower() and s["secret"] and s["uri"].startswith("otpauth://")
    assert client.post("/api/2fa/enable", json={"code": "000000"}).status_code == 400
    assert client.post("/api/2fa/enable", json={"code": pyotp.TOTP(s["secret"]).now()}).json()["enabled"] is True
    assert client.get("/api/2fa").json()["enabled"] is True
    assert client.post("/api/2fa/disable", json={"code": "000000"}).status_code == 400
    assert client.post("/api/2fa/disable", json={"code": pyotp.TOTP(s["secret"]).now()}).json()["enabled"] is False


def test_backup_codes_generate_and_consume(client, monkeypatch):
    from app import auth, db
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "hunter2")
    conn = db.connect()
    codes = auth.generate_backup_codes(conn)
    assert len(codes) == 10 and auth.backup_codes_remaining(conn) == 10
    assert auth.consume_backup_code(conn, codes[0]) is True          # burns it
    assert auth.backup_codes_remaining(conn) == 9
    assert auth.consume_backup_code(conn, codes[0]) is False         # single-use
    assert auth.consume_backup_code(conn, codes[3].replace("-", "").upper()) is True  # format-insensitive
    assert auth.consume_backup_code(conn, "not-a-code") is False
    conn.close()


def test_login_accepts_backup_code(client, monkeypatch):
    import pyotp
    from app import auth, db
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "hunter2")
    conn = db.connect(); secret = auth.new_totp_secret(conn)
    auth.enable_totp(conn, pyotp.TOTP(secret).now())
    codes = auth.generate_backup_codes(conn); conn.close()
    r = client.post("/login", data={"password": "hunter2", "otp": codes[0]})
    assert r.status_code == 302 and r.headers["location"] == "/"      # backup code logs in
    # and it's now spent
    conn = db.connect(); assert auth.consume_backup_code(conn, codes[0]) is False; conn.close()


def test_2fa_backup_endpoints(client, monkeypatch):
    import pyotp
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "hunter2")
    client.post("/login", data={"password": "hunter2"})
    s = client.post("/api/2fa/setup").json()
    enabled = client.post("/api/2fa/enable", json={"code": pyotp.TOTP(s["secret"]).now()}).json()
    assert len(enabled["backup_codes"]) == 10                         # shown once on enable
    assert client.get("/api/2fa").json()["backup_remaining"] == 10
    # regenerate needs a real code
    assert client.post("/api/2fa/backup", json={"code": "000000"}).status_code == 400
    fresh = client.post("/api/2fa/backup", json={"code": pyotp.TOTP(s["secret"]).now()}).json()
    assert len(fresh["backup_codes"]) == 10 and fresh["backup_codes"] != enabled["backup_codes"]
    # disable wipes them
    client.post("/api/2fa/disable", json={"code": pyotp.TOTP(s["secret"]).now()})
    assert client.get("/api/2fa").json()["backup_remaining"] == 0
