# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Run history. One SQLite file, no ORM, no migrations.

    python -m bof.store --self-check

The database lives at %LOCALAPPDATA%\\ai-visibility-audit\\runs.db, deliberately
outside any synced folder: a cloud sync client copying a live -wal/-shm pair
while it is being written is a corruption path. Override with BOF_AUDIT_DB.

WAL is set at creation so a dashboard can read this file live while an audit is
still writing to it.

One run at a time is enforced by the database, not by application code. The
``active`` generated column is 1 while a row is queued or running and NULL
otherwise, and NULLs never collide in a SQLite unique index, so the index
permits exactly one in-flight row and unlimited finished ones. Status is the
single source of truth and ``active`` is derived from it, so the two cannot
drift apart.

A run is stalled by heartbeat, never by checking whether its PID is alive:
os.kill(pid, 0) is unreliable on Windows. The pid is stored only so a human can
kill the thing.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from typing import Optional

#: Seconds without a heartbeat before an in-flight run is presumed dead. The
#: slowest real boundary is the render, comfortably under a minute.
STALL_AFTER = 180

STEP_TOTAL = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  brand TEXT,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','done','failed','stalled','cancelled')),
  step TEXT,
  step_no INTEGER NOT NULL DEFAULT 0,
  step_total INTEGER NOT NULL DEFAULT 7,
  pid INTEGER,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  ended_at TEXT,
  cancel INTEGER NOT NULL DEFAULT 0,
  scoring_version INTEGER,
  engine_readability INTEGER,
  agent_operability INTEGER,
  social_surface INTEGER,
  social_coverage REAL,
  future_readiness INTEGER,
  seo INTEGER,
  seo_field INTEGER,
  overall INTEGER,
  out_dir TEXT,
  error TEXT,
  active INTEGER GENERATED ALWAYS AS
    (CASE WHEN status IN ('queued','running') THEN 1 END) VIRTUAL
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active ON runs(active);
CREATE INDEX IF NOT EXISTS runs_url_time ON runs(url, started_at DESC);

CREATE TABLE IF NOT EXISTS watchlist (
  url TEXT PRIMARY KEY,
  brand TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

#: Envelope key -> column. engine_readability and agent_operability are
#: nullable on purpose: NULL means the lens did not apply to that page, which
#: is a different fact from a score of zero.
SCORE_COLUMNS = {
    "engine_readability": "engine_readability",
    "agent_operability": "agent_operability",
    "social_surface": "social_surface",
    "social_coverage": "social_coverage",
    "future_readiness": "future_readiness",
    "seo": "seo",
    "seo_field": "seo_field",
    "scoring_version": "scoring_version",
}


class Busy(RuntimeError):
    """Another run is already in flight. Carries its id so a caller can say so."""

    def __init__(self, run_id: Optional[str]):
        self.run_id = run_id
        super().__init__(f"a run is already in flight: {run_id}")


def db_path() -> pathlib.Path:
    override = os.environ.get("BOF_AUDIT_DB")
    if override:
        return pathlib.Path(override)
    local = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return pathlib.Path(local) / "ai-visibility-audit" / "runs.db"


def connect(path: Optional[pathlib.Path] = None) -> sqlite3.Connection:
    """Open, create if needed, and sweep anything that died without saying so."""
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    # Not a migration framework, just the one column L7 added after rows already
    # existed. Idempotent, and older rows keep a NULL fingerprint, which trend.py
    # reads as "no per-signal history" rather than as "nothing changed".
    if "signal_classes" not in {r[1] for r in con.execute("PRAGMA table_info(runs)")}:
        con.execute("ALTER TABLE runs ADD COLUMN signal_classes TEXT")
        con.commit()
    sweep(con)
    return con


def sweep(con: sqlite3.Connection) -> int:
    """Mark as stalled anything that stopped heartbeating. Returns the count."""
    cur = con.execute(
        "UPDATE runs SET status='stalled', ended_at=datetime('now') "
        "WHERE status IN ('queued','running') "
        "  AND (julianday('now') - julianday(updated_at)) * 86400 > ?",
        (STALL_AFTER,))
    con.commit()
    return cur.rowcount


def active(con: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM runs WHERE active = 1").fetchone()


def enqueue(con: sqlite3.Connection, url: str, brand: Optional[str] = None) -> str:
    """Insert a queued run. Raises Busy if one is already in flight.

    The refusal comes from the unique index, not from a check-then-insert:
    Chromium plus two Python services on 1.8 GB free is not where to discover a
    race between reading and writing.
    """
    run_id = uuid.uuid4().hex[:12]
    try:
        con.execute("INSERT INTO runs (id, url, brand, step_total) VALUES (?,?,?,?)",
                    (run_id, url, brand, STEP_TOTAL))
        con.commit()
    except sqlite3.IntegrityError:
        row = active(con)
        raise Busy(row["id"] if row else None) from None
    return run_id


def get(con: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def start(con: sqlite3.Connection, run_id: str, pid: int) -> None:
    con.execute("UPDATE runs SET status='running', pid=?, "
                "updated_at=datetime('now') WHERE id=?", (pid, run_id))
    con.commit()


def step(con: sqlite3.Connection, run_id: str, n: int, label: str) -> bool:
    """Write the heartbeat, return whether a cancel has been requested."""
    con.execute("UPDATE runs SET step=?, step_no=?, updated_at=datetime('now') "
                "WHERE id=?", (label, n, run_id))
    con.commit()
    row = get(con, run_id)
    return bool(row and row["cancel"])


def finish(con: sqlite3.Connection, run_id: str, scores: dict, overall: int,
           out_dir: str) -> None:
    cols = ", ".join(f"{c}=?" for c in SCORE_COLUMNS.values())
    values = [scores.get(k) for k in SCORE_COLUMNS]
    # The per-signal fingerprint is stored because the report directory is
    # rewritten on every run of the same domain, so last run's audit-data.json
    # is already gone by the time anyone wants to diff against it.
    classes = scores.get("signal_classes")
    con.execute(
        f"UPDATE runs SET status='done', {cols}, overall=?, out_dir=?, "
        f"signal_classes=?, step_no=step_total, ended_at=datetime('now'), "
        f"updated_at=datetime('now') WHERE id=?",
        (*values, overall, out_dir, json.dumps(classes) if classes else None,
         run_id))
    con.commit()


def fail(con: sqlite3.Connection, run_id: str, error: str) -> None:
    con.execute("UPDATE runs SET status='failed', error=?, "
                "ended_at=datetime('now'), updated_at=datetime('now') "
                "WHERE id=?", (str(error)[:2000], run_id))
    con.commit()


def cancel(con: sqlite3.Connection, run_id: str) -> bool:
    """Ask a run to stop. The worker notices at its next step boundary."""
    cur = con.execute("UPDATE runs SET cancel=1, updated_at=datetime('now') "
                      "WHERE id=? AND status IN ('queued','running')", (run_id,))
    con.commit()
    return cur.rowcount > 0


def cancelled(con: sqlite3.Connection, run_id: str) -> None:
    con.execute("UPDATE runs SET status='cancelled', ended_at=datetime('now'), "
                "updated_at=datetime('now') WHERE id=?", (run_id,))
    con.commit()


def latest(con: sqlite3.Connection, url: Optional[str] = None,
           limit: int = 20) -> list:
    if url:
        return con.execute(
            "SELECT * FROM runs WHERE url=? ORDER BY started_at DESC, rowid DESC "
            "LIMIT ?", (url, limit)).fetchall()
    return con.execute(
        "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
        (limit,)).fetchall()


def watch_add(con: sqlite3.Connection, url: str, brand: Optional[str] = None) -> None:
    con.execute("INSERT INTO watchlist (url, brand) VALUES (?,?) "
                "ON CONFLICT(url) DO UPDATE SET brand=excluded.brand, enabled=1",
                (url, brand))
    con.commit()


def watch_remove(con: sqlite3.Connection, url: str) -> bool:
    cur = con.execute("DELETE FROM watchlist WHERE url=?", (url,))
    con.commit()
    return cur.rowcount > 0


def watchlist(con: sqlite3.Connection, enabled_only: bool = True) -> list:
    sql = "SELECT * FROM watchlist"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return con.execute(sql + " ORDER BY added_at").fetchall()


def spawn(run_id: str, out: str = ".") -> int:
    """Launch the worker detached, and return its pid.

    A detached process rather than a thread: a thread dies silently when its
    parent restarts and orphans Chromium, whereas a detached child keeps
    writing its state to this database and the UI simply reconnects.
    """
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
             | getattr(subprocess, "DETACHED_PROCESS", 0))
    proc = subprocess.Popen(
        [sys.executable, "-m", "bof.worker", run_id, out],
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags)
    return proc.pid


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def self_check() -> int:
    import shutil

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bof-store-"))
    failures = []
    try:
        con = connect(tmp / "runs.db")

        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            failures.append(f"journal_mode is {mode}, not wal; DuckDB will read "
                            f"this file live")
        else:
            print("  ok    journal_mode is wal")

        first = enqueue(con, "https://a.test/", "A")
        try:
            enqueue(con, "https://b.test/", "B")
            failures.append("a second run was accepted while one was in flight")
        except Busy as exc:
            if exc.run_id != first:
                failures.append(f"Busy named {exc.run_id}, expected {first}")
            else:
                print(f"  ok    a second concurrent run is refused, naming {first}")

        start(con, first, pid=4242)
        if step(con, first, 3, "future readiness"):
            failures.append("a fresh run reported itself cancelled")
        row = get(con, first)
        if row["step_no"] != 3 or row["step"] != "future readiness":
            failures.append(f"step() did not record progress: {dict(row)}")
        else:
            print("  ok    step() advances the counter and the label")

        finish(con, first, {"engine_readability": 64, "agent_operability": 78,
                            "social_surface": 41, "social_coverage": 0.75,
                            "future_readiness": 0, "seo": 66, "seo_field": None,
                            "scoring_version": 3}, overall=69, out_dir="x")
        row = get(con, first)
        unset = [c for c in SCORE_COLUMNS.values()
                 if c != "seo_field" and row[c] is None]
        if unset:
            failures.append(f"a completed run left score columns unset: {unset}")
        elif row["overall"] != 69 or row["step_no"] != row["step_total"]:
            failures.append(f"finish() did not close the run out: {dict(row)}")
        else:
            print("  ok    a completed run records every score column")

        second = enqueue(con, "https://a.test/", "A")
        third_blocked = True
        try:
            enqueue(con, "https://c.test/", None)
            third_blocked = False
        except Busy:
            pass
        if not third_blocked:
            failures.append("two runs were in flight at once")
        else:
            print("  ok    finishing a run frees the slot for exactly one more")

        if not cancel(con, second):
            failures.append("cancel() did not mark the in-flight run")
        elif not step(con, second, 1, "render"):
            failures.append("the worker was not told to stop after cancel()")
        else:
            print("  ok    cancel() is seen at the next step boundary")
        cancelled(con, second)

        # Two finished rows for the same url, plus one that must not be swept.
        fresh = enqueue(con, "https://a.test/", "A")
        start(con, fresh, pid=1)
        if sweep(con) != 0:
            failures.append("a run heartbeating right now was swept as stalled")
        else:
            print("  ok    a live run is not swept")

        con.execute("UPDATE runs SET updated_at=datetime('now','-400 seconds') "
                    "WHERE id=?", (fresh,))
        con.commit()
        if sweep(con) != 1:
            failures.append("a run with a 400-second-old heartbeat was not swept")
        elif get(con, fresh)["status"] != "stalled":
            failures.append("the swept run is not marked stalled")
        else:
            print(f"  ok    a run silent for over {STALL_AFTER}s is swept to stalled")

        if active(con) is not None:
            failures.append("sweeping left a row still marked active")
        else:
            print("  ok    sweeping frees the slot")

        watch_add(con, "https://w.test/", "W")
        watch_add(con, "https://w.test/", "W2")   # same url again
        watch_add(con, "https://x.test/", None)
        wl = watchlist(con)
        if len(wl) != 2:
            failures.append(f"watchlist has {len(wl)} rows, expected 2: adding the "
                            f"same url twice must update, not duplicate")
        elif wl[0]["brand"] != "W2":
            failures.append("re-adding a watched url did not update its brand")
        elif not watch_remove(con, "https://x.test/") or len(watchlist(con)) != 1:
            failures.append("watch_remove did not remove exactly one row")
        else:
            print("  ok    watchlist upserts by url and removes cleanly")

        rows = latest(con, "https://a.test/")
        if len(rows) != 3:
            failures.append(f"latest() found {len(rows)} rows for the url, expected 3")
        elif [r["id"] for r in rows][0] != fresh:
            failures.append("latest() is not newest first")
        else:
            print(f"  ok    latest() filters by url, newest first ({len(rows)} rows)")

        con.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll run-store self-checks passed.")
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--path", action="store_true", help="print the database path")
    args = ap.parse_args(argv)
    if args.path:
        print(db_path())
        return 0
    if args.self_check:
        return self_check()
    ap.error("nothing to do; try --self-check or --path")


if __name__ == "__main__":
    raise SystemExit(main())
