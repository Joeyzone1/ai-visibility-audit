# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Agent readiness: can an AI agent actually read and operate this page.

    python -m bof.agent_ready https://example.com [--json] [--stable]
    python -m bof.agent_ready --self-check

Twelve signals summing to 100, defined in ``signals.json``. Every signal is a
bounded ratio or a boolean, never a raw count, so page size cannot move the
score. That is the whole reason the suite's ``agent_ux_check.score()`` is not
reused: it deducts per occurrence, so a bigger page scores worse for the same
quality of markup, which is not a number you can put in front of a client.

The same twelve signals are also reported as two lenses, ``lens_scores()``: AI
engine readability (can an assistant read, quote and cite this page) and AI
agent operability (can an agent acting for a user actually work it). They are
different questions with different buyers, so one combined number hides the
second one entirely. A2 belongs to both, because being blocked at the edge
defeats reading and doing alike.

Gathering and scoring are separate. ``gather()`` touches the network,
``score()`` is pure. The self-check feeds canned evidence to ``score()``, so it
runs offline and gives the same answer every time.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bof import SCORING_VERSION
from bof import suite  # noqa: F401  # must precede every suite import

from agent_ux_check import analyze_html  # noqa: E402
from parse_html import parse_html  # noqa: E402
from render_page import render_page  # noqa: E402
from url_safety import URLSafetyError, safe_requests_get, validate_url_strict  # noqa: E402

CONFIG_PATH = pathlib.Path(__file__).with_name("signals.json")
FIXTURES = pathlib.Path(__file__).with_name("fixtures")

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_SECTION_SPLIT_RE = re.compile(r"<h[23]\b", re.I)
_CONTENT_START_RE = re.compile(r"<(?:main|article)\b|<h1\b", re.I)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_TEXTAREA_RE = re.compile(r"<textarea\b[^>]*>", re.I)
_SELECT_RE = re.compile(r"<select\b[^>]*>", re.I)
_TYPE_RE = re.compile(r'\btype\s*=\s*["\']?([a-z]+)', re.I)
_ID_RE = re.compile(r'\bid\s*=\s*["\']([^"\']+)["\']', re.I)
_ARIA_LABEL_RE = re.compile(r"\baria-label(?:ledby)?\s*=", re.I)
_LABEL_FOR_RE = re.compile(r'<label\b[^>]*\bfor\s*=\s*["\']([^"\']+)["\']', re.I)
_SITEMAP_DIRECTIVE_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.I | re.M)
_LOC_RE = re.compile(r"<loc\b[^>]*>", re.I)
_WIKIDATA_QID_RE = re.compile(r"wikidata\.org/(?:entity|wiki)/(Q\d+)", re.I)
#: aria_snapshot lines look like `- button "Save"` or `- button` when unnamed.
_ARIA_LINE_RE = re.compile(r'^\s*-\s+([a-z]+)(?:\s+"((?:[^"\\]|\\.)*)")?')

#: Inputs that are never user-labelled and must not count against D3.
_UNLABELLABLE_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}


def load_config(path: pathlib.Path = CONFIG_PATH) -> dict:
    """signals.json, with any local branding merged over the top.

    ``bof/branding.local.json`` is gitignored and holds only the keys you want
    to change, usually just ``author``. It exists so the byline on a client PDF
    can be yours without your company name living in a public repository.
    """
    cfg = json.loads(path.read_text(encoding="utf-8"))
    local = path.with_name("branding.local.json")
    if local.is_file():
        cfg["report"]["branding"].update(
            json.loads(local.read_text(encoding="utf-8")))
    return cfg


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def visible_words(html: Optional[str]) -> int:
    """Word count of the text a reader sees, scripts and markup removed."""
    if not html:
        return 0
    return len(_TAG_RE.sub(" ", _SCRIPT_STYLE_RE.sub(" ", html)).split())


def aria_from_html(html: str, *, timeout_ms: int = 20000) -> Optional[str]:
    """Accessibility view of already-rendered HTML, as an ARIA snapshot string.

    Playwright 1.61 removed ``page.accessibility``, and ``render_page`` swallows
    the resulting AttributeError, so its ``accessibility_tree`` is always None.
    ``locator.aria_snapshot()`` is the supported replacement.

    Uses ``set_content`` on HTML that is already post-JS rather than navigating
    again, and aborts every subresource request. So this is a second browser
    launch but not a second page load: no network, roughly a second.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route, request: route.abort())
                page.set_content(html, wait_until="domcontentloaded",
                                 timeout=timeout_ms)
                return page.locator("body").aria_snapshot()
            finally:
                browser.close()
    except Exception:
        # Never block an audit on the a11y view; D2 reports it as unavailable.
        return None


def parse_aria(snapshot: Optional[str], interactive_roles: set) -> dict:
    """Count interactive nodes and how many carry an accessible name."""
    found = {"present": snapshot is not None, "interactive": 0, "named": 0,
             "unnamed_roles": []}
    if not snapshot:
        return found
    for line in snapshot.splitlines():
        m = _ARIA_LINE_RE.match(line)
        if not m:
            continue
        role, name = m.group(1), m.group(2)
        if role not in interactive_roles:
            continue
        found["interactive"] += 1
        if name and name.strip():
            found["named"] += 1
        elif len(found["unnamed_roles"]) < 10:
            found["unnamed_roles"].append(role)
    return found


def schema_nodes(parsed: dict) -> list:
    """Flat list of JSON-LD nodes from parse_html output."""
    nodes = []
    for block in parsed.get("schema") or []:
        if isinstance(block, dict):
            nodes.append(block)
        elif isinstance(block, list):
            nodes.extend(n for n in block if isinstance(n, dict))
    return nodes


def node_types(node: dict) -> set:
    raw = node.get("@type") or node.get("type") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(t).split("/")[-1] for t in raw if t}


def same_as_urls(nodes: list, entity_types: set) -> list:
    urls = []
    for node in nodes:
        if entity_types and not (node_types(node) & entity_types):
            continue
        raw = node.get("sameAs") or []
        if isinstance(raw, str):
            raw = [raw]
        urls.extend(str(u) for u in raw if u)
    return urls


def _band(total: float, bands: dict) -> str:
    for letter in ("A", "B", "C", "D"):
        if total >= bands[letter]:
            return letter
    return "F"


def lens_scores(rows: list, cfg: dict) -> dict:
    """Split scored rows into the two lenses, each normalised to 100.

    Pure, and tolerant of a short or empty ``rows`` list: a signal with no row
    scores zero rather than raising, which is what lets the gather-failure stub
    and the canned report fixtures go through the same code path as a real run.

    A signal with nothing to measure drops out of the denominator and the
    remaining weights renormalise pro rata, the same way ``social_surface``
    handles an unmeasurable platform. Absent is not the same as correct: a page
    with no controls, no accessible names and no form fields used to take full
    marks on all three and score 84 for operability, which is a number about the
    page's emptiness rather than about whether an agent could work it.

    When too little of a lens survives to carry a number at all, its score is
    ``None`` and ``applicable`` is False. Reporting a lens out of one signal
    would be worse than admitting the question does not apply here.
    """
    weights = cfg["weights"]
    by_id = {r["id"]: r for r in rows}
    floor = cfg["lenses"]["_min_evidence_for_headline"]
    out = {}
    for lens_id, lens in cfg["lenses"].items():
        if lens_id.startswith("_"):  # explanatory keys, not lenses
            continue
        ids = lens["signals"]
        dropped = sorted(sid for sid in ids
                         if (by_id.get(sid) or {}).get("not_applicable")
                         or (by_id.get(sid) or {}).get("unavailable"))
        scored = [sid for sid in ids if sid not in dropped]
        full = sum(weights[sid] for sid in ids)
        possible = sum(weights[sid] for sid in scored)
        retained = possible / full

        row = {"title": lens["title"], "owner": lens["owner"],
               "signals": list(ids), "scored_signals": scored,
               "dropped_signals": dropped, "possible": possible,
               "retained": round(retained, 2)}
        if retained < floor:
            # Includes possible == 0, where the lens measured nothing at all.
            out[lens_id] = {**row, "score": None, "band": None,
                            "applicable": False}
            continue
        earned = sum((by_id.get(sid) or {}).get("points", 0.0) for sid in scored)
        total = round(100 * earned / possible)
        out[lens_id] = {**row, "score": total,
                        "band": _band(total, cfg["bands"]), "applicable": True}
    return out


def _taper(value: float, full_at: float, zero_at: float) -> float:
    """1.0 at or below full_at, 0.0 at or above zero_at, linear between."""
    if value <= full_at:
        return 1.0
    if value >= zero_at:
        return 0.0
    return (zero_at - value) / (zero_at - full_at)


# --------------------------------------------------------------------------
# signals: each takes (evidence, config) and returns ratio 0-1 plus its working
# --------------------------------------------------------------------------

def s_a1(ev: dict, cfg: dict) -> dict:
    crawlers = cfg["citation_crawlers"]
    robots = ev.get("robots_txt")
    if not robots:
        return {"ratio": 1.0, "how": "no robots.txt, so nothing is disallowed",
                "detail": {"blocked": [], "allowed": crawlers}}
    parser = RobotFileParser()
    parser.parse(robots.splitlines())
    root = ev.get("final_url") or ev["url"]
    # ponytail: stdlib robotparser; swap for a wildcard-aware parser if a client
    # site turns out to rely on Allow-precedence edge cases it gets wrong.
    blocked = [ua for ua in crawlers if not parser.can_fetch(ua, root)]
    blocked_training = [
        ua for ua in cfg["training_crawlers"] if not parser.can_fetch(ua, root)
    ]
    return {
        "ratio": (len(crawlers) - len(blocked)) / len(crawlers),
        "how": f"robots.txt parsed, can_fetch tested for {len(crawlers)} citation crawlers",
        "detail": {"blocked": blocked,
                   "allowed": [c for c in crawlers if c not in blocked],
                   "training_crawlers_blocked": blocked_training,
                   "training_block_is_a_business_choice": True},
    }


def s_a2(ev: dict, cfg: dict) -> dict:
    plain, browser = ev.get("plain") or {}, ev.get("browser") or {}
    p_status, b_status = plain.get("status"), browser.get("status")
    checks = {
        "plain_ua_returns_200": p_status == 200,
        "plain_ua_body_has_text": plain.get("words", 0) >= 50,
        "matches_browser_ua": p_status is not None and p_status == b_status,
    }
    return {
        "ratio": sum(checks.values()) / len(checks),
        "how": "fetched / with a plain UA and a browser UA, compared status and text",
        "detail": {**checks, "plain_status": p_status, "browser_status": b_status,
                   "plain_words": plain.get("words", 0)},
    }


def s_a3(ev: dict, cfg: dict) -> dict:
    sm = ev.get("sitemap") or {}
    ratio = 0.5 * bool(sm.get("discovered")) + 0.5 * bool(sm.get("valid"))
    return {"ratio": ratio,
            "how": "looked for a Sitemap: directive then /sitemap.xml, parsed for <loc>",
            "detail": {k: sm.get(k) for k in ("discovered", "valid", "loc_count", "source", "url")}}


def s_b1(ev: dict, cfg: dict) -> dict:
    raw, rendered = visible_words(ev.get("raw_html")), visible_words(ev.get("rendered_html"))
    if rendered <= 0:
        ratio = 0.0
    else:
        ratio = min(1.0, raw / rendered)
    return {"ratio": ratio,
            "how": "visible words in the pre-JavaScript fetch over the rendered page",
            "detail": {"raw_words": raw, "rendered_words": rendered}}


def s_b2(ev: dict, cfg: dict) -> dict:
    html = ev.get("rendered_html") or ""
    cs = cfg["content_start"]
    if not html:
        return {"ratio": 0.0, "how": "no HTML to measure", "detail": {}}
    m = _CONTENT_START_RE.search(html)
    frac = (m.start() / len(html)) if m else 1.0
    return {"ratio": _taper(frac, cs["full_credit_at"], cs["zero_credit_at"]),
            "how": "byte offset of the first <main>, <article> or <h1> over document length",
            "detail": {"marker": m.group(0) if m else None, "offset_fraction": round(frac, 4)}}


def s_b3(ev: dict, cfg: dict) -> dict:
    p = cfg["passage"]
    html = _SCRIPT_STYLE_RE.sub(" ", ev.get("rendered_html") or "")
    sections = [len(_TAG_RE.sub(" ", part).split())
                for part in _SECTION_SPLIT_RE.split(html)[1:]]
    if not sections:
        return {"ratio": 0.0, "how": "no H2 or H3 sections to size",
                "detail": {"sections": 0}}
    credit = 0.0
    ideal = 0
    for words in sections:
        if p["ideal_min"] <= words <= p["ideal_max"]:
            credit += 1.0
            ideal += 1
        elif p["partial_min"] <= words <= p["partial_max"]:
            credit += 0.5
    return {"ratio": credit / len(sections),
            "how": f"share of H2/H3 sections sized {p['ideal_min']}-{p['ideal_max']} words, half credit {p['partial_min']}-{p['partial_max']}",
            "detail": {"sections": len(sections), "ideal": ideal,
                       "word_counts": sections[:20]}}


def s_c1(ev: dict, cfg: dict) -> dict:
    nodes = schema_nodes(ev.get("parsed") or {})
    if not nodes:
        return {"ratio": 0.0, "how": "no JSON-LD blocks found", "detail": {"blocks": 0}}
    known = set(cfg["schema_types"])
    types = set()
    for node in nodes:
        types |= node_types(node)
    recognised = types & known
    return {"ratio": 1.0 if recognised else 0.5,
            "how": "JSON-LD @type values checked against known schema.org types",
            "detail": {"blocks": len(nodes), "types": sorted(types),
                       "recognised": sorted(recognised)}}


def s_c2(ev: dict, cfg: dict) -> dict:
    nodes = schema_nodes(ev.get("parsed") or {})
    urls = same_as_urls(nodes, set(cfg["entity_types"]))
    ratio = 0.0 if not urls else (1.0 if len(urls) >= 2 else 0.5)
    return {"ratio": ratio,
            "how": "sameAs on an Organization, LocalBusiness or Person node",
            "detail": {"same_as_count": len(urls), "same_as": urls[:10]}}


def s_c3(ev: dict, cfg: dict) -> dict:
    wd = ev.get("wikidata") or {}
    declared, found = wd.get("declared_qid"), wd.get("found_qid")
    if declared:
        ratio, how = 1.0, "site declares its Wikidata QID in sameAs"
    elif found:
        ratio, how = 0.5, "a Wikidata item matches this domain but the site does not link it"
    else:
        ratio, how = 0.0, "no Wikidata item found for this brand or domain"
    if not wd.get("looked_up") and not declared:
        how += " (lookup unavailable)"
    return {"ratio": ratio, "how": how,
            "detail": {"declared_qid": declared, "found_qid": found,
                       "looked_up": bool(wd.get("looked_up"))}}


def s_d1(ev: dict, cfg: dict) -> dict:
    f = analyze_html(ev.get("rendered_html") or "")
    real = f["real_buttons"] + f["real_anchors"]
    fake = f["div_onclick_widgets"]
    if real + fake == 0:
        return {"ratio": 0.0, "how": "no interactive controls on the page",
                "detail": {"real": 0, "div_onclick": 0}, "not_applicable": True}
    return {"ratio": real / (real + fake),
            "how": "real <button>/<a href> over real plus <div onclick>",
            "detail": {"real": real, "div_onclick": fake,
                       "semantic_landmarks": f["semantic_landmarks"]}}


def s_d2(ev: dict, cfg: dict) -> dict:
    found = parse_aria(ev.get("aria_snapshot"), set(cfg["interactive_roles"]))
    if not found["present"]:
        return {"ratio": 0.0, "how": "accessibility snapshot unavailable",
                "detail": found, "unavailable": True}
    if found["interactive"] == 0:
        return {"ratio": 0.0, "how": "no interactive nodes in the accessibility tree",
                "detail": found, "not_applicable": True}
    return {"ratio": found["named"] / found["interactive"],
            "how": "interactive nodes carrying an accessible name, over all interactive nodes",
            "detail": found}


def s_d3(ev: dict, cfg: dict) -> dict:
    html = ev.get("rendered_html") or ""
    label_targets = set(_LABEL_FOR_RE.findall(html))
    fields, labelled = 0, 0
    for tag in (_INPUT_RE.findall(html) + _TEXTAREA_RE.findall(html)
                + _SELECT_RE.findall(html)):
        kind = _TYPE_RE.search(tag)
        if kind and kind.group(1).lower() in _UNLABELLABLE_INPUT_TYPES:
            continue
        fields += 1
        ident = _ID_RE.search(tag)
        if _ARIA_LABEL_RE.search(tag) or (ident and ident.group(1) in label_targets):
            labelled += 1
    if fields == 0:
        return {"ratio": 0.0, "how": "no form fields on the page",
                "detail": {"fields": 0}, "not_applicable": True}
    return {"ratio": labelled / fields,
            "how": "fields with aria-label, aria-labelledby or a matching <label for>",
            "detail": {"fields": fields, "labelled": labelled}}


SIGNALS = [("A1", s_a1), ("A2", s_a2), ("A3", s_a3),
           ("B1", s_b1), ("B2", s_b2), ("B3", s_b3),
           ("C1", s_c1), ("C2", s_c2), ("C3", s_c3),
           ("D1", s_d1), ("D2", s_d2), ("D3", s_d3)]


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score(evidence: dict, cfg: Optional[dict] = None) -> dict:
    """Pure. Same evidence in, same result out, always."""
    cfg = cfg or load_config()
    weights, titles = cfg["weights"], cfg["signal_titles"]
    assert sum(weights.values()) == 100, f"weights sum to {sum(weights.values())}, not 100"

    rows, unavailable = [], []
    for sid, fn in SIGNALS:
        out = fn(evidence, cfg)
        ratio = max(0.0, min(1.0, float(out["ratio"])))
        rows.append({
            "id": sid,
            "title": titles[sid],
            "weight": weights[sid],
            "ratio": round(ratio, 4),
            "points": round(weights[sid] * ratio, 2),
            "how_measured": out["how"],
            "detail": out.get("detail", {}),
            # Nothing of this kind on the page, so there is nothing to judge.
            # Drops out of its lens's denominator rather than scoring full marks
            # for absence or zero for a fault the page does not have.
            "not_applicable": bool(out.get("not_applicable")),
            "unavailable": bool(out.get("unavailable")),
        })
        if out.get("unavailable"):
            unavailable.append(sid)

    total = round(sum(r["points"] for r in rows))
    return {
        "url": evidence.get("url"),
        "scoring_version": SCORING_VERSION,
        "signals_last_reviewed": cfg["last_reviewed"],
        "score": total,
        "band": _band(total, cfg["bands"]),
        "lenses": lens_scores(rows, cfg),
        "signals": rows,
        "unavailable": unavailable,
        "error": evidence.get("error"),
    }


# --------------------------------------------------------------------------
# gathering
# --------------------------------------------------------------------------

def _get(url: str, ua: str, timeout: int) -> dict:
    try:
        resp = safe_requests_get(url, timeout=timeout, headers={"User-Agent": ua})
        return {"status": resp.status_code, "text": resp.text,
                "words": visible_words(resp.text), "error": None}
    except Exception as exc:  # noqa: BLE001 - a blocked fetch is itself the finding
        return {"status": None, "text": "", "words": 0,
                "error": f"{type(exc).__name__}: {exc}"}


def _sitemap(root: str, robots: Optional[str], timeout: int) -> dict:
    candidates = []
    if robots:
        candidates += [m.strip() for m in _SITEMAP_DIRECTIVE_RE.findall(robots)]
    source = "robots.txt" if candidates else "convention"
    candidates.append(urljoin(root, "/sitemap.xml"))
    for url in candidates:
        got = _get(url, "bof-audit/0.1", timeout)
        if got["status"] == 200:
            locs = len(_LOC_RE.findall(got["text"]))
            return {"discovered": True, "valid": locs > 0, "loc_count": locs,
                    "source": source, "url": url}
    return {"discovered": False, "valid": False, "loc_count": 0,
            "source": None, "url": None}


def _wikidata(brand: Optional[str], domain: str, declared: Optional[str],
              timeout: int) -> dict:
    out = {"declared_qid": declared, "found_qid": None, "looked_up": False}
    if declared or not brand:
        return out
    api = "https://www.wikidata.org/w/api.php"
    try:
        search = safe_requests_get(
            f"{api}?action=wbsearchentities&format=json&language=en&limit=5"
            f"&search={brand}", timeout=timeout,
            headers={"User-Agent": "bof-audit/0.1"})
        ids = [hit["id"] for hit in search.json().get("search", [])]
        if not ids:
            out["looked_up"] = True
            return out
        entities = safe_requests_get(
            f"{api}?action=wbgetentities&format=json&props=claims"
            f"&ids={'|'.join(ids)}", timeout=timeout,
            headers={"User-Agent": "bof-audit/0.1"})
        for qid, body in entities.json().get("entities", {}).items():
            for claim in body.get("claims", {}).get("P856", []):
                site = claim.get("mainsnak", {}).get("datavalue", {}).get("value", "")
                if domain and domain in str(site):
                    out["found_qid"] = qid
                    break
            if out["found_qid"]:
                break
        out["looked_up"] = True
    except Exception:  # noqa: BLE001 - C3 reports the lookup as unavailable
        pass
    return out


def gather(url: str, *, timeout: int = 20, timeout_ms: int = 30000,
           network: bool = True) -> dict:
    """Everything the score needs. This is the only part that touches the net."""
    cfg = load_config()
    uas = cfg["user_agents"]
    try:
        normalised, _ip = validate_url_strict(url)
    except (URLSafetyError, ValueError) as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}

    parsed_url = urlparse(normalised)
    root = f"{parsed_url.scheme}://{parsed_url.netloc}/"

    # extract_content=True runs trafilatura over HTML already in memory. No
    # signal here reads extracted_text; bof.seo_core does, and the alternative
    # is a second fetch, which is the one thing this machine cannot afford.
    rendered = render_page(normalised, mode="always", timeout_ms=timeout_ms,
                           block_resources=["image", "font", "media"],
                           extract_content=True, user_agent=uas["browser"])
    rendered_html = rendered.get("content") or rendered.get("raw_content") or ""

    ev = {
        "url": normalised,
        "final_url": normalised,
        "rendered_html": rendered_html,
        "raw_html": rendered.get("raw_content") or "",
        "parsed": parse_html(rendered_html, base_url=normalised),
        "aria_snapshot": aria_from_html(rendered_html) if rendered_html else None,
        "render_error": rendered.get("error"),
        # What the render already told us and this module happens not to need.
        # Kept so the SEO baseline can be scored without touching the network.
        "render": {k: rendered.get(k) for k in
                   ("status_code", "redirect_chain", "headers", "extracted_text")},
        "error": None,
    }

    if not network:
        ev.update(plain={}, browser={}, robots_txt=None,
                  sitemap={}, wikidata={})
        return ev

    browser_hit = _get(normalised, uas["browser"], timeout)
    plain_hit = _get(normalised, uas["plain"], timeout)
    robots_hit = _get(urljoin(root, "/robots.txt"), uas["plain"], timeout)
    robots = robots_hit["text"] if robots_hit["status"] == 200 else None

    nodes = schema_nodes(ev["parsed"])
    all_same_as = same_as_urls(nodes, set())
    declared = next((m.group(1) for u in all_same_as
                     if (m := _WIKIDATA_QID_RE.search(u))), None)
    brand = next((n.get("name") for n in nodes
                  if node_types(n) & set(cfg["entity_types"]) and n.get("name")),
                 None) or ev["parsed"].get("title")

    ev.update(
        browser={"status": browser_hit["status"]},
        plain={"status": plain_hit["status"], "words": plain_hit["words"]},
        robots_txt=robots,
        sitemap=_sitemap(root, robots, timeout),
        wikidata=_wikidata(brand, parsed_url.netloc, declared, timeout),
    )
    return ev


def audit(url: str, *, stable: bool = False, **kwargs) -> dict:
    """Score one URL. ``stable`` re-renders and takes the median of the render
    dependent signals, which is the defence against a client seeing a score
    move on a rerun that changed nothing."""
    ev = gather(url, **kwargs)
    if ev.get("error"):
        return {"url": url, "score": 0, "band": "F", "signals": [],
                "unavailable": [], "error": ev["error"],
                "scoring_version": SCORING_VERSION}
    result = score(ev)
    if not stable:
        return result

    totals = [result["score"]]
    for _ in range(2):
        totals.append(score(gather(url, **kwargs))["score"])
    totals.sort()
    result["score"] = totals[1]
    result["band"] = _band(totals[1], load_config()["bands"])
    result["stability"] = {"samples": totals, "spread": totals[-1] - totals[0],
                           "unstable": (totals[-1] - totals[0]) > 4}
    return result


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def self_check() -> int:
    cfg = load_config()
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    failures = []

    weight_total = sum(cfg["weights"].values())
    if weight_total != 100:
        failures.append(f"weights sum to {weight_total}, not 100")
    else:
        print("  ok    weights sum to 100")

    if set(cfg["weights"]) != {sid for sid, _ in SIGNALS}:
        failures.append("signals.json weights and the SIGNALS registry disagree")
    else:
        print("  ok    every signal has a weight")

    lenses = {k: v for k, v in cfg["lenses"].items() if not k.startswith("_")}
    members = [sid for lens in lenses.values() for sid in lens["signals"]]
    if set(members) != set(cfg["weights"]):
        orphans = set(cfg["weights"]) ^ set(members)
        failures.append(f"lenses do not cover exactly the twelve signals: {sorted(orphans)}")
    else:
        print("  ok    every signal belongs to a lens")

    shared = [lid for lid, lens in lenses.items() if "A2" in lens["signals"]]
    if len(shared) != 2:
        failures.append(f"A2 must count in both lenses, found in {shared}")
    else:
        print("  ok    A2 counts in both lenses")

    # Full marks in, 100 out. Each lens is normalised over its own signals, so a
    # lens that quietly reused the flat 100-point denominator would fail here.
    perfect = [{"id": sid, "points": float(w)} for sid, w in cfg["weights"].items()]
    full = lens_scores(perfect, cfg)
    wrong = {lid: l["score"] for lid, l in full.items() if l["score"] != 100}
    if wrong:
        failures.append(f"lenses do not normalise to 100 at full marks: {wrong}")
    else:
        sizes = ", ".join(f"{lid} out of {l['possible']}" for lid, l in full.items())
        print(f"  ok    each lens normalises to 100 at full marks ({sizes})")

    empty = lens_scores([], cfg)
    if any(l["score"] != 0 or l["band"] != "F" for l in empty.values()):
        failures.append(f"empty rows did not score 0/F: {empty}")
    else:
        print("  ok    no signals scores 0 and band F, without raising")

    # Dropping a signal must renormalise the survivors, not shrink the score.
    # D3 out of operability leaves A2, D1 and D2 covering 28 points; all three
    # at full marks must still read 100, or absence is being punished.
    partial = [{"id": sid, "points": float(w), "not_applicable": sid == "D3"}
               for sid, w in cfg["weights"].items()]
    renorm = lens_scores(partial, cfg)["agent_operability"]
    if renorm["score"] != 100:
        failures.append(f"after D3 dropped out the lens scored {renorm['score']}, "
                        f"not 100: the survivors did not renormalise")
    elif renorm["possible"] != 28:
        failures.append(f"denominator after dropping D3 is {renorm['possible']}, expected 28")
    else:
        print("  ok    a dropped signal renormalises the rest to 100 (33 -> 28)")

    # Every recommendation must say what it costs, or the action plan cannot be
    # ordered by anything but points, which promotes the unachievable.
    effort, allowed = cfg["effort"], set(cfg["effort"]["_ranks"])
    ids = (set(cfg["recommendations"]) | set(cfg["seo"]["recommendations"])
           | set(cfg["social_surface"]["weights"]))
    missing = sorted(i for i in ids if effort.get(i) not in allowed)
    if missing:
        failures.append(f"recommendations with no valid effort value: {missing}")
    else:
        print(f"  ok    all {len(ids)} recommendations carry an effort from "
              f"{sorted(allowed)}")

    for name, spec in sorted(expected.items()):
        if name.startswith("_"):  # explanatory keys, not fixtures
            continue
        html = (FIXTURES / f"{name}.html").read_text(encoding="utf-8")
        ev = dict(spec["evidence"])
        ev["url"] = f"https://{name}.test/"
        ev["rendered_html"] = html
        ev["raw_html"] = html if spec["raw_is_rendered"] else spec["raw_html"]
        ev["parsed"] = parse_html(html, base_url=ev["url"])

        first = score(ev, cfg)
        again = score(ev, cfg)
        if json.dumps(first, sort_keys=True) != json.dumps(again, sort_keys=True):
            failures.append(f"{name}: two scorings of the same evidence differ")
            continue

        got, want = first["band"], spec["expected_band"]
        line = f"{name}: {first['score']}/100 band {got}"
        if got != want:
            failures.append(f"{line}, expected band {want}")
            print(f"  FAIL  {line}, expected {want}")
            for row in first["signals"]:
                print(f"          {row['id']} {row['points']:>5}/{row['weight']:<3} {row['title']}")
        else:
            print(f"  ok    {line}, deterministic")

        got_na = sorted(lid for lid, l in first["lenses"].items()
                        if not l["applicable"])
        want_na = spec.get("expected_not_applicable", [])
        if got_na != want_na:
            failures.append(f"{name}: lenses reporting not-applicable {got_na}, "
                            f"expected {want_na}")
        elif want_na:
            for lid in want_na:
                lens = first["lenses"][lid]
                if lens["score"] is not None:
                    failures.append(f"{name}: {lid} does not apply but still "
                                    f"reported {lens['score']}")
            print(f"  ok    {name}: {', '.join(want_na)} reports no score, "
                  f"dropped {', '.join(first['lenses'][want_na[0]]['dropped_signals'])}")

        if "expected_headline" in spec:
            from bof.report import build_envelope
            env = build_envelope(first, {"future_readiness": 0, "probes": []},
                                 None, domain=f"{name}.test", cfg=cfg)
            got_h, want_h = env["summary"]["health_score"], spec["expected_headline"]
            if got_h != want_h:
                failures.append(f"{name}: headline {got_h}, expected {want_h}")
            else:
                print(f"  ok    {name}: headline {got_h}, which is the promise "
                      f"that a site AI cannot use scores zero")

        got_lens = {lid: l["band"] for lid, l in first["lenses"].items()}
        want_lens = spec["expected_lens_bands"]
        detail = ", ".join(
            f"{lid} not applicable" if b is None else
            f"{lid} {first['lenses'][lid]['score']}/100 band {b}"
            for lid, b in sorted(got_lens.items()))
        if got_lens != want_lens:
            failures.append(f"{name}: lens bands {got_lens}, expected {want_lens}")
            print(f"  FAIL  {name}: {detail}, expected {want_lens}")
            for row in first["signals"]:
                print(f"          {row['id']} {row['points']:>5}/{row['weight']:<3} {row['title']}")
        else:
            print(f"  ok    {name}: {detail}")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll agent-readiness self-checks passed.")
    return 0


# --------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", nargs="?", help="page to audit")
    ap.add_argument("--json", action="store_true", help="full result as JSON")
    ap.add_argument("--stable", action="store_true",
                    help="render three times, take the median score")
    ap.add_argument("--no-network", action="store_true",
                    help="render only, skip robots/sitemap/Wikidata")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)

    if args.self_check:
        return self_check()
    if not args.url:
        ap.error("a url is required unless --self-check is given")

    result = audit(args.url, stable=args.stable, timeout=args.timeout,
                   network=not args.no_network)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if not result.get("error") else 1

    if result.get("error"):
        print(f"error: {result['error']}")
        return 1
    print(f"{result['url']}")
    print(f"agent readiness {result['score']}/100  band {result['band']}"
          f"  (model v{result['scoring_version']}, reviewed {result['signals_last_reviewed']})")
    for row in result["signals"]:
        print(f"  {row['id']}  {row['points']:>5.1f}/{row['weight']:<3} {row['title']}")
        print(f"        {row['how_measured']}")
    if result["unavailable"]:
        print(f"  unavailable: {', '.join(result['unavailable'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
