import logging
import os
import sqlite3
from datetime import datetime, timezone

from . import config, sleeves

LOGGER = logging.getLogger(__name__)

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
CREATE TABLE IF NOT EXISTS topups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, mode TEXT NOT NULL, amount REAL NOT NULL, per_sleeve REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, mode TEXT NOT NULL, sleeve TEXT NOT NULL DEFAULT '',
    total_eur REAL NOT NULL, holdings TEXT NOT NULL, prices TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inflight (
    exchange_id TEXT PRIMARY KEY,       -- written the MOMENT the order is created, so a
    decision_id INTEGER NOT NULL,       -- crash mid-fill leaves something to recover from
    at TEXT NOT NULL, mode TEXT NOT NULL, sleeve TEXT NOT NULL,
    pair TEXT NOT NULL, side TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL, sleeve TEXT NOT NULL, pair TEXT NOT NULL,
    amount REAL NOT NULL, stop_price REAL NOT NULL, entry_price REAL NOT NULL, pct REAL NOT NULL,
    exchange_id TEXT,                       -- the resting order at Kraken (live only)
    placed_at TEXT NOT NULL, closed_at TEXT, fill_price REAL,
    status TEXT NOT NULL                    -- open | filled | cancelled
);
CREATE TABLE IF NOT EXISTS scores (
    decision_id INTEGER PRIMARY KEY,   -- one mark per decision, graded once
    at TEXT NOT NULL, graded_at TEXT NOT NULL,
    mode TEXT NOT NULL, sleeve TEXT NOT NULL, pair TEXT NOT NULL, action TEXT NOT NULL,
    confidence REAL, horizon_days INTEGER NOT NULL,
    entry_price REAL NOT NULL, exit_price REAL NOT NULL, move_pct REAL NOT NULL,
    correct INTEGER NOT NULL
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
    # live sleeves start as empty shells — the top-up detector funds them from
    # whatever EUR is actually on the exchange at the first live cycle
    if conn.execute("SELECT 1 FROM sleeve_meta WHERE mode='live'").fetchone() is None:
        for s in sleeves.ALL:
            conn.execute("INSERT INTO sleeve_meta(mode, sleeve, allocated, hwm) VALUES('live',?,0,0)", (s,))
            conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('live',?,'EUR',0)", (s,))
        conn.commit()
    return conn


def backup(conn, path: str | None = None) -> dict:
    """Write a crash-consistent copy of the ledger, and prune old ones.

    VACUUM INTO is SQLite's own online-backup path: it produces a file that is
    guaranteed to open, even though the app is writing at the time. Copying the
    live WAL-mode file (cp, or a filesystem snapshot) usually works — which is
    not the same as always (#41). This is the audit trail for real money; it
    should not rest on 'usually'.
    """
    import glob
    src = path or config.DB_PATH
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(config.BACKUP_DIR, f"magpie-{stamp}.db")
    n = 1
    while os.path.exists(dest):    # VACUUM INTO refuses to overwrite; two in the same second collide
        dest = os.path.join(config.BACKUP_DIR, f"magpie-{stamp}-{n}.db")
        n += 1
    conn.execute("VACUUM INTO ?", (dest,))
    size = os.path.getsize(dest)

    kept = sorted(glob.glob(os.path.join(config.BACKUP_DIR, "magpie-*.db")))
    pruned = []
    while len(kept) > config.BACKUP_KEEP:
        old = kept.pop(0)
        os.remove(old)
        pruned.append(os.path.basename(old))
    LOGGER.info("ledger backed up to %s (%d KB); pruned %d", dest, size // 1024, len(pruned))
    return {"file": dest, "bytes": size, "kept": len(kept), "pruned": pruned}


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def bump_setting(conn, key: str, delta: int = 1, reset: bool = False) -> int:
    """Increment a counter IN THE DATABASE, and return the new value (#79).

    The counters (consecutive_failures, retry_attempts) were read into Python, added to,
    and written back — a read-modify-write across a connection boundary. Two overlapping
    runs read the same value and one increment was simply lost, which is exactly how the
    retry attempt cap became unenforceable during a sustained outage: the very situation
    it exists to bound. Do the arithmetic in SQL so the write cannot be based on a stale
    read. (The #68 mutex makes overlap rare; this makes the counter correct regardless.)
    """
    if reset:
        set_setting(conn, key, "0")
        return 0
    cur = conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + ? AS TEXT) "
        "RETURNING value", (key, str(delta), delta))
    row = cur.fetchone()
    conn.commit()
    return int(row["value"])
