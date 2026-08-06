# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Start an audit and watch it, without blocking a terminal on it.

    python -m bof.audit https://example.com --brand "Brand" [--out ./audits]
    python -m bof.audit --watch <run_id>
    python -m bof.audit --list [--url https://example.com]
    python -m bof.audit --cancel <run_id>

The audit runs in a detached child, so closing this terminal does not kill it
and a later --watch reconnects to whatever is in flight. L3 puts HTTP over
exactly these calls rather than inventing a second path to the same thing.
"""

from __future__ import annotations

import argparse
import time
from typing import Optional

from bof import store

WATCH_INTERVAL = 1.5  # the job runs for minutes; polling faster buys nothing


def _line(row) -> str:
    pct = int(100 * row["step_no"] / (row["step_total"] or 1))
    head = f"{row['status']:<9} {pct:>3}%  {row['step'] or '-'}"
    if row["status"] == "done":
        return (f"{head}\n  engine {row['engine_readability']}  "
                f"operability {row['agent_operability']}  seo {row['seo']}  "
                f"headline {row['overall']}\n  {row['out_dir']}")
    if row["status"] in ("failed", "stalled"):
        return f"{head}\n  {(row['error'] or 'no heartbeat').splitlines()[-1][:200]}"
    return head


def watch(con, run_id: str) -> int:
    last = None
    while True:
        store.sweep(con)
        row = store.get(con, run_id)
        if row is None:
            print(f"no such run: {run_id}")
            return 2
        now = (row["status"], row["step_no"])
        if now != last:
            print(_line(row))
            last = now
        if row["status"] in ("done", "failed", "stalled", "cancelled"):
            return 0 if row["status"] == "done" else 1
        time.sleep(WATCH_INTERVAL)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", nargs="?")
    ap.add_argument("--brand", help="brand name; enables the social surface")
    ap.add_argument("--out", default="./audits")
    ap.add_argument("--watch", metavar="RUN_ID")
    ap.add_argument("--follow", action="store_true",
                    help="start the run, then watch it in this terminal")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--url", help="filter --list by url")
    ap.add_argument("--cancel", metavar="RUN_ID")
    args = ap.parse_args(argv)

    con = store.connect()

    if args.list:
        rows = store.latest(con, args.url)
        if not rows:
            print("no runs yet")
            return 0
        print(f"{'id':<14}{'status':<10}{'headline':>9}  {'started':<20} url")
        for r in rows:
            head = "-" if r["overall"] is None else str(r["overall"])
            print(f"{r['id']:<14}{r['status']:<10}{head:>9}  "
                  f"{r['started_at']:<20} {r['url']}")
        return 0

    if args.cancel:
        if store.cancel(con, args.cancel):
            print(f"cancel requested for {args.cancel}; it stops at the next "
                  f"step boundary so Chromium closes cleanly")
            return 0
        print(f"{args.cancel} is not in flight")
        return 1

    if args.watch:
        return watch(con, args.watch)

    if not args.url:
        ap.error("a url is required unless --watch, --list or --cancel is given")

    try:
        run_id = store.enqueue(con, args.url, args.brand)
    except store.Busy as exc:
        print(f"a run is already in flight: {exc.run_id}\n"
              f"  watch it:  python -m bof.audit --watch {exc.run_id}\n"
              f"  stop it:   python -m bof.audit --cancel {exc.run_id}")
        return 9

    pid = store.spawn(run_id, args.out)
    print(f"started {run_id} (pid {pid})")
    if args.follow:
        return watch(con, run_id)
    print(f"  watch:  python -m bof.audit --watch {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
