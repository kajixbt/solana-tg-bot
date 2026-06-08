"""
Persistent storage — SQLite backend
Phase 2 rewrite: indexed tables, token metadata DB, cached holdings
"""

import os
import json
import time
import hashlib
import sqlite3
import threading
from pathlib import Path
from cryptography.fernet import Fernet

DB_PATH        = Path(os.environ.get("DATA_DIR", "./data")) / "bot.db"
DB_PATH.parent.mkdir(exist_ok=True)
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

_local = threading.local()

def _db() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
    return _local.conn

def init_db():
    db = _db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS wallets (
        user_id    INTEGER NOT NULL,
        label      TEXT    NOT NULL,
        enc_key    TEXT    NOT NULL,
        is_active  INTEGER DEFAULT 0,
        created_at INTEGER DEFAULT (strftime('%s','now')),
        PRIMARY KEY (user_id, label)
    );
    CREATE INDEX IF NOT EXISTS idx_wallets_user ON wallets(user_id);

    CREATE TABLE IF NOT EXISTS trades (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL,
        ts           INTEGER NOT NULL,
        side         TEXT    NOT NULL,
        symbol       TEXT    NOT NULL,
        mint         TEXT    NOT NULL,
        token_amount REAL    NOT NULL,
        sol_amount   REAL    NOT NULL,
        price_usd    REAL    NOT NULL DEFAULT 0,
        tx           TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_trades_user     ON trades(user_id);
    CREATE INDEX IF NOT EXISTS idx_trades_user_mint ON trades(user_id, mint);

    CREATE TABLE IF NOT EXISTS limit_orders (
        id               INTEGER PRIMARY KEY,
        user_id          INTEGER NOT NULL,
        side             TEXT    NOT NULL,
        mint             TEXT    NOT NULL,
        symbol           TEXT    NOT NULL,
        sol_amount       REAL    DEFAULT 0,
        token_amount     REAL    DEFAULT 0,
        target_price_usd REAL    NOT NULL,
        slippage_bps     INTEGER DEFAULT 100,
        status           TEXT    DEFAULT 'open',
        created_at       INTEGER DEFAULT (strftime('%s','now'))
    );
    CREATE INDEX IF NOT EXISTS idx_orders_user   ON limit_orders(user_id);
    CREATE INDEX IF NOT EXISTS idx_orders_status ON limit_orders(status);

    CREATE TABLE IF NOT EXISTS settings (
        user_id INTEGER NOT NULL,
        key     TEXT    NOT NULL,
        value   TEXT    NOT NULL,
        PRIMARY KEY (user_id, key)
    );

    CREATE TABLE IF NOT EXISTS pins (
        user_id  INTEGER PRIMARY KEY,
        pin_hash TEXT    NOT NULL
    );

    CREATE TABLE IF NOT EXISTS token_meta (
        mint       TEXT PRIMARY KEY,
        symbol     TEXT NOT NULL,
        name       TEXT NOT NULL,
        updated_at INTEGER DEFAULT (strftime('%s','now'))
    );
    CREATE INDEX IF NOT EXISTS idx_token_meta_symbol ON token_meta(symbol);

    CREATE TABLE IF NOT EXISTS watchlist (
        user_id    INTEGER NOT NULL,
        mint       TEXT    NOT NULL,
        symbol     TEXT    NOT NULL,
        name       TEXT    NOT NULL,
        added_at   INTEGER DEFAULT (strftime('%s','now')),
        PRIMARY KEY (user_id, mint)
    );
    """)
    db.commit()

init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    if not ENCRYPTION_KEY:
        raise RuntimeError("ENCRYPTION_KEY not set in .env")
    return Fernet(ENCRYPTION_KEY.encode())

# ── PIN ───────────────────────────────────────────────────────────────────────

def set_pin(user_id: int, pin: str):
    h = hashlib.sha256(pin.encode()).hexdigest()
    _db().execute("INSERT OR REPLACE INTO pins VALUES (?,?)", (user_id, h))
    _db().commit()

def check_pin(user_id: int, pin: str) -> bool:
    row = _db().execute("SELECT pin_hash FROM pins WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return True
    return row["pin_hash"] == hashlib.sha256(pin.encode()).hexdigest()

def has_pin(user_id: int) -> bool:
    return bool(_db().execute("SELECT 1 FROM pins WHERE user_id=?", (user_id,)).fetchone())

# ── Wallets ───────────────────────────────────────────────────────────────────

def save_wallet(user_id: int, label: str, private_key: str):
    enc = _fernet().encrypt(private_key.encode()).decode()
    db  = _db()
    # If first wallet, make active
    count = db.execute("SELECT COUNT(*) FROM wallets WHERE user_id=?", (user_id,)).fetchone()[0]
    db.execute(
        "INSERT OR REPLACE INTO wallets (user_id, label, enc_key, is_active) VALUES (?,?,?,?)",
        (user_id, label, enc, 1 if count == 0 else 0)
    )
    db.commit()

def load_wallet(user_id: int, label: str = None) -> str | None:
    db = _db()
    if label:
        row = db.execute("SELECT enc_key FROM wallets WHERE user_id=? AND label=?",
                         (user_id, label)).fetchone()
    else:
        row = db.execute("SELECT enc_key FROM wallets WHERE user_id=? AND is_active=1",
                         (user_id,)).fetchone()
    if not row:
        return None
    try:
        return _fernet().decrypt(row["enc_key"].encode()).decode()
    except Exception:
        return None

def list_wallets(user_id: int) -> tuple[dict, str]:
    rows   = _db().execute(
        "SELECT label, is_active FROM wallets WHERE user_id=? ORDER BY created_at",
        (user_id,)
    ).fetchall()
    active = next((r["label"] for r in rows if r["is_active"]), "")
    return {r["label"]: r["label"] for r in rows}, active

def set_active_wallet(user_id: int, label: str) -> bool:
    db = _db()
    if not db.execute("SELECT 1 FROM wallets WHERE user_id=? AND label=?",
                      (user_id, label)).fetchone():
        return False
    db.execute("UPDATE wallets SET is_active=0 WHERE user_id=?", (user_id,))
    db.execute("UPDATE wallets SET is_active=1 WHERE user_id=? AND label=?", (user_id, label))
    db.commit()
    return True

def delete_wallet(user_id: int, label: str) -> bool:
    db = _db()
    if not db.execute("SELECT 1 FROM wallets WHERE user_id=? AND label=?",
                      (user_id, label)).fetchone():
        return False
    db.execute("DELETE FROM wallets WHERE user_id=? AND label=?", (user_id, label))
    # If deleted was active, make another active
    db.execute("""
        UPDATE wallets SET is_active=1
        WHERE user_id=? AND rowid=(
            SELECT rowid FROM wallets WHERE user_id=? LIMIT 1
        )
    """, (user_id, user_id))
    db.commit()
    return True

# ── Token metadata ────────────────────────────────────────────────────────────

def save_token_meta(mint: str, symbol: str, name: str):
    _db().execute(
        "INSERT OR REPLACE INTO token_meta (mint, symbol, name, updated_at) VALUES (?,?,?,?)",
        (mint, symbol, name, int(time.time()))
    )
    _db().commit()

def get_token_meta_db(mint: str) -> dict | None:
    row = _db().execute("SELECT symbol, name FROM token_meta WHERE mint=?", (mint,)).fetchone()
    return {"symbol": row["symbol"], "name": row["name"]} if row else None

def get_all_token_meta() -> list[tuple]:
    return [(r["mint"], r["symbol"], r["name"])
            for r in _db().execute("SELECT mint, symbol, name FROM token_meta").fetchall()]

# ── Trades ────────────────────────────────────────────────────────────────────

def record_trade(user_id: int, side: str, symbol: str, mint: str,
                 token_amount: float, sol_amount: float, price_usd: float, tx: str):
    _db().execute(
        "INSERT INTO trades (user_id,ts,side,symbol,mint,token_amount,sol_amount,price_usd,tx) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, int(time.time()), side, symbol, mint, token_amount, sol_amount, price_usd, tx)
    )
    _db().commit()

def get_trades(user_id: int, mint: str = None, limit: int = 200) -> list[dict]:
    if mint:
        rows = _db().execute(
            "SELECT * FROM trades WHERE user_id=? AND mint=? ORDER BY ts DESC LIMIT ?",
            (user_id, mint, limit)
        ).fetchall()
    else:
        rows = _db().execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]

def get_holdings(user_id: int) -> list[dict]:
    """Compute current holdings from trade history."""
    rows = _db().execute(
        "SELECT mint, symbol, side, SUM(token_amount) as amt, SUM(sol_amount) as sol, "
        "SUM(token_amount * price_usd) as cost, SUM(token_amount) as total_bought "
        "FROM trades WHERE user_id=? GROUP BY mint, side",
        (user_id,)
    ).fetchall()

    # Aggregate buy/sell per mint
    agg: dict[str, dict] = {}
    for r in rows:
        mint = r["mint"]
        if mint not in agg:
            agg[mint] = {"mint": mint, "symbol": r["symbol"],
                         "bought": 0.0, "sold": 0.0,
                         "spent_sol": 0.0, "cost_usd": 0.0, "total_bought": 0.0}
        if r["side"] == "buy":
            agg[mint]["bought"]      += r["amt"]
            agg[mint]["spent_sol"]   += r["sol"]
            agg[mint]["cost_usd"]    += r["cost"]
            agg[mint]["total_bought"]+= r["total_bought"]
        else:
            agg[mint]["sold"]        += r["amt"]
            agg[mint]["spent_sol"]   -= r["sol"]

    result = []
    for h in agg.values():
        remaining = h["bought"] - h["sold"]
        if remaining > 0.0001:
            avg_entry = (h["cost_usd"] / h["total_bought"]) if h["total_bought"] else 0
            # Get best available symbol from token_meta DB
            meta = get_token_meta_db(h["mint"])
            sym  = (meta["symbol"] if meta else None) or h["symbol"]
            result.append({
                "mint":            h["mint"],
                "symbol":          sym,
                "token_amount":    remaining,
                "avg_entry_usd":   avg_entry,
                "total_spent_sol": h["spent_sol"],
            })
    return result

def get_pnl(user_id: int, mint: str, current_price_usd: float) -> dict:
    rows = _db().execute(
        "SELECT side, token_amount, price_usd FROM trades WHERE user_id=? AND mint=?",
        (user_id, mint)
    ).fetchall()
    if not rows:
        return {}

    bought = sold = cost = received = 0.0
    for r in rows:
        if r["side"] == "buy":
            bought += r["token_amount"]
            cost   += r["token_amount"] * r["price_usd"]
        else:
            sold     += r["token_amount"]
            received += r["token_amount"] * r["price_usd"]

    remaining  = bought - sold
    avg_entry  = cost / bought if bought else 0
    unrealized = remaining * (current_price_usd - avg_entry)
    realized   = received - (sold * avg_entry)
    pnl_pct    = ((current_price_usd - avg_entry) / avg_entry * 100) if avg_entry else 0

    return {
        "avg_entry_usd":     avg_entry,
        "current_price_usd": current_price_usd,
        "remaining_tokens":  remaining,
        "unrealized_pnl":    unrealized,
        "realized_pnl":      realized,
        "pnl_pct":           pnl_pct,
    }

# ── Limit orders ──────────────────────────────────────────────────────────────

def add_limit_order(user_id: int, order: dict) -> int:
    oid = int(time.time() * 1000)
    _db().execute(
        "INSERT INTO limit_orders (id,user_id,side,mint,symbol,sol_amount,token_amount,"
        "target_price_usd,slippage_bps,status) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (oid, user_id, order["side"], order["mint"], order["symbol"],
         order.get("sol_amount", 0), order.get("token_amount", 0),
         order["target_price_usd"], order.get("slippage_bps", 100), "open")
    )
    _db().commit()
    return oid

def get_open_orders(user_id: int) -> list[dict]:
    return [dict(r) for r in _db().execute(
        "SELECT * FROM limit_orders WHERE user_id=? AND status='open' ORDER BY created_at",
        (user_id,)
    ).fetchall()]

def get_all_open_orders() -> list[tuple]:
    return [(r["user_id"], dict(r)) for r in _db().execute(
        "SELECT * FROM limit_orders WHERE status='open'"
    ).fetchall()]

def close_order(user_id: int, order_id: int, status: str = "filled"):
    _db().execute(
        "UPDATE limit_orders SET status=? WHERE id=? AND user_id=?",
        (status, order_id, user_id)
    )
    _db().commit()

# ── Settings ──────────────────────────────────────────────────────────────────

DEFAULTS = {
    "slippage":       1.0,
    "mev_protect":    False,
    "auto_confirm":   False,
    "smart_slippage": True,
    "price_alerts":   True,
    "notifications":  True,
}

def get_setting(user_id: int, key: str, default=None):
    row = _db().execute(
        "SELECT value FROM settings WHERE user_id=? AND key=?", (user_id, key)
    ).fetchone()
    if row:
        val = row["value"]
        try:
            return json.loads(val)
        except Exception:
            return val
    return default if default is not None else DEFAULTS.get(key)

def set_setting(user_id: int, key: str, value):
    _db().execute(
        "INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?,?,?)",
        (user_id, key, json.dumps(value))
    )
    _db().commit()

def toggle_setting(user_id: int, key: str) -> bool:
    current = get_setting(user_id, key, DEFAULTS.get(key, False))
    new_val = not current
    set_setting(user_id, key, new_val)
    return new_val

def get_all_settings(user_id: int) -> dict:
    rows = _db().execute(
        "SELECT key, value FROM settings WHERE user_id=?", (user_id,)
    ).fetchall()
    result = dict(DEFAULTS)
    for r in rows:
        try:
            result[r["key"]] = json.loads(r["value"])
        except Exception:
            result[r["key"]] = r["value"]
    return result

# Priority fee: None = auto, integer = custom lamports
def get_priority_fee_setting(user_id: int) -> int | None:
    val = get_setting(user_id, "priority_fee_lamports", None)
    return int(val) if val is not None else None

def set_priority_fee_custom(user_id: int, lamports: int):
    set_setting(user_id, "priority_fee_lamports", int(lamports))

def use_auto_priority_fee(user_id: int):
    _db().execute("DELETE FROM settings WHERE user_id=? AND key='priority_fee_lamports'", (user_id,))
    _db().commit()

# ── Watchlist ─────────────────────────────────────────────────────────────────

def add_to_watchlist(user_id: int, mint: str, symbol: str, name: str):
    _db().execute(
        "INSERT OR REPLACE INTO watchlist (user_id, mint, symbol, name) VALUES (?,?,?,?)",
        (user_id, mint, symbol, name)
    )
    _db().commit()

def remove_from_watchlist(user_id: int, mint: str):
    _db().execute("DELETE FROM watchlist WHERE user_id=? AND mint=?", (user_id, mint))
    _db().commit()

def get_watchlist(user_id: int) -> list[dict]:
    return [dict(r) for r in _db().execute(
        "SELECT * FROM watchlist WHERE user_id=? ORDER BY added_at DESC",
        (user_id,)
    ).fetchall()]


# ── Alerts ────────────────────────────────────────────────────────────────────

def _ensure_alerts_table():
    _db().executescript("""
    CREATE TABLE IF NOT EXISTS alerts (
        id           INTEGER PRIMARY KEY,
        user_id      INTEGER NOT NULL,
        mint         TEXT    NOT NULL,
        symbol       TEXT    NOT NULL,
        condition    TEXT    NOT NULL,
        target_price REAL    NOT NULL,
        status       TEXT    DEFAULT 'open',
        created_at   INTEGER DEFAULT (strftime('%s','now'))
    );
    CREATE INDEX IF NOT EXISTS idx_alerts_user   ON alerts(user_id);
    CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);

    CREATE TABLE IF NOT EXISTS tp_sl (
        id            INTEGER PRIMARY KEY,
        user_id       INTEGER NOT NULL,
        mint          TEXT    NOT NULL,
        symbol        TEXT    NOT NULL,
        type          TEXT    NOT NULL,
        token_amount  REAL    NOT NULL,
        target_price  REAL    NOT NULL,
        slippage_bps  INTEGER DEFAULT 200,
        status        TEXT    DEFAULT 'open',
        created_at    INTEGER DEFAULT (strftime('%s','now'))
    );
    CREATE INDEX IF NOT EXISTS idx_tpsl_user   ON tp_sl(user_id);
    CREATE INDEX IF NOT EXISTS idx_tpsl_status ON tp_sl(status);

    CREATE TABLE IF NOT EXISTS dca_orders (
        id              INTEGER PRIMARY KEY,
        user_id         INTEGER NOT NULL,
        mint            TEXT    NOT NULL,
        symbol          TEXT    NOT NULL,
        sol_per_order   REAL    NOT NULL,
        total_orders    INTEGER NOT NULL,
        executed_count  INTEGER DEFAULT 0,
        interval_secs   INTEGER NOT NULL,
        next_run        INTEGER NOT NULL,
        slippage_bps    INTEGER DEFAULT 100,
        status          TEXT    DEFAULT 'active',
        created_at      INTEGER DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS copy_targets (
        id             INTEGER PRIMARY KEY,
        user_id        INTEGER NOT NULL,
        wallet_address TEXT    NOT NULL,
        label          TEXT    NOT NULL,
        max_sol_per_trade REAL DEFAULT 0.1,
        sell_percentage   REAL DEFAULT 100,
        slippage_bps      INTEGER DEFAULT 200,
        active         INTEGER DEFAULT 1,
        created_at     INTEGER DEFAULT (strftime('%s','now')),
        UNIQUE(user_id, wallet_address)
    );
    """)
    _db().commit()

_ensure_alerts_table()

def add_alert(user_id: int, mint: str, symbol: str, condition: str, target_price: float) -> int:
    oid = int(time.time() * 1000)
    _db().execute(
        "INSERT INTO alerts (id,user_id,mint,symbol,condition,target_price) VALUES (?,?,?,?,?,?)",
        (oid, user_id, mint, symbol, condition, target_price)
    )
    _db().commit()
    return oid

def get_open_alerts(user_id: int) -> list[dict]:
    return [dict(r) for r in _db().execute(
        "SELECT * FROM alerts WHERE user_id=? AND status='open'", (user_id,)).fetchall()]

def get_all_open_alerts() -> list[tuple]:
    return [(r["user_id"], dict(r)) for r in _db().execute(
        "SELECT * FROM alerts WHERE status='open'").fetchall()]

def close_alert(user_id: int, alert_id: int):
    _db().execute("UPDATE alerts SET status='triggered' WHERE id=? AND user_id=?", (alert_id, user_id))
    _db().commit()

def delete_alert(user_id: int, alert_id: int):
    _db().execute("DELETE FROM alerts WHERE id=? AND user_id=?", (alert_id, user_id))
    _db().commit()

# ── TP/SL ─────────────────────────────────────────────────────────────────────

def add_tp_sl(user_id: int, mint: str, symbol: str, tp_sl_type: str,
              token_amount: float, target_price: float, slippage_bps: int = 200) -> int:
    oid = int(time.time() * 1000)
    _db().execute(
        "INSERT INTO tp_sl (id,user_id,mint,symbol,type,token_amount,target_price,slippage_bps) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (oid, user_id, mint, symbol, tp_sl_type, token_amount, target_price, slippage_bps)
    )
    _db().commit()
    return oid

def get_open_tp_sl(user_id: int) -> list[dict]:
    return [dict(r) for r in _db().execute(
        "SELECT * FROM tp_sl WHERE user_id=? AND status='open'", (user_id,)).fetchall()]

def get_all_open_tp_sl() -> list[tuple]:
    return [(r["user_id"], dict(r)) for r in _db().execute(
        "SELECT * FROM tp_sl WHERE status='open'").fetchall()]

def close_tp_sl(user_id: int, order_id: int, status: str = "filled"):
    _db().execute("UPDATE tp_sl SET status=? WHERE id=? AND user_id=?", (status, order_id, user_id))
    _db().commit()

# ── DCA ───────────────────────────────────────────────────────────────────────

def add_dca_order(user_id: int, mint: str, symbol: str, sol_per_order: float,
                  total_orders: int, interval_secs: int, slippage_bps: int = 100) -> int:
    oid      = int(time.time() * 1000)
    next_run = int(time.time()) + interval_secs
    _db().execute(
        "INSERT INTO dca_orders (id,user_id,mint,symbol,sol_per_order,total_orders,"
        "interval_secs,next_run,slippage_bps) VALUES (?,?,?,?,?,?,?,?,?)",
        (oid, user_id, mint, symbol, sol_per_order, total_orders, interval_secs, next_run, slippage_bps)
    )
    _db().commit()
    return oid

def get_due_dca_orders(now: int) -> list[tuple]:
    return [(r["user_id"], dict(r)) for r in _db().execute(
        "SELECT * FROM dca_orders WHERE status='active' AND next_run<=?", (now,)).fetchall()]

def update_dca_after_execution(dca_id: int, new_count: int):
    row = _db().execute("SELECT * FROM dca_orders WHERE id=?", (dca_id,)).fetchone()
    if not row:
        return
    next_run = int(time.time()) + row["interval_secs"]
    _db().execute(
        "UPDATE dca_orders SET executed_count=?, next_run=? WHERE id=?",
        (new_count, next_run, dca_id)
    )
    _db().commit()

def close_dca(dca_id: int, status: str = "completed"):
    _db().execute("UPDATE dca_orders SET status=? WHERE id=?", (status, dca_id))
    _db().commit()

def get_active_dca(user_id: int) -> list[dict]:
    return [dict(r) for r in _db().execute(
        "SELECT * FROM dca_orders WHERE user_id=? AND status='active'", (user_id,)).fetchall()]

# ── Copy trading ──────────────────────────────────────────────────────────────

def add_copy_target(user_id: int, wallet_address: str, label: str,
                    max_sol: float = 0.1, sell_pct: float = 100) -> bool:
    try:
        _db().execute(
            "INSERT INTO copy_targets (user_id,wallet_address,label,max_sol_per_trade,sell_percentage) "
            "VALUES (?,?,?,?,?)",
            (user_id, wallet_address, label, max_sol, sell_pct)
        )
        _db().commit()
        return True
    except Exception:
        return False

def remove_copy_target(user_id: int, wallet_address: str):
    _db().execute("DELETE FROM copy_targets WHERE user_id=? AND wallet_address=?",
                  (user_id, wallet_address))
    _db().commit()

def get_copy_targets(user_id: int) -> list[dict]:
    return [dict(r) for r in _db().execute(
        "SELECT * FROM copy_targets WHERE user_id=? AND active=1", (user_id,)).fetchall()]

def get_all_copy_targets() -> list[tuple]:
    return [(r["user_id"], dict(r)) for r in _db().execute(
        "SELECT * FROM copy_targets WHERE active=1").fetchall()]

# ── Analytics ─────────────────────────────────────────────────────────────────

def get_analytics(user_id: int, since_ts: int = 0) -> dict:
    rows = _db().execute(
        "SELECT side, sol_amount, price_usd, token_amount, mint, symbol, ts "
        "FROM trades WHERE user_id=? AND ts>=? ORDER BY ts",
        (user_id, since_ts)
    ).fetchall()

    total_trades = len(rows)
    total_buy_sol = total_sell_sol = 0.0
    wins = losses = 0
    best_pnl = worst_pnl = 0.0
    best_sym = worst_sym = ""

    buy_map: dict[str, dict] = {}
    for r in rows:
        mint = r["mint"]
        if r["side"] == "buy":
            total_buy_sol += r["sol_amount"]
            if mint not in buy_map:
                buy_map[mint] = {"cost": 0.0, "tokens": 0.0, "symbol": r["symbol"]}
            buy_map[mint]["cost"]   += r["token_amount"] * r["price_usd"]
            buy_map[mint]["tokens"] += r["token_amount"]
        else:
            total_sell_sol += r["sol_amount"]
            if mint in buy_map and buy_map[mint]["tokens"] > 0:
                avg_entry  = buy_map[mint]["cost"] / buy_map[mint]["tokens"]
                pnl        = r["token_amount"] * (r["price_usd"] - avg_entry)
                if pnl > 0:
                    wins += 1
                    if pnl > best_pnl:
                        best_pnl = pnl
                        best_sym = r["symbol"]
                else:
                    losses += 1
                    if pnl < worst_pnl:
                        worst_pnl = pnl
                        worst_sym = r["symbol"]

    total_pnl  = total_sell_sol - total_buy_sol
    win_rate   = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

    return {
        "total_trades":   total_trades,
        "total_buy_sol":  total_buy_sol,
        "total_sell_sol": total_sell_sol,
        "total_pnl_sol":  total_pnl,
        "wins":           wins,
        "losses":         losses,
        "win_rate":       win_rate,
        "best_trade":     (best_sym, best_pnl),
        "worst_trade":    (worst_sym, worst_pnl),
    }