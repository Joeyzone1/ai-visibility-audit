# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Score history per domain, with a gate on what counts as a change.

    python -m bof.trend                       every watched url
    python -m bof.trend --url https://x.com   one url
    python -m bof.trend --self-check

A page re-audited on two ordinary days will not produce the same number twice.
A crawler is slower, a sitemap fetch times out, a passage lands a word either
side of a threshold. Reporting every one of those as movement teaches a client
to ignore the number, which costs more than the number is worth.

So a change is **material** only when the headline moved by at least
MATERIAL_POINTS, or when some signal changed class: satisfied to critical,
critical to warning, measured to not-applicable. Below that bar the previous
headline is what gets shown, and the report says the score held rather than
inventing a trend out of jitter.

The asymmetry is deliberate. A one-point drop with three signals newly critical
IS material, because the classes moved. A two-point rise with nothing reclassed
is not, however much a client would enjoy hearing about it.
"""

from __future__ import annotations

import argparse
import json
from typing import Optional

from bof import store

#: Headline movement, in points, that counts on its own. Three is roughly the
#: width of the jitter observed re-running the same unchanged page.
MATERIAL_POINTS = 3


def _classes(row) -> dict:
    raw = row["signal_classes"] if "signal_classes" in row.keys() else None
    try:
        parsed = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        parsed = None
    # A stored JSON null parses to None, which is not a dict and must not reach
    # set(). An absent fingerprint means no per-signal history, so nothing can
    # flip and the point delta decides on its own.
    return parsed if isinstance(parsed, dict) else {}


def compare(previous, latest) -> dict:
    """Two run rows in, one verdict out. Pure."""
    delta = (latest["overall"] or 0) - (previous["overall"] or 0)
    before, after = _classes(previous), _classes(latest)

    # Only signals present in both runs can flip. A signal that appeared or
    # vanished is a change in what was measured, not in what was found.
    flips = sorted(sid for sid in set(before) & set(after)
                   if before[sid] != after[sid])
    flip_detail = [{"id": sid, "from": before[sid], "to": after[sid]}
                   for sid in flips]

    material = abs(delta) >= MATERIAL_POINTS or bool(flips)
    return {
        "url": latest["url"],
        "previous": previous["overall"],
        "latest": latest["overall"],
        "delta": delta,
        "flips": flip_detail,
        "material": material,
        # What to put in front of a person. Suppressed movement shows the
        # number that still stands, not the new one.
        "headline": latest["overall"] if material else previous["overall"],
        "reason": (
            f"moved {delta:+d} points" if abs(delta) >= MATERIAL_POINTS and not flips
            else f"moved {delta:+d} points, {len(flips)} signal(s) changed class"
            if material and flips
            else f"held at {previous['overall']}: {delta:+d} points and no signal "
                 f"changed class, which is inside normal re-run variation"),
        "compared_at": latest["started_at"],
        "against": previous["started_at"],
    }


def history(con, url: str, limit: int = 20) -> list:
    return [r for r in store.latest(con, url, limit) if r["status"] == "done"]


def for_url(con, url: str) -> Optional[dict]:
    runs = history(con, url, limit=2)
    if len(runs) < 2:
        return None
    return compare(runs[1], runs[0])   # latest() is newest first


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)

    if args.self_check:
        return self_check()

    con = store.connect()
    urls = [args.url] if args.url else [r["url"] for r in store.watchlist(con)]
    if not urls:
        print("nothing to compare: pass --url or add something with bof.monitor --add")
        return 0

    out = []
    for url in urls:
        verdict = for_url(con, url)
        if verdict is None:
            runs = history(con, url, limit=1)
            print(f"{url}\n  only {len(runs)} finished run, nothing to compare yet")
            continue
        out.append(verdict)
        if args.json:
            continue
        arrow = "=" if not verdict["material"] else ("^" if verdict["delta"] > 0 else "v")
        print(f"{verdict['url']}")
        print(f"  {arrow} {verdict['headline']}/100   {verdict['reason']}")
        for f in verdict["flips"]:
            print(f"      {f['id']}: {f['from']} -> {f['to']}")
    con.close()
    if args.json:
        print(json.dumps(out, indent=2))
    return 0


# --------------------------------------------------------------------------

def self_check() -> int:
    failures = []

    def row(overall, classes, at="2026-01-01 00:00:00", url="https://x.test/"):
        return {"overall": overall, "url": url, "started_at": at,
                "signal_classes": json.dumps(classes)}

    class Row(dict):
        """sqlite3.Row exposes keys() and [] the same way; mimic just that."""
        def keys(self):  # noqa: D102
            return list(super().keys())

    base = {"A1": "ok", "B1": "critical", "C3": "warning"}

    # 1. Two points and nothing reclassed: suppressed.
    v = compare(Row(row(69, base)), Row(row(71, base)))
    if v["material"]:
        failures.append(f"+2 with no class change was treated as material: {v['reason']}")
    elif v["headline"] != 69:
        failures.append(f"suppressed change showed {v['headline']}, expected the "
                        f"prior 69")
    else:
        print(f"  ok    +2 with nothing reclassed holds at 69 ({v['reason']})")

    # 2. Three points, nothing reclassed: material, because the bar is 3.
    v = compare(Row(row(69, base)), Row(row(72, base)))
    if not v["material"] or v["headline"] != 72 or v["delta"] != 3:
        failures.append(f"+3 should be material and show 72: {v}")
    else:
        print("  ok    +3 clears the bar and shows the new number")

    # 3. One point, but a signal went from satisfied to critical. Material.
    worse = dict(base, A1="critical")
    v = compare(Row(row(69, base)), Row(row(70, worse)))
    if not v["material"]:
        failures.append("a signal flipping ok -> critical was suppressed as noise")
    elif v["headline"] != 70 or [f["id"] for f in v["flips"]] != ["A1"]:
        failures.append(f"class flip not reported correctly: {v}")
    else:
        print("  ok    +1 with a signal newly critical is material, and names it")

    # 4. Negative deltas are not treated more kindly than positive ones.
    v = compare(Row(row(80, base)), Row(row(78, base)))
    if v["material"] or v["headline"] != 80:
        failures.append(f"-2 was handled differently from +2: {v}")
    else:
        print("  ok    -2 is suppressed exactly like +2")

    v = compare(Row(row(80, base)), Row(row(76, base)))
    if not v["material"] or v["delta"] != -4:
        failures.append(f"-4 should be material: {v}")
    else:
        print("  ok    -4 clears the bar and reports a drop")

    # 5. A signal that only exists in one run is not a flip: what was measured
    #    changed, not what was found.
    v = compare(Row(row(69, base)), Row(row(70, dict(base, E1="ok"))))
    if v["flips"]:
        failures.append(f"a signal appearing counted as a class flip: {v['flips']}")
    else:
        print("  ok    a signal appearing or vanishing is not a class flip")

    # 6. No fingerprint at all (rows written before L7) must not read as
    #    "nothing changed" and silently suppress a real move.
    v = compare(Row(row(69, None)), Row(row(78, None)))
    if not v["material"]:
        failures.append("a 9-point move with no stored classes was suppressed")
    else:
        print("  ok    missing fingerprints do not suppress a real move")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll trend self-checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
