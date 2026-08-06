# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Audit the watchlist on a schedule, one URL at a time.

    python -m bof.monitor --add https://example.com --brand "Example"
    python -m bof.monitor --remove https://example.com
    python -m bof.monitor --list
    python -m bof.monitor --run

``--run`` is what Task Scheduler calls. It walks the watchlist in order and
audits each URL in this process, sequentially. Never in parallel: an audit
launches Chromium up to four times and this machine has about 1.8 GB free, so
two at once is not a performance question, it is an out-of-memory question.

**If a run is already in flight it logs one line and exits 0.** It does not
retry, does not queue, and does not wait. A scheduled job that fights a person
using the tool is worse than a scheduled job that skips a day, and
-StartWhenAvailable means a missed 07:30 fires on wake anyway, which is exactly
when the OS gets opened.

Exit code is 0 whenever the schedule behaved correctly, including when it
skipped, so Task Scheduler's Last Run Result stays meaningful: non-zero means
something actually broke.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Optional

from bof import store, worker


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def run_all(out: str = "./audits") -> int:
    con = store.connect()
    targets = store.watchlist(con)
    if not targets:
        log("watchlist is empty, nothing to do")
        con.close()
        return 0

    busy = store.active(con)
    if busy is not None:
        # Deliberately not an error: a person is using the tool.
        log(f"skipped, run {busy['id']} in flight ({busy['status']})")
        con.close()
        return 0

    log(f"auditing {len(targets)} watched url(s)")
    ok = failed = 0
    for row in targets:
        url, brand = row["url"], row["brand"]
        try:
            run_id = store.enqueue(con, url, brand)
        except store.Busy as exc:
            # Something else grabbed the slot between the check above and here.
            # The database refused it, which is the point of the unique index.
            log(f"skipped {url}, run {exc.run_id} took the slot")
            break
        log(f"start {run_id} {url}")
        # In-process, not detached: this is already a background task with no
        # terminal to block, and running it here means the scheduler's exit
        # code reflects what actually happened.
        code = worker.execute(run_id, out)
        finished = store.get(con, run_id)
        if code == 0:
            ok += 1
            log(f"done  {run_id} {url} headline {finished['overall']}")
        else:
            failed += 1
            first = (finished["error"] or "").strip().splitlines()
            log(f"FAIL  {run_id} {url}: {first[-1][:200] if first else 'no detail'}")

    con.close()
    log(f"finished: {ok} ok, {failed} failed")
    return 1 if failed else 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--add", metavar="URL")
    ap.add_argument("--brand")
    ap.add_argument("--remove", metavar="URL")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--out", default="./audits")
    args = ap.parse_args(argv)

    if args.run:
        return run_all(args.out)

    con = store.connect()
    try:
        if args.add:
            url = args.add if "://" in args.add else f"https://{args.add}"
            from bof import suite  # noqa: F401
            from url_safety import URLSafetyError, validate_url_strict
            try:
                url, _ip = validate_url_strict(url)
            except (URLSafetyError, ValueError) as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 1
            store.watch_add(con, url, args.brand)
            print(f"watching {url}")
            return 0
        if args.remove:
            url = args.remove if "://" in args.remove else f"https://{args.remove}"
            print(f"removed {url}" if store.watch_remove(con, url)
                  else f"{url} was not on the watchlist")
            return 0
        rows = store.watchlist(con, enabled_only=False)
        if not rows:
            print("watchlist is empty")
            return 0
        print(f"{'url':<52}{'brand':<24}added")
        for r in rows:
            print(f"{r['url']:<52}{(r['brand'] or '-'):<24}{r['added_at']}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
