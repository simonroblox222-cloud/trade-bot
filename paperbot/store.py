"""SQLite persistence. The bot writes, the dashboard only reads."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

-- one row per position (aggregate of its buy + sell fills)
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mint TEXT, symbol TEXT, source TEXT, status TEXT,   -- open | closed
  opened_at REAL, closed_at REAL,
  size_sol REAL,           -- SOL swapped in
  cost_sol REAL,           -- size + network fees + tip + ATA rent
  token_amount INTEGER, decimals INTEGER,
  entry_reason TEXT, exit_reason TEXT,
  peak_value_sol REAL, last_value_sol REAL, quote_failures INTEGER DEFAULT 0,
  proceeds_sol REAL, pnl_sol REAL, pnl_pct REAL, fees_sol REAL
);

-- one row per simulated fill; every trade carries its reason
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, position_id INTEGER, mint TEXT, symbol TEXT,
  side TEXT,               -- BUY | SELL
  reason TEXT,
  sol_amount REAL,         -- SOL in (BUY) or SOL out after slippage (SELL), before network fees
  token_amount INTEGER,
  quote_out INTEGER,       -- Jupiter quote outAmount (already includes AMM/LP fees + price impact)
  fill_out INTEGER,        -- simulated fill after extra slippage
  price_impact_pct REAL, slippage_cost_sol REAL,
  network_fee_sol REAL,    -- base + priority + jito tip
  rent_sol REAL,           -- + paid on BUY, - refunded on SELL
  route TEXT, pnl_sol REAL
);

CREATE TABLE IF NOT EXISTS equity (ts REAL, cash REAL, positions_value REAL, equity REAL);
CREATE TABLE IF NOT EXISTS events (ts REAL, agent TEXT, level TEXT, mint TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS rugchecks (ts REAL, mint TEXT, symbol TEXT, source TEXT, passed INTEGER,
  score REAL, top10_pct REAL, lp_locked_pct REAL, roundtrip_loss_pct REAL, reasons TEXT, warnings TEXT);
-- every Critic decision before a buy: approved or rejected, with reasons
CREATE TABLE IF NOT EXISTS critic (ts REAL, mint TEXT, symbol TEXT, source TEXT, approved INTEGER,
  size_sol REAL, social_score REAL, reasons TEXT, notes TEXT);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class Store:
    def __init__(self, path: str, readonly: bool = False):
        uri = f"file:{path}?mode=ro" if readonly else f"file:{path}"
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        if not readonly:
            with self.lock:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.executescript(SCHEMA)
                cols = {r[1] for r in self.conn.execute("PRAGMA table_info(positions)")}
                for col, typ in (("bonding", "INTEGER DEFAULT 0"),           # added with the Critic
                                 ("partial_proceeds_sol", "REAL DEFAULT 0"),  # partial take-profits
                                 ("tp_level", "INTEGER DEFAULT 0"),
                                 ("entry_value_sol", "REAL")):                # first mark after the buy
                    if col not in cols:  # older DBs get the new columns
                        self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {typ}")
                self.conn.commit()

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args)]

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    # kv ------------------------------------------------------------------
    def get(self, k: str, default=None):
        rows = self._rows("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(rows[0]["v"]) if rows else default

    def set(self, k: str, v) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v)))

    # positions / trades --------------------------------------------------
    def insert(self, table: str, **cols) -> int:
        return self._exec(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          tuple(cols.values()))

    def update_position(self, pid: int, **cols) -> None:
        self._exec(f"UPDATE positions SET {','.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), pid))

    def open_positions(self) -> list[dict]:
        return self._rows("SELECT * FROM positions WHERE status='open' ORDER BY id")

    def closed_positions(self, limit: int = 1000) -> list[dict]:
        return self._rows("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT ?", (limit,))

    def trades(self, limit: int = 200) -> list[dict]:
        return self._rows("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def last_exit_ts(self, mint: str) -> float | None:
        rows = self._rows("SELECT MAX(closed_at) m FROM positions WHERE mint=?", (mint,))
        return rows[0]["m"] if rows else None

    def stats(self) -> dict:
        r = self._rows("""SELECT COUNT(*) n, SUM(pnl_sol>0) wins, SUM(pnl_sol<=0) losses,
                                 COALESCE(SUM(pnl_sol),0) realized, COALESCE(SUM(fees_sol),0) fees,
                                 MAX(pnl_sol) best, MIN(pnl_sol) worst
                          FROM positions WHERE status='closed'""")[0]
        return {k: (r[k] or 0) for k in r}

    # equity / events -----------------------------------------------------
    def record_equity(self, cash: float, pos_value: float) -> None:
        self._exec("INSERT INTO equity VALUES (?,?,?,?)", (time.time(), cash, pos_value, cash + pos_value))

    def equity_curve(self, limit: int = 3000) -> list[dict]:
        return list(reversed(self._rows("SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,))))

    def event(self, agent: str, level: str, message: str, mint: str | None = None) -> None:
        self._exec("INSERT INTO events VALUES (?,?,?,?,?)", (time.time(), agent, level, mint, message))

    def events(self, limit: int = 150) -> list[dict]:
        return self._rows("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))

    def record_rugcheck(self, rep, cand) -> None:
        self._exec("INSERT INTO rugchecks VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            time.time(), rep.mint, cand.symbol, cand.source, int(rep.passed), rep.rugcheck_score,
            rep.top10_pct, rep.lp_locked_pct, rep.roundtrip_loss_pct,
            json.dumps(rep.reasons), json.dumps(rep.warnings)))

    def record_critic(self, cand, approved: bool, size_sol: float, reasons: list[str], notes: list[str]) -> None:
        self._exec("INSERT INTO critic VALUES (?,?,?,?,?,?,?,?,?)", (
            time.time(), cand.mint, cand.symbol, cand.source, int(approved), size_sol, cand.social_score,
            json.dumps(reasons), json.dumps(notes)))

    def critic(self, limit: int = 60) -> list[dict]:
        out = self._rows("SELECT * FROM critic ORDER BY ts DESC LIMIT ?", (limit,))
        for d in out:
            d["reasons"] = json.loads(d["reasons"])
            d["notes"] = json.loads(d["notes"])
        return out

    def critic_counts(self) -> dict:
        r = self._rows("SELECT COUNT(*) n, COALESCE(SUM(approved),0) ok FROM critic")[0]
        return {"approved": int(r["ok"]), "rejected": int(r["n"] - r["ok"])}

    def rugchecks(self, limit: int = 60) -> list[dict]:
        out = self._rows("SELECT * FROM rugchecks ORDER BY ts DESC LIMIT ?", (limit,))
        for d in out:
            d["reasons"] = json.loads(d["reasons"])
            d["warnings"] = json.loads(d["warnings"])
        return out
