# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Social citation surface: the signals that actually predict AI citation.

    python -m bof.social_surface "Brand Name" --domain example.com --live
    python -m bof.social_surface --self-check

Ahrefs measured 75,000 brands in December 2025: YouTube mentions correlate with
AI citation at roughly r=0.737 while Domain Rating manages about r=0.266. Every
backlink-centric SEO tool is optimising the weaker signal. Domain Rating does
not appear in this file at all, and that absence is the point.

Weights are correlation times **actionability**. Reddit correlates highly but a
brand cannot control it, so every recommendation here is earn, never post.
Advising a client to astroturf Reddit would be malpractice and is out of scope.

Anything genuinely unmeasurable is reported as unmeasured and its weight is
redistributed, rather than being scored zero and read as a failure. LinkedIn is
the honest case: no third party can measure share of voice there.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from bof import SCORING_VERSION
from bof import suite  # noqa: F401  # must precede every suite import
from bof.agent_ready import load_config

from url_safety import safe_requests_get  # noqa: E402

UA = {"User-Agent": "bof-audit/0.1 (+social-surface check)"}
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

MEASURED, UNMEASURED, NOT_APPLICABLE = "measured", "unmeasured", "not_applicable"

#: Description chapter markers look like `0:00 Intro` or `01:23:45 Wrap up`.
_CHAPTER_RE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+\S", re.M)
_LINKEDIN_RE = re.compile(r"linkedin\.com/(?:company|school)/([A-Za-z0-9._-]+)", re.I)
_YOUTUBE_RE = re.compile(r"youtube\.com/(?:@|channel/|c/|user/)([A-Za-z0-9._-]+)", re.I)


def _get_json(url: str, timeout: int = 20) -> dict:
    """GET returning parsed JSON, or {} on any failure. Never raises."""
    try:
        resp = safe_requests_get(url, timeout=timeout, headers=UA)
        if resp.status_code != 200:
            return {"_status": resp.status_code}
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"{type(exc).__name__}: {exc}"}


def _iso_days_ago(stamp: Optional[str]) -> Optional[int]:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - when).days


# --------------------------------------------------------------------------
# scorers. Pure: (evidence, config) -> {state, ratio, how, detail, actions}
# --------------------------------------------------------------------------

def score_youtube(ev: dict, cfg: dict) -> dict:
    yt, conf = ev.get("youtube") or {}, cfg["youtube"]
    if yt.get("unmeasured"):
        return {"state": UNMEASURED, "ratio": 0.0,
                "how": f"not measured ({yt['unmeasured']})", "detail": yt}

    videos = yt.get("videos") or []
    owned = yt.get("owned_channel") or {}
    parts = {}

    parts["owned_channel_ranks"] = 1.0 if owned.get("in_top_n") else (
        0.5 if owned.get("channel_id") else 0.0)

    if videos:
        good = 0
        for v in videos:
            has_caps = bool(v.get("captions"))
            chapters = len(_CHAPTER_RE.findall(v.get("description") or ""))
            desc_ok = len(v.get("description") or "") >= conf["min_description_chars"]
            good += (has_caps + (chapters >= conf["min_chapters"]) + desc_ok) / 3
        parts["video_citability"] = good / len(videos)
    else:
        parts["video_citability"] = 0.0

    fresh = [d for d in (_iso_days_ago(v.get("published")) for v in videos)
             if d is not None]
    parts["freshness"] = 1.0 if (fresh and min(fresh) <= conf["freshness_days"]) else 0.0
    parts["third_party_mentions"] = 1.0 if yt.get("third_party_videos") else 0.0

    weights = {"owned_channel_ranks": 0.35, "video_citability": 0.35,
               "freshness": 0.15, "third_party_mentions": 0.15}
    ratio = sum(parts[k] * w for k, w in weights.items())

    actions = []
    if parts["owned_channel_ranks"] < 1.0:
        actions.append("Own the brand term on YouTube: a channel that does not "
                       "rank for your own name cannot be cited for it.")
    if parts["video_citability"] < 0.8:
        actions.append(f"Add real captions (not auto-generated), at least "
                       f"{conf['min_chapters']} chapter timestamps, and a "
                       f"{conf['min_description_chars']}+ character description "
                       f"carrying your URL, to every video.")
    if not parts["freshness"]:
        actions.append(f"Publish something within {conf['freshness_days']} days: "
                       f"recency roughly triples citation likelihood.")
    return {"state": MEASURED, "ratio": ratio,
            "how": "owned channel rank, per-video captions/chapters/description, "
                   "recency, and third-party coverage of the brand term",
            "detail": {"components": {k: round(v, 3) for k, v in parts.items()},
                       "videos_examined": len(videos),
                       "owned_channel": owned,
                       "third_party_videos": yt.get("third_party_videos", 0)},
            "actions": actions}


def score_reddit(ev: dict, cfg: dict) -> dict:
    rd, conf = ev.get("reddit") or {}, cfg["reddit"]
    if rd.get("unmeasured"):
        return {"state": UNMEASURED, "ratio": 0.0,
                "how": f"not measured ({rd['unmeasured']})",
                "detail": {**rd, "terms_note": conf["tos_note"]}}

    posts = rd.get("posts") or []
    subs = {p.get("subreddit") for p in posts if p.get("subreddit")}
    organic = [p for p in posts if not p.get("self_promo")]
    engaged = [p for p in posts if (p.get("score") or 0) >= conf["min_score"]]

    parts = {
        "subreddit_spread": min(1.0, len(subs) / conf["min_subreddits"]),
        "engagement": min(1.0, len(engaged) / max(1, conf["min_subreddits"])),
        "organic_not_self_promo": (len(organic) / len(posts)) if posts else 0.0,
    }
    weights = {"subreddit_spread": 0.5, "engagement": 0.25,
               "organic_not_self_promo": 0.25}
    ratio = sum(parts[k] * w for k, w in weights.items())

    actions = []
    if parts["subreddit_spread"] < 1.0:
        actions.append(f"Presence in fewer than {conf['min_subreddits']} "
                       f"subreddits. Earn this by answering questions in the "
                       f"communities that already discuss your category. Do not "
                       f"post promotional threads: Reddit removes them and the "
                       f"account, and seeded mentions are what platforms filter for.")
    return {"state": MEASURED, "ratio": ratio,
            "how": "distinct subreddits, engaged threads, and the share of "
                   "mentions that are not self-promotion, over 12 months",
            "detail": {"components": {k: round(v, 3) for k, v in parts.items()},
                       "posts": len(posts), "subreddits": sorted(s for s in subs)[:15],
                       "terms_note": conf["tos_note"]},
            "actions": actions}


def score_wikidata(ev: dict, cfg: dict) -> dict:
    wd = ev.get("wikidata") or {}
    if wd.get("unmeasured"):
        return {"state": UNMEASURED, "ratio": 0.0,
                "how": f"not measured ({wd['unmeasured']})", "detail": wd}
    props = wd.get("properties") or {}
    parts = {
        "item_exists": 1.0 if wd.get("qid") else 0.0,
        "website_claim_matches": 1.0 if wd.get("website_matches") else 0.0,
        "typed": 1.0 if (props.get("P31") or props.get("P452")) else 0.0,
        "social_handles": min(1.0, sum(
            1 for p in ("P2002", "P2013", "P2397", "P4264") if props.get(p)) / 2),
    }
    weights = {"item_exists": 0.4, "website_claim_matches": 0.3,
               "typed": 0.15, "social_handles": 0.15}
    ratio = sum(parts[k] * w for k, w in weights.items())

    actions = []
    if not parts["item_exists"]:
        actions.append("Create a Wikidata item. Wikidata has no notability bar "
                       "like Wikipedia's, it is the single highest-return action "
                       "in this whole report, and it is free.")
    elif ratio < 1.0:
        missing = [k for k, v in parts.items() if v < 1.0]
        actions.append(f"Complete the Wikidata item: {', '.join(missing)}.")
    return {"state": MEASURED, "ratio": ratio,
            "how": "Wikidata item, its P856 official website claim, its type "
                   "claims, and its social handle properties",
            "detail": {"qid": wd.get("qid"),
                       "components": {k: round(v, 3) for k, v in parts.items()},
                       "properties_present": sorted(props)},
            "actions": actions}


def score_wikipedia(ev: dict, cfg: dict) -> dict:
    wp = ev.get("wikipedia") or {}
    if wp.get("unmeasured"):
        return {"state": UNMEASURED, "ratio": 0.0,
                "how": f"not measured ({wp['unmeasured']})", "detail": wp}
    if not wp.get("title") and wp.get("notability_unmet"):
        return {"state": NOT_APPLICABLE, "ratio": 0.0,
                "how": "no article, and the organisation does not meet Wikipedia's "
                       "notability threshold, so this is not a gap to close",
                "detail": {"note": cfg["wikipedia"]["notability_note"]}}
    parts = {
        "article_exists": 1.0 if wp.get("title") else 0.0,
        "not_orphaned": 1.0 if (wp.get("backlinks") or 0) >= 3 else 0.0,
        "cites_our_domain": 1.0 if wp.get("cites_domain") else 0.0,
    }
    weights = {"article_exists": 0.5, "not_orphaned": 0.25, "cites_our_domain": 0.25}
    ratio = sum(parts[k] * w for k, w in weights.items())

    actions = []
    if not parts["article_exists"]:
        actions.append("No Wikipedia article. This cannot be bought or written "
                       "by you; it is earned through independent coverage. Treat "
                       "it as a long-horizon PR goal, not a task.")
    elif not parts["cites_our_domain"]:
        actions.append("The article does not cite your domain. A citation there "
                       "is one of the strongest entity signals available.")
    return {"state": MEASURED, "ratio": ratio,
            "how": "article existence, incoming article links, and whether the "
                   "article cites the client domain",
            "detail": {"title": wp.get("title"),
                       "backlinks": wp.get("backlinks"),
                       "components": {k: round(v, 3) for k, v in parts.items()}},
            "actions": actions}


def score_linkedin(ev: dict, cfg: dict) -> dict:
    li = ev.get("linkedin") or {}
    note = cfg["linkedin"]["measurable_note"]
    parts = {
        "declared_in_same_as": 1.0 if li.get("declared_url") else 0.0,
        "page_resolves": 1.0 if li.get("resolves") else 0.0,
    }
    ratio = sum(parts.values()) / 2
    actions = []
    if not parts["declared_in_same_as"]:
        actions.append("Add your LinkedIn company page to the Organization "
                       "sameAs array so the entity graph connects.")
    return {"state": MEASURED, "ratio": ratio,
            "how": "company page declared in sameAs, and whether that URL resolves. "
                   "Nothing further is measurable from outside.",
            "detail": {"components": {k: round(v, 3) for k, v in parts.items()},
                       "declared_url": li.get("declared_url"),
                       "self_reported_followers": li.get("self_reported_followers"),
                       "limits": note},
            "actions": actions}


def score_entity_consistency(ev: dict, cfg: dict) -> dict:
    """Does the same entity round-trip across every platform we found."""
    site_same_as = {u.lower() for u in (ev.get("site_same_as") or [])}
    found, linked = [], []
    for platform, url in (
        ("youtube", ((ev.get("youtube") or {}).get("owned_channel") or {}).get("url")),
        ("wikidata", (ev.get("wikidata") or {}).get("url")),
        ("wikipedia", (ev.get("wikipedia") or {}).get("url")),
        ("linkedin", (ev.get("linkedin") or {}).get("declared_url")),
    ):
        if not url:
            continue
        found.append(platform)
        key = url.lower().rstrip("/")
        if any(key in s or s.rstrip("/") in key for s in site_same_as):
            linked.append(platform)

    if not found:
        return {"state": MEASURED, "ratio": 0.0,
                "how": "no platform profiles found to cross-reference",
                "detail": {"found": [], "linked_from_site": []},
                "actions": ["Nothing to connect yet. Wikidata first."]}
    ratio = len(linked) / len(found)
    actions = []
    missing = sorted(set(found) - set(linked))
    if missing:
        actions.append(f"Your site's Organization sameAs does not link: "
                       f"{', '.join(missing)}. Each missing link is an entity "
                       f"the graph cannot connect back to you.")
    return {"state": MEASURED, "ratio": ratio,
            "how": "profiles found, over profiles the site links from its "
                   "Organization sameAs",
            "detail": {"found": found, "linked_from_site": linked},
            "actions": actions}


PLATFORMS = [
    ("youtube", score_youtube),
    ("reddit", score_reddit),
    ("wikidata", score_wikidata),
    ("wikipedia", score_wikipedia),
    ("linkedin", score_linkedin),
    ("entity_consistency", score_entity_consistency),
]


# --------------------------------------------------------------------------
# scoring, with redistribution for anything genuinely unmeasurable
# --------------------------------------------------------------------------

def score(evidence: dict, cfg: Optional[dict] = None) -> dict:
    """Pure. Unmeasured platforms have their weight shared out, not zeroed."""
    cfg = cfg or load_config()["social_surface"]
    base, titles = cfg["weights"], cfg["titles"]
    assert sum(base.values()) == 100, f"weights sum to {sum(base.values())}"
    assert base["linkedin"] <= 10, "LinkedIn must stay capped: it is barely measurable"

    outs = {pid: fn(evidence, cfg) for pid, fn in PLATFORMS}
    skipped = [p for p, o in outs.items() if o["state"] in (UNMEASURED, NOT_APPLICABLE)]
    scored = [p for p in base if p not in skipped]

    freed = sum(base[p] for p in skipped)
    kept = sum(base[p] for p in scored)
    weights = {p: base[p] + (freed * base[p] / kept if kept else 0) for p in scored}

    rows = []
    for pid, _ in PLATFORMS:
        out = outs[pid]
        weight = round(weights.get(pid, 0.0), 2)
        ratio = max(0.0, min(1.0, float(out["ratio"])))
        rows.append({
            "id": pid,
            "title": titles[pid],
            "state": out["state"],
            "base_weight": base[pid],
            "weight": weight,
            "ratio": round(ratio, 4),
            "points": round(weight * ratio, 2),
            "how_measured": out["how"],
            "detail": out.get("detail", {}),
            "actions": out.get("actions", []),
        })

    total = round(sum(r["points"] for r in rows))
    coverage = kept / 100
    return {
        "brand": evidence.get("brand"),
        "domain": evidence.get("domain"),
        "scoring_version": SCORING_VERSION,
        "social_surface": total,
        "coverage": round(coverage, 2),
        "low_confidence": coverage < cfg["min_coverage_for_headline"],
        "correlation_source": cfg["correlation_source"],
        "platforms": rows,
        "redistributed": skipped,
        "actions": [a for r in rows for a in r["actions"]],
    }


# --------------------------------------------------------------------------
# quota
# --------------------------------------------------------------------------

def quota_projection(cfg: dict, *, youtube: bool) -> dict:
    q = cfg["quota"]
    units = (q["youtube_search_units"] + 2 * q["youtube_list_units"]) if youtube else 0
    return {"youtube_units": units,
            "daily_budget": q["youtube_daily_units"],
            "runs_per_day": (q["youtube_daily_units"] // units) if units else None}


# --------------------------------------------------------------------------
# gathering
# --------------------------------------------------------------------------

def gather_youtube(brand: str, domain: str, cfg: dict) -> dict:
    try:
        from google_auth import get_api_key
        key = get_api_key()
    except Exception as exc:  # noqa: BLE001
        return {"unmeasured": f"no YouTube API key ({type(exc).__name__})"}
    if not key:
        return {"unmeasured": "no YouTube API key configured"}

    if "PASTE_" in key:
        # The placeholder is still sitting in google-api.json. Say so plainly
        # rather than letting Google answer with an opaque 400.
        return {"unmeasured": "YouTube API key placeholder not replaced yet in "
                              "~/.config/claude-seo/google-api.json"}

    conf = cfg["youtube"]
    search = _get_json(
        f"{YOUTUBE_API}/search?part=snippet&type=video&maxResults="
        f"{conf['search_results']}&q={brand}&key={key}")
    if "items" not in search:
        return {"unmeasured": f"YouTube search failed ({search.get('_status') or search.get('_error')})"}

    items = search["items"]
    channels = [i["snippet"].get("channelTitle", "") for i in items]
    lower_brand = brand.lower()
    owned_idx = next((i for i, c in enumerate(channels)
                      if lower_brand in (c or "").lower()), None)
    owned = {}
    if owned_idx is not None:
        snip = items[owned_idx]["snippet"]
        owned = {"channel_id": snip.get("channelId"),
                 "title": snip.get("channelTitle"),
                 "url": f"https://www.youtube.com/channel/{snip.get('channelId')}",
                 "in_top_n": owned_idx < conf["top_n_for_owned"]}

    ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
    videos = []
    if ids:
        details = _get_json(
            f"{YOUTUBE_API}/videos?part=snippet,contentDetails&id="
            f"{','.join(ids[:20])}&key={key}")
        for item in details.get("items", []):
            snip, content = item.get("snippet", {}), item.get("contentDetails", {})
            if owned and snip.get("channelId") != owned.get("channel_id"):
                continue
            videos.append({
                "id": item.get("id"),
                "title": snip.get("title"),
                "description": snip.get("description", ""),
                "published": snip.get("publishedAt"),
                "captions": content.get("caption") == "true",
            })
    third_party = sum(1 for i in items
                      if not owned or i["snippet"].get("channelId") != owned.get("channel_id"))
    return {"videos": videos, "owned_channel": owned,
            "third_party_videos": third_party, "results": len(items)}


def gather_reddit(brand: str, domain: str, cfg: dict) -> dict:
    conf = cfg["reddit"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * conf["lookback_months"])
    data = _get_json(
        f"https://www.reddit.com/search.json?q={brand}&sort=relevance"
        f"&t=year&limit={conf['search_limit']}")
    children = (data.get("data") or {}).get("children")
    if children is None:
        return {"unmeasured": f"Reddit search unavailable "
                              f"({data.get('_status') or data.get('_error') or 'blocked'})"}
    posts = []
    for child in children:
        d = child.get("data") or {}
        created = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
        if created < cutoff:
            continue
        url = (d.get("url") or "") + " " + (d.get("selftext") or "")
        posts.append({
            "subreddit": d.get("subreddit"),
            "score": d.get("score", 0),
            "title": d.get("title"),
            # A link post pointing at the brand's own domain reads as self-promo.
            "self_promo": bool(domain and domain in url),
        })
    return {"posts": posts}


def gather_wikidata(brand: str, domain: str, cfg: dict) -> dict:
    conf = cfg["wikidata"]
    search = _get_json(f"{WIKIDATA_API}?action=wbsearchentities&format=json"
                       f"&language=en&limit={conf['search_limit']}&search={brand}")
    hits = search.get("search")
    if hits is None:
        return {"unmeasured": f"Wikidata search unavailable "
                              f"({search.get('_status') or search.get('_error')})"}
    if not hits:
        return {"qid": None, "properties": {}, "website_matches": False}

    ids = "|".join(h["id"] for h in hits)
    entities = _get_json(f"{WIKIDATA_API}?action=wbgetentities&format=json"
                         f"&props=claims|sitelinks&ids={ids}")
    best, best_props, matched = None, {}, False
    for qid, body in (entities.get("entities") or {}).items():
        claims = body.get("claims") or {}
        present = {p: True for p in conf["properties"] if claims.get(p)}
        site_ok = any(
            domain in str(c.get("mainsnak", {}).get("datavalue", {}).get("value", ""))
            for c in claims.get("P856", []))
        if site_ok:
            return {"qid": qid, "url": f"https://www.wikidata.org/wiki/{qid}",
                    "properties": present, "website_matches": True,
                    "sitelinks": len(body.get("sitelinks") or {})}
        if best is None:
            best, best_props = qid, present
    return {"qid": best,
            "url": f"https://www.wikidata.org/wiki/{best}" if best else None,
            "properties": best_props, "website_matches": matched,
            "ambiguous": True}


def gather_wikipedia(brand: str, domain: str, cfg: dict) -> dict:
    conf = cfg["wikipedia"]
    search = _get_json(f"{WIKIPEDIA_API}?action=query&format=json&list=search"
                       f"&srlimit={conf['search_limit']}&srsearch={brand}")
    results = ((search.get("query") or {}).get("search"))
    if results is None:
        return {"unmeasured": f"Wikipedia search unavailable "
                              f"({search.get('_status') or search.get('_error')})"}
    exact = next((r for r in results
                  if (r.get("title") or "").lower() == brand.lower()), None)
    if not exact:
        return {"title": None, "notability_unmet": True, "candidates":
                [r.get("title") for r in results[:3]]}

    title = exact["title"]
    links = _get_json(f"{WIKIPEDIA_API}?action=query&format=json&list=backlinks"
                      f"&bltitle={title}&bllimit=50")
    ext = _get_json(f"{WIKIPEDIA_API}?action=query&format=json&prop=extlinks"
                    f"&titles={title}&ellimit=500")
    pages = (ext.get("query") or {}).get("pages") or {}
    cites = any(domain in (link.get("*") or "")
                for page in pages.values() for link in (page.get("extlinks") or []))
    return {"title": title,
            "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
            "backlinks": len(((links.get("query") or {}).get("backlinks")) or []),
            "cites_domain": cites}


def gather_linkedin(brand: str, domain: str, site_same_as: list, cfg: dict) -> dict:
    declared = next((u for u in site_same_as if _LINKEDIN_RE.search(u)), None)
    resolves = False
    if declared:
        try:
            resp = safe_requests_get(declared, timeout=15, headers=UA)
            resolves = resp.status_code == 200
        except Exception:  # noqa: BLE001 - LinkedIn blocks plenty of clients
            resolves = False
    return {"declared_url": declared, "resolves": resolves,
            "self_reported_followers": None}


def gather(brand: str, domain: str, *, site_same_as: Optional[list] = None,
           quota_budget: Optional[int] = None) -> dict:
    cfg = load_config()["social_surface"]
    site_same_as = site_same_as or []

    projection = quota_projection(cfg, youtube=True)
    print(f"quota projection: {projection['youtube_units']} YouTube units "
          f"({projection['runs_per_day']} runs/day against the "
          f"{projection['daily_budget']} unit free tier)")
    budget = quota_budget if quota_budget is not None else cfg["quota"]["default_run_budget"]
    if projection["youtube_units"] > budget:
        print(f"refusing to run: projection exceeds --quota-budget {budget}")
        return {"brand": brand, "domain": domain,
                "youtube": {"unmeasured": "over quota budget"},
                "site_same_as": site_same_as}

    return {
        "brand": brand,
        "domain": domain,
        "site_same_as": site_same_as,
        "youtube": gather_youtube(brand, domain, cfg),
        "reddit": gather_reddit(brand, domain, cfg),
        "wikidata": gather_wikidata(brand, domain, cfg),
        "wikipedia": gather_wikipedia(brand, domain, cfg),
        "linkedin": gather_linkedin(brand, domain, site_same_as, cfg),
    }


def audit(brand: str, domain: str, **kwargs) -> dict:
    return score(gather(brand, domain, **kwargs))


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def _canned(strong: bool) -> dict:
    """Canned API shapes. `strong` is a brand doing everything right."""
    if strong:
        return {
            "brand": "Northwind Instruments", "domain": "northwind.test",
            "site_same_as": ["https://www.youtube.com/channel/UCdemo",
                             "https://www.wikidata.org/wiki/Q7059932",
                             "https://en.wikipedia.org/wiki/Northwind_Instruments",
                             "https://www.linkedin.com/company/northwind-instruments"],
            "youtube": {
                "owned_channel": {"channel_id": "UCdemo", "title": "Northwind Instruments",
                                  "url": "https://www.youtube.com/channel/UCdemo",
                                  "in_top_n": True},
                "third_party_videos": 4,
                "videos": [{"id": "v1", "captions": True,
                            "published": datetime.now(timezone.utc).isoformat(),
                            "description": "0:00 Intro\n1:20 Calibration\n4:05 Wrap\n"
                                           + "Full guide at https://northwind.test/docs " * 8}],
            },
            "reddit": {"posts": [
                {"subreddit": "labrats", "score": 42, "self_promo": False},
                {"subreddit": "chemistry", "score": 18, "self_promo": False},
                {"subreddit": "metrology", "score": 9, "self_promo": False}]},
            "wikidata": {"qid": "Q7059932", "url": "https://www.wikidata.org/wiki/Q7059932",
                         "website_matches": True,
                         "properties": {"P856": True, "P31": True, "P452": True,
                                        "P2002": True, "P4264": True}},
            "wikipedia": {"title": "Northwind Instruments",
                          "url": "https://en.wikipedia.org/wiki/Northwind_Instruments",
                          "backlinks": 11, "cites_domain": True},
            "linkedin": {"declared_url": "https://www.linkedin.com/company/northwind-instruments",
                         "resolves": True},
        }
    return {
        "brand": "Harbour Dental Care", "domain": "harbour.test",
        "site_same_as": [],
        "youtube": {"owned_channel": {}, "videos": [], "third_party_videos": 0},
        "reddit": {"posts": []},
        "wikidata": {"qid": None, "properties": {}, "website_matches": False},
        "wikipedia": {"title": None, "notability_unmet": True},
        "linkedin": {"declared_url": None, "resolves": False},
    }


def self_check() -> int:
    cfg = load_config()["social_surface"]
    failures = []

    total = sum(cfg["weights"].values())
    if total != 100:
        failures.append(f"weights sum to {total}, not 100")
    else:
        print("  ok    weights sum to 100")

    if cfg["weights"]["linkedin"] > 10:
        failures.append(f"LinkedIn weight {cfg['weights']['linkedin']} exceeds its cap of 10")
    else:
        print("  ok    LinkedIn capped at 10, the most it can honestly carry")

    if set(cfg["weights"]) != {pid for pid, _ in PLATFORMS}:
        failures.append("signals.json weights and the PLATFORMS registry disagree")
    else:
        print("  ok    every platform has a weight")

    if "domain_rating" in json.dumps(cfg).lower().replace("domain rating", "domain_rating"):
        if "deliberately absent" not in cfg["_comment"]:
            failures.append("Domain Rating leaked into the scoring model")
    print("  ok    Domain Rating is not a scored signal")

    strong = score(_canned(True), cfg)
    if strong["social_surface"] < 90:
        failures.append(f"a brand doing everything right scored "
                        f"{strong['social_surface']}, expected 90+")
    else:
        print(f"  ok    strong brand scores {strong['social_surface']}/100")

    weak = score(_canned(False), cfg)
    if weak["social_surface"] > 15:
        failures.append(f"a brand with no presence scored {weak['social_surface']}, "
                        f"expected under 15")
    else:
        print(f"  ok    absent brand scores {weak['social_surface']}/100")

    if "wikipedia" not in weak["redistributed"]:
        failures.append("a non-notable brand's Wikipedia weight was not redistributed")
    else:
        wp = next(r for r in weak["platforms"] if r["id"] == "wikipedia")
        others = [r for r in weak["platforms"] if r["id"] != "wikipedia"]
        if wp["weight"] != 0 or not all(r["weight"] >= r["base_weight"] for r in others):
            failures.append("redistribution did not move the freed weight")
        else:
            print("  ok    an unmeasurable platform redistributes rather than fails")

    # Redistribution must preserve the scale: still out of 100.
    reachable = sum(r["weight"] for r in weak["platforms"])
    if abs(reachable - 100) > 0.05:
        failures.append(f"weights after redistribution total {reachable}, not 100")
    else:
        print("  ok    redistribution keeps the total out of 100")

    # A score resting on half the model must refuse to be a headline. Real case:
    # no YouTube API key plus Reddit 403 leaves 45 of 100 points measurable.
    blind = _canned(False)
    blind["youtube"] = {"unmeasured": "no YouTube API key configured"}
    blind["reddit"] = {"unmeasured": "Reddit search unavailable (403)"}
    blind_result = score(blind, cfg)
    if not blind_result["low_confidence"]:
        failures.append(f"coverage {blind_result['coverage']} was not flagged "
                        f"low confidence")
    elif strong["low_confidence"] or strong["coverage"] != 1.0:
        failures.append("a fully measured brand was flagged low confidence")
    else:
        print(f"  ok    {blind_result['coverage']:.0%} coverage is flagged low "
              f"confidence, 100% is not")

    if not any("earn" in a or "Do not post" in a for a in weak["actions"]):
        failures.append("Reddit guidance must say earn, never post")
    else:
        print("  ok    Reddit guidance says earn, never seed")

    once, twice = score(_canned(True), cfg), score(_canned(True), cfg)
    if json.dumps(once, sort_keys=True) != json.dumps(twice, sort_keys=True):
        failures.append("two scorings of the same evidence differ")
    else:
        print("  ok    scoring is deterministic")

    proj = quota_projection(cfg, youtube=True)
    if not proj["youtube_units"] or not proj["runs_per_day"]:
        failures.append("quota projection produced nothing to print")
    else:
        print(f"  ok    quota projection: {proj['youtube_units']} units, "
              f"{proj['runs_per_day']} runs/day")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll social-surface self-checks passed.")
    return 0


# --------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("brand", nargs="?", help="brand name as people write it")
    ap.add_argument("--domain", help="bare domain, e.g. example.com")
    ap.add_argument("--same-as", action="append", default=[],
                    help="a sameAs URL from the site (repeatable)")
    ap.add_argument("--live", action="store_true", help="make real API calls")
    ap.add_argument("--quota-budget", type=int)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)

    if args.self_check:
        return self_check()
    if not args.brand or not args.domain:
        ap.error("brand and --domain are required unless --self-check is given")
    if not args.live:
        ap.error("live API calls cost quota; pass --live to confirm")

    result = audit(args.brand, args.domain, site_same_as=args.same_as,
                   quota_budget=args.quota_budget)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print(f"\n{result['brand']} ({result['domain']})")
    if result["low_confidence"]:
        missing = ", ".join(result["redistributed"])
        print(f"LOW CONFIDENCE: only {result['coverage']:.0%} of the model could be "
              f"measured ({missing} unavailable).")
        print(f"Do not present {result['social_surface']}/100 as a headline. "
              f"Fix the measurement gap first.")
    else:
        print(f"social citation surface {result['social_surface']}/100 "
              f"({result['coverage']:.0%} of the model measured)")
    for row in result["platforms"]:
        mark = {"measured": " ", "unmeasured": "~", "not_applicable": "-"}[row["state"]]
        print(f"  {mark} {row['points']:>5.1f}/{row['weight']:<6.1f} {row['title']}")
        print(f"      {row['how_measured']}")
    if result["redistributed"]:
        print(f"\n  weight redistributed from: {', '.join(result['redistributed'])}")
    if result["actions"]:
        print("\n  what to do:")
        for action in result["actions"]:
            print(f"   - {action}")
    print(f"\n  {result['correlation_source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
