# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""SEO Technical Baseline: supporting evidence, never the headline.

    python -m bof.seo_core https://example.com [--json]
    python -m bof.seo_core --self-check

Nine signals summing to 100, defined in ``signals.json`` under ``seo``. Every
one is presence, syntax, a count, a threshold or HTTP semantics, so the same
input gives the same score forever. What it cannot say is listed in the report
next to what it can, because the gap between "has a heading" and "has a
meaningful heading" is where SEO tools lose their credibility.

This module makes no network call at all. It scores the evidence
``agent_ready.gather()`` already collected: one render, one robots.txt, one
sitemap probe. A second fetch here would mean a fifth Chromium launch on a
machine with 1.8 GB free, which is the reason the whole audit is sequential.

The score is deliberately absent from ``report.headline_weights``. A check in
bof.report asserts that moving it from 0 to 100 leaves the AI Visibility Score
untouched. If that check ever fails, this has become an SEO tool.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Optional
from urllib.parse import urlparse

from bof import SCORING_VERSION
from bof import suite  # noqa: F401  # must precede every suite import
from bof.agent_ready import load_config, node_types, schema_nodes

from content_quality import analyse as content_analyse  # noqa: E402
from preload_check import analyse as preload_analyse  # noqa: E402

_NOINDEX_RE = re.compile(r"\bnone\b|\bnoindex\b", re.I)


def _pct(part: float, whole: float) -> float:
    return part / whole if whole else 0.0


def _headers(ev: dict) -> dict:
    """Header lookup is case-insensitive; the servers are not consistent."""
    raw = (ev.get("render") or {}).get("headers") or {}
    return {str(k).lower(): v for k, v in raw.items()}


# --------------------------------------------------------------------------
# signals: each takes (evidence, seo config) and returns ratio 0-1 plus working
# --------------------------------------------------------------------------

def s_e1(ev: dict, cfg: dict) -> dict:
    """Indexable: status, robots directives, canonical."""
    parts = cfg["parts"]["E1"]
    render = ev.get("render") or {}
    parsed = ev.get("parsed") or {}
    status = render.get("status_code")
    meta_robots = parsed.get("meta_robots") or ""
    header_robots = str(_headers(ev).get("x-robots-tag") or "")
    canonical = parsed.get("canonical") or ""

    earned, detail = 0, {}
    detail["status_code"] = status
    if status == 200:
        earned += parts["status_ok"]

    blocked = bool(_NOINDEX_RE.search(meta_robots) or _NOINDEX_RE.search(header_robots))
    detail.update(meta_robots=meta_robots or None,
                  x_robots_tag=header_robots or None, noindex=blocked)
    if not blocked:
        earned += parts["no_noindex"]

    page = ev.get("final_url") or ev.get("url") or ""
    absolute = canonical.startswith(("http://", "https://"))
    # Compared without scheme, trailing slash or fragment: a canonical differing
    # only in those is self-referential in every way that matters.
    self_ref = absolute and _norm(canonical) == _norm(page)
    detail.update(canonical=canonical or None, canonical_absolute=absolute,
                  self_referential=self_ref)
    if self_ref:
        earned += parts["canonical"]

    total = sum(parts.values())
    how = "HTTP status, robots meta and X-Robots-Tag header, canonical link"
    if status is None:
        return {"ratio": 0.0, "how": "the page was never fetched", "detail": detail,
                "unmeasured": True}
    return {"ratio": _pct(earned, total), "how": how, "detail": detail}


def _norm(url: str) -> str:
    p = urlparse(url)
    return f"{p.netloc.lower()}{p.path.rstrip('/')}".lower()


def s_e2(ev: dict, cfg: dict) -> dict:
    """Title, description, social preview."""
    parts, parsed = cfg["parts"]["E2"], ev.get("parsed") or {}
    title = (parsed.get("title") or "").strip()
    desc = (parsed.get("meta_description") or "").strip()
    og = {k.lower(): v for k, v in (parsed.get("open_graph") or {}).items()}

    tl, dl = cfg["title_length"], cfg["description_length"]
    earned = 0
    if tl["min"] <= len(title) <= tl["max"]:
        earned += parts["title"]
    if dl["min"] <= len(desc) <= dl["max"]:
        earned += parts["description"]
    has_og = bool(og.get("og:title")) and bool(og.get("og:image"))
    if has_og:
        earned += parts["open_graph"]

    return {"ratio": _pct(earned, sum(parts.values())),
            "how": (f"title length against {tl['min']}-{tl['max']}, description "
                    f"against {dl['min']}-{dl['max']}, og:title and og:image"),
            "detail": {"title_chars": len(title), "description_chars": len(desc),
                       "og_title": bool(og.get("og:title")),
                       "og_image": bool(og.get("og:image"))}}


def s_e3(ev: dict, cfg: dict) -> dict:
    """JSON-LD present, and typed at both the site and the page level."""
    parts = cfg["parts"]["E3"]
    nodes = schema_nodes(ev.get("parsed") or {})
    types = set()
    for node in nodes:
        types |= node_types(node)

    site = types & set(cfg["site_entity_types"])
    page = types & set(cfg["page_entity_types"])
    earned = 0
    if nodes:
        earned += parts["any_jsonld"]
    if site:
        earned += parts["site_entity"]
    if page:
        earned += parts["page_entity"]

    return {"ratio": _pct(earned, sum(parts.values())),
            "how": "JSON-LD nodes, matched against site-level and page-level types",
            "detail": {"nodes": len(nodes), "types": sorted(types),
                       "site_entity": sorted(site), "page_entity": sorted(page)}}


def s_e4(ev: dict, cfg: dict) -> dict:
    """robots.txt and a sitemap. Reuses the evidence signal A3 already gathered."""
    parts = cfg["parts"]["E4"]
    sitemap = ev.get("sitemap") or {}
    earned, robots = 0, ev.get("robots_txt")
    if robots is not None:
        earned += parts["robots_txt"]
    if sitemap.get("valid"):
        earned += parts["sitemap_valid"]
    if sitemap.get("source") == "robots.txt":
        earned += parts["declared"]

    return {"ratio": _pct(earned, sum(parts.values())),
            "how": "robots.txt fetch, sitemap reachability and how it was found",
            "detail": {"robots_txt": robots is not None,
                       "sitemap_valid": bool(sitemap.get("valid")),
                       "urls_in_sitemap": sitemap.get("loc_count", 0),
                       "found_by": sitemap.get("source")}}


def s_e5(ev: dict, cfg: dict) -> dict:
    """Content depth and quality. Thin pages score zero, deliberately."""
    text = (ev.get("render") or {}).get("extracted_text")
    words = (ev.get("parsed") or {}).get("word_count", 0)
    floor = cfg["min_words_for_quality"]

    if text is None:
        return {"ratio": 0.0, "how": "no extracted text available",
                "detail": {"word_count": words}, "unmeasured": True}
    if words < floor:
        return {"ratio": 0.0,
                "how": f"under {floor} words, too thin to assess or to quote",
                "detail": {"word_count": words, "floor": floor}}

    out = content_analyse(text)
    return {"ratio": out["overall_quality"] / 100,
            "how": "content_quality.overall_quality over filler, AI patterns, density and repetition",
            "detail": {"word_count": words, "overall_quality": out["overall_quality"],
                       "information_density": out["information_density"],
                       "flags": out["flags"]}}


def s_e6(ev: dict, cfg: dict) -> dict:
    """One h1, not a stub, with sections under it."""
    parts, parsed = cfg["parts"]["E6"], ev.get("parsed") or {}
    h1, h2 = parsed.get("h1") or [], parsed.get("h2") or []
    suspicious = parsed.get("h1_suspicious")  # only present when flagged

    earned = 0
    if len(h1) == 1:
        earned += parts["single_h1"]
    if h1 and not suspicious:
        earned += parts["h1_not_suspicious"]
    if len(h2) >= 2:
        earned += parts["two_h2"]

    return {"ratio": _pct(earned, sum(parts.values())),
            "how": "count of h1 and h2, and whether the h1 looks like a stub",
            "detail": {"h1": len(h1), "h2": len(h2),
                       "h1_suspicious": bool(suspicious),
                       "h1_text": h1[:1]}}


def s_e7(ev: dict, cfg: dict) -> dict:
    """Speed READINESS. Static hints in the HTML, not a measurement."""
    html = ev.get("rendered_html") or ""
    if not html:
        return {"ratio": 0.0, "how": "no HTML to inspect", "detail": {},
                "unmeasured": True}
    out = preload_analyse(html, _headers(ev))
    return {"ratio": out["score"] / 100,
            "how": ("presence of preload, prerender and speculation-rule hints. "
                    "This is readiness, not measured performance"),
            "detail": {"hint_score": out["score"],
                       "preload_hints": out["preload_hints"],
                       "prerender_links": out["prerender_links"],
                       "lcp_hints": out["lcp_resource_hints"]}}


def s_e8(ev: dict, cfg: dict) -> dict:
    """Share of images carrying alt text. A lazy-loading method counts as optimised."""
    images = (ev.get("parsed") or {}).get("images") or []
    if not images:
        return {"ratio": 1.0, "how": "no images on the page", "detail": {"images": 0}}
    withalt = [i for i in images if (i.get("alt") or "").strip()]
    lazy = [i for i in images if (i.get("lazy_method") or "none") != "none"]
    return {"ratio": _pct(len(withalt), len(images)),
            "how": "images with a non-empty alt attribute, over all images",
            "detail": {"images": len(images), "with_alt": len(withalt),
                       "lazy_loaded": len(lazy)}}


def s_e9(ev: dict, cfg: dict) -> dict:
    """Enough internal links, and none of them nofollowed."""
    parts = cfg["parts"]["E9"]
    internal = ((ev.get("parsed") or {}).get("links") or {}).get("internal") or []
    nofollow = [l for l in internal if "nofollow" in str(l.get("rel") or "").lower()]

    earned = 0
    if len(internal) >= cfg["min_internal_links"]:
        earned += parts["enough_links"]
    if not nofollow:
        earned += parts["no_internal_nofollow"]

    return {"ratio": _pct(earned, sum(parts.values())),
            "how": f"internal link count against {cfg['min_internal_links']}, and rel=nofollow on them",
            "detail": {"internal_links": len(internal),
                       "nofollowed": len(nofollow)}}


SIGNALS = [("E1", s_e1), ("E2", s_e2), ("E3", s_e3), ("E4", s_e4), ("E5", s_e5),
           ("E6", s_e6), ("E7", s_e7), ("E8", s_e8), ("E9", s_e9)]


# --------------------------------------------------------------------------
# the keyed tier
# --------------------------------------------------------------------------

def valid_api_key(key: Optional[str]) -> bool:
    """``fullmatch``, not ``search``.

    ``google_auth._GOOGLE_API_KEY_RE`` is unanchored because its real job is
    redacting keys out of log lines. Used as a validator it would accept
    ``AIzaSomething <paste your key here>``, so anchor it.
    """
    if not key:
        return False
    from google_auth import _GOOGLE_API_KEY_RE
    return bool(_GOOGLE_API_KEY_RE.fullmatch(key.strip()))


def field_score(api_key: Optional[str] = None) -> dict:
    """Never folded into the 100. Null with a reason beats a redistribution:
    a missing performance score is a known hole, not a reweighting."""
    if api_key is None:
        from google_auth import get_api_key
        api_key = get_api_key()
    if not valid_api_key(api_key):
        reason = ("no Google API key" if not api_key else
                  "the configured Google API key is not well formed")
        return {"seo_field": None, "reason": reason,
                "unlocks": ("one key would add Core Web Vitals here and the "
                            "30-point YouTube signal in the social citation "
                            "surface")}
    # Deliberately not implemented until a key exists to test it against.
    # Reporting null with a reason is honest; guessing at an untested code path
    # and shipping it to a client is not.
    return {"seo_field": None, "reason": "a key is configured but field data is not wired up yet",
            "unlocks": None}


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def _band(total: float, bands: dict) -> str:
    for letter in ("A", "B", "C", "D"):
        if total >= bands[letter]:
            return letter
    return "F"


def score(evidence: dict, cfg: Optional[dict] = None) -> dict:
    """Pure. Same evidence in, same result out, always."""
    cfg = cfg or load_config()
    scfg = cfg["seo"]
    weights, titles = scfg["weights"], scfg["titles"]
    assert sum(weights.values()) == 100, f"seo weights sum to {sum(weights.values())}"

    rows, unmeasured = [], []
    for sid, fn in SIGNALS:
        out = fn(evidence, scfg)
        ratio = max(0.0, min(1.0, float(out["ratio"])))
        rows.append({
            "id": sid,
            "title": titles[sid],
            "weight": weights[sid],
            "ratio": round(ratio, 4),
            "points": round(weights[sid] * ratio, 2),
            "how_measured": out["how"],
            "detail": out.get("detail", {}),
        })
        if out.get("unmeasured"):
            unmeasured.append(sid)

    total = round(sum(r["points"] for r in rows))
    field = field_score()
    return {
        "url": evidence.get("url"),
        "scoring_version": SCORING_VERSION,
        "seo": total,
        "band": _band(total, scfg["bands"]),
        "seo_field": field["seo_field"],
        "seo_field_reason": field["reason"],
        "signals": rows,
        "unmeasured": unmeasured,
        "scope": scfg["_scope"],
        "cannot_say": scfg["_cannot_say"],
        "performance_note": scfg["_performance_note"],
    }


def audit(url: str, rendered: Optional[dict] = None, **kwargs) -> dict:
    """``rendered`` is the evidence dict ``agent_ready.gather()`` produced.

    Passing it is the whole point: this module never fetches anything, so
    without it there is nothing to score. Gathering here would mean a second
    render of a page the audit has already rendered once.
    """
    if rendered is None:
        from bof.agent_ready import gather
        rendered = gather(url, **kwargs)
    if rendered.get("error"):
        return {"url": url, "seo": 0, "band": "F", "seo_field": None,
                "seo_field_reason": "the page was never fetched",
                "signals": [], "unmeasured": [], "scope": "not measured",
                "cannot_say": [], "performance_note": "",
                "error": rendered["error"], "scoring_version": SCORING_VERSION}
    return score(rendered)


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def _canned_evidence() -> dict:
    """A good page. Every signal has something real to read."""
    html = (
        '<!doctype html><html><head><title>Calibration hardware for lab benches</title>'
        '<meta name="description" content="Northwind Instruments builds calibration '
        'hardware for laboratory benches, shipped worldwide with a two year warranty '
        'and on-site setup.">'
        '<meta property="og:title" content="Calibration hardware">'
        '<meta property="og:image" content="https://demo.test/og.png">'
        '<link rel="canonical" href="https://demo.test/hardware">'
        '<link rel="preload" as="image" href="/hero.webp" fetchpriority="high">'
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@type":"Organization","name":"Northwind"}</script>'
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@type":"Article","headline":"Calibration hardware"}</script>'
        '</head><body><main><h1>Calibration hardware for lab benches</h1>'
        '<h2>What it measures</h2><h2>How it is calibrated</h2>'
        '<img src="/a.png" alt="A bench calibrator"><img src="/b.png" alt="Close up">'
        + "".join(f'<a href="/page{i}">Page {i}</a>' for i in range(6))
        + '</main></body></html>'
    )
    from parse_html import parse_html
    return {
        "url": "https://demo.test/hardware",
        "final_url": "https://demo.test/hardware",
        "rendered_html": html,
        "parsed": parse_html(html, base_url="https://demo.test/hardware"),
        "robots_txt": "User-agent: *\nAllow: /\nSitemap: https://demo.test/sitemap.xml\n",
        "sitemap": {"discovered": True, "valid": True, "loc_count": 14,
                    "source": "robots.txt", "url": "https://demo.test/sitemap.xml"},
        "render": {"status_code": 200, "redirect_chain": [],
                   "headers": {"Content-Type": "text/html"},
                   "extracted_text": ("Northwind Instruments builds calibration "
                                      "hardware for laboratory benches. " * 40)},
    }


def self_check() -> int:
    cfg = load_config()
    scfg = cfg["seo"]
    failures = []

    total = sum(scfg["weights"].values())
    if total != 100:
        failures.append(f"seo weights sum to {total}, not 100")
    else:
        print("  ok    seo weights sum to 100")

    if set(scfg["weights"]) != {sid for sid, _ in SIGNALS}:
        failures.append("signals.json seo weights and the SIGNALS registry disagree")
    else:
        print("  ok    every seo signal has a weight")

    for sid, parts in scfg["parts"].items():
        if sum(parts.values()) != scfg["weights"][sid]:
            failures.append(f"{sid} parts sum to {sum(parts.values())}, "
                            f"weight is {scfg['weights'][sid]}")
    if not any("parts sum" in f for f in failures):
        print("  ok    every part-scored signal's parts sum to its weight")

    if any(k in cfg["report"]["headline_weights"] for k in ("seo", "seo_field")):
        failures.append("SEO entered the headline weights: this is now an SEO tool")
    else:
        print("  ok    SEO is absent from the headline weights")

    # The RAM budget depends on this module never fetching anything: an audit
    # already launches Chromium four times sequentially and a fifth is what
    # breaks a machine with 1.8 GB free.
    # The module's own namespace, not its source text: a source scan matches the
    # names in this very check.
    reachers = sorted(n for n in ("requests", "safe_requests_get", "urllib",
                                  "render_page", "sync_playwright", "urlopen",
                                  "gather")
                      if n in globals())
    if reachers:
        failures.append(f"seo_core reaches the network: {reachers}. It must score "
                        f"the evidence agent_ready.gather() already collected")
    else:
        print("  ok    seo_core makes no network call, so it adds no page fetch")

    ev = _canned_evidence()
    first, again = score(ev, cfg), score(ev, cfg)
    if json.dumps(first, sort_keys=True) != json.dumps(again, sort_keys=True):
        failures.append("two scorings of the same evidence differ")
    else:
        print(f"  ok    a good page scores {first['seo']}/100 band "
              f"{first['band']}, deterministically")

    if first["seo"] < 85:
        failures.append(f"the canned good page scored only {first['seo']}; "
                        f"a page that satisfies every signal should band A")
        for row in first["signals"]:
            print(f"          {row['id']} {row['points']:>5}/{row['weight']:<3} {row['title']}")

    # Key validation: the suite regex is unanchored because it exists to redact.
    good = "AIza" + "SyD-ExampleKeyMaterial_1234567890abc"
    for key, want, why in [(None, False, "no key"), ("", False, "empty string"),
                           ("PASTE_YOUR_YOUTUBE_API_KEY_HERE", False, "the placeholder"),
                           (good, True, "a well-formed key"),
                           (good + " <paste yours here>", False,
                            "a key with trailing junk that search() would accept")]:
        if valid_api_key(key) != want:
            failures.append(f"key validation got {why} wrong")
    if not any("key validation" in f for f in failures):
        print("  ok    key validation anchors the regex, placeholder and junk rejected")

    field = field_score(None)
    if field["seo_field"] is not None:
        failures.append("seo_field produced a number with no key")
    elif not field["reason"]:
        failures.append("seo_field is null without saying why")
    else:
        print(f"  ok    seo_field is null with a reason ({field['reason']})")

    thin = dict(ev, parsed=dict(ev["parsed"], word_count=120))
    thin_row = next(r for r in score(thin, cfg)["signals"] if r["id"] == "E5")
    if thin_row["ratio"] != 0.0:
        failures.append(f"a {thin['parsed']['word_count']}-word page scored "
                        f"{thin_row['ratio']} on content quality, expected 0")
    else:
        print("  ok    a thin page scores zero on content quality")

    blank = {"url": "https://demo.test/", "parsed": {}, "rendered_html": "",
             "render": {}}
    out = score(blank, cfg)
    if not out["unmeasured"]:
        failures.append("evidence with no render block reported nothing as unmeasured")
    else:
        print(f"  ok    a missing render block reports {', '.join(out['unmeasured'])} "
              f"as unmeasured rather than as failures")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll SEO baseline self-checks passed.")
    return 0


# --------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", nargs="?", help="page to score")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)

    if args.self_check:
        return self_check()
    if not args.url:
        ap.error("a url is required unless --self-check is given")

    result = audit(args.url)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if not result.get("error") else 1

    if result.get("error"):
        print(f"error: {result['error']}")
        return 1
    print(f"SEO Technical Baseline {result['seo']}/100 band {result['band']}")
    for row in result["signals"]:
        print(f"  {row['id']} {row['points']:>5}/{row['weight']:<3} {row['title']}")
    print(f"\n  field data: {result['seo_field_reason']}")
    print(f"  scope: {result['scope']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
