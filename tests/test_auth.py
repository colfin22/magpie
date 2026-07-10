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
