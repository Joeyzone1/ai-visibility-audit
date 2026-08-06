# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""One audit, in its own process, reporting to the run store.

    python -m bof.worker <run_id>

Normally spawned detached by ``store.spawn()``. It never talks to a user: its
whole output is the row it keeps updating, which is what lets the UI reconnect
to a run it did not start and lets a closed terminal not kill an audit.

Cancellation is cooperative and checked at the same seven boundaries the
progress hook uses. Raising out of ``report.run()`` unwinds through
``sync_playwright()``'s context manager, so Chromium closes rather than being
orphaned, which matters more than a fast exit on a machine with 1.8 GB free.
"""

from __future__ import annotations

import os
import pathlib
import sys
import traceback
from typing import Optional

from bof import report, store


def execute(run_id: str, out: str = ".") -> int:
    con = store.connect()
    row = store.get(con, run_id)
    if row is None:
        print(f"no such run: {run_id}", file=sys.stderr)
        return 2
    if row["status"] not in ("queued", "running"):
        print(f"run {run_id} is already {row['status']}", file=sys.stderr)
        return 2

    store.start(con, run_id, os.getpid())

    def on_step(n: int, label: str) -> None:
        if store.step(con, run_id, n, label):
            raise report.Cancelled(f"run {run_id} was cancelled at step {n}")

    try:
        result = report.run(row["url"], brand=row["brand"], out=out,
                            on_step=on_step)
    except report.Cancelled:
        store.cancelled(con, run_id)
        return 3
    except Exception:  # noqa: BLE001 - the row is the only place to report it
        store.fail(con, run_id, traceback.format_exc()[-2000:])
        return 1

    if result.get("error"):
        store.fail(con, run_id, result["error"])
        return 1

    envelope = result["envelope"]
    data = result.get("audit_data")
    store.finish(con, run_id, envelope["bof"],
                 overall=envelope["summary"]["health_score"],
                 out_dir=str(pathlib.Path(data).parent if data else out))
    return 0


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 1:
        print("usage: python -m bof.worker <run_id> [out_dir]", file=sys.stderr)
        return 2
    return execute(argv[0], argv[1] if len(argv) > 1 else ".")


if __name__ == "__main__":
    raise SystemExit(main())
