import os
import sqlite3

from . import config, sleeves

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    pair TEXT NOT NULL, ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (pair, ts)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, mode TEXT NOT NULL, sleeve TEXT NOT NULL DEFAULT '',
    prompt TEXT, response_raw TEXT,
    action TEXT, pair TEXT, fraction REAL, confidence REAL, reasoning TEXT,
    status TEXT NOT NULL,          -- executed | held | invalid | error | halted | no_key
    detail TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, mode TEXT NOT NULL, sleeve TEXT NOT NULL DEFAULT '',
    decision_id INTEGER,
    pair TEXT NOT NULL, side TEXT NOT NULL,
    amount REAL NOT NULL, price REAL NOT NULL, cost REAL NOT NULL, fee REAL NOT NULL,
    exchange_id TEXT
);
CREATE TABLE IF NOT EXISTS holdings (
    mode TEXT NOT NULL, sleeve TEXT NOT NULL, asset TEXT NOT NULL, amount REAL NOT NULL,
    PRIMARY KEY (mode, sleeve, asset)
);
CREATE TABLE IF NOT EXISTS sleeve_meta (
    mode TEXT NOT NULL, sleeve TEXT NOT NULL,
    allocated REAL NOT NULL,        -- initial stake for this sleeve
    hwm REAL NOT NULL,              -- high-water mark for profit skimming
    PRIMARY KEY (mode, sleeve)
);
CREATE TABLE IF NOT EXISTS skims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, mode TEXT NOT NULL, sleeve TEXT NOT NULL, amount REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, mode TEXT NOT NULL, sleeve TEXT NOT NULL DEFAULT '',
    total_eur REAL NOT NULL, holdings TEXT NOT NULL, prices TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or config.DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(SCHEMA)
    # seed the paper sleeves once: three equal actives, an empty vault
    if conn.execute("SELECT 1 FROM sleeve_meta WHERE mode='paper'").fetchone() is None:
        per = round(config.START_BALANCE_EUR / len(sleeves.ACTIVE), 2)
        for s in sleeves.ACTIVE:
            conn.execute("INSERT INTO sleeve_meta(mode, sleeve, allocated, hwm) VALUES('paper',?,?,?)",
                         (s, per, per))
            conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('paper',?,'EUR',?)",
                         (s, per))
        conn.execute("INSERT INTO sleeve_meta(mode, sleeve, allocated, hwm) VALUES('paper',?,0,0)",
                     (sleeves.VAULT,))
        conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('paper',?,'EUR',0)",
                     (sleeves.VAULT,))
        conn.commit()
    return conn


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
