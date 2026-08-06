# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Merge the three scores into one client deliverable.

    python -m bof.report https://example.com --brand "Brand" [--out ./audits]
    python -m bof.report --self-check

Writes the suite's own ``audit-data.json`` envelope, hands it to
``google_report.py`` to build branded HTML with charts, then prints that HTML to
PDF with Chromium.

Chromium and not WeasyPrint: WeasyPrint needs GTK natives that pip cannot
install on Windows, so ``--format pdf`` raises OSError here. Chromium is already
bundled with the suite, renders the same HTML, and will keep working on a VPS.

The headline is the AI Visibility Score: engine readability 40, social citation
surface 35, agent operability 25. Future readiness is reported beside it and
never folded in, because a site that has not shipped a draft standard is not
worse than one that has, it is just less early. A component that cannot be
measured with confidence drops out and the rest are renormalised.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import tempfile
from datetime import date
from typing import Optional
from urllib.parse import urlparse

from bof import SCORING_VERSION
from bof import suite  # noqa: F401  # must precede every suite import
from bof.agent_ready import lens_scores, load_config

from google_report import generate_report  # noqa: E402

SEVERITY_ORDER = ["critical", "warning", "info"]


def _severity(ratio: float, thresholds: dict) -> Optional[str]:
    """None means the signal is fully satisfied and belongs in what_works."""
    if ratio >= 1.0:
        return None
    if ratio < thresholds["critical"]:
        return "critical"
    if ratio < thresholds["warning"]:
        return "warning"
    return "info"


def _executive_summary(domain: str, headline: int, lenses: dict, plan: list,
                       quick_target: int, platform: Optional[dict],
                       social_low: bool) -> str:
    """One paragraph for whoever is paying, in their language, from real numbers.

    Every figure here is read off the envelope. Nothing is a template constant,
    and a self-check asserts the numbers quoted match the ones computed.
    """
    eng = lenses["engine_readability"]
    ops = lenses["agent_operability"]

    def phrase(lens):
        if not lens["applicable"]:
            return "does not apply to this page"
        return f"{lens['score']} out of 100"

    lines = [
        f"{domain} scores {headline} out of 100 for AI visibility.",
        f"Broken down: assistants reading and quoting you, {phrase(eng)}; "
        f"agents acting on your site, {phrase(ops)}.",
    ]
    quick = [p for p in plan if p["effort"] == "quick"]
    if quick and quick_target > headline:
        # Titles as written: lowercasing turns sameAs into sameas.
        names = "; ".join(p["title"] for p in quick[:5])
        lines.append(
            f"{len(quick)} of the fixes below take under an hour each ({names}). "
            f"Doing only those takes the score to about {quick_target}.")
    elif not plan:
        lines.append("Nothing measured here has an unclaimed fix worth a point, "
                     "which is unusual and worth checking against a second run.")
    if platform:
        lines.append(
            f"This site runs on {platform['name']}, which controls some of the "
            f"markup outright. Findings you cannot act on from inside the "
            f"platform are marked, and carry a workaround instead of an "
            f"instruction.")
    if social_low:
        lines.append(
            "The social citation surface could not be measured well enough to "
            "count towards the headline, so this number rests on what is "
            "verifiable on the page itself.")
    return " ".join(lines)


def detect_platform(evidence: dict, cfg: dict) -> Optional[dict]:
    """Which CMS built this page, from evidence already gathered.

    Header match first, then HTML fingerprints. Returns None rather than
    guessing: an unrecognised platform must not inherit another one's excuses.
    """
    html = (evidence or {}).get("rendered_html") or ""
    raw = {str(k).lower(): str(v).lower()
           for k, v in (((evidence or {}).get("render") or {}).get("headers") or {}).items()}
    for spec in cfg["platforms"]["detect"]:
        for header, needle in (spec.get("headers") or {}).items():
            if header in raw and needle.lower() in raw[header]:
                return spec
        if any(f in html for f in spec.get("html") or []):
            return spec
    return None


def _signal_classes(agent: dict, seo: Optional[dict], social: Optional[dict],
                    thresholds: dict) -> dict:
    """Every signal reduced to one word, for diffing two runs.

    ``ok`` when satisfied, ``n/a`` when it had nothing to measure, otherwise
    its severity. A class change is what separates a real regression from a
    page that scored two points differently because a crawler was slow.
    """
    out = {}
    for row in agent.get("signals") or []:
        if row.get("not_applicable") or row.get("unavailable"):
            out[row["id"]] = "n/a"
        else:
            out[row["id"]] = _severity(row["ratio"], thresholds) or "ok"
    for row in (seo or {}).get("signals") or []:
        out[row["id"]] = _severity(row["ratio"], thresholds) or "ok"
    for row in (social or {}).get("platforms") or []:
        if row.get("state") in ("unmeasured", "not_applicable"):
            out[row["id"]] = "n/a"
        else:
            out[row["id"]] = _severity(row["ratio"], thresholds) or "ok"
    return out


def seo_recs(cfg: dict) -> dict:
    """SEO recommendations live under the seo block, not beside the signal ones."""
    return cfg["seo"]["recommendations"]


def _headline_lift(sid: str, ratio: float, lenses: dict, blend: dict,
                   kept: float, weights: dict) -> float:
    """Headline points gained by taking one signal to full marks.

    Exact, not estimated: every term is already on disk. A signal earns
    ``(1 - ratio) * weight`` more points, which is worth that share of its
    lens's own denominator, which is worth its lens's renormalised share of the
    headline. A2 sits in both lenses, so it is summed over both and is worth
    roughly twice what its weight alone suggests, which is the honest answer.

    ``lenses`` holds only the lenses that actually vote, so a signal inside a
    dropped-out lens promises nothing, and a page where nothing votes promises
    nothing rather than dividing by zero.
    """
    gain = (1.0 - ratio) * weights[sid]
    return sum(blend[lid] / kept * 100 * gain / lens["possible"]
               for lid, lens in lenses.items() if sid in lens["signals"])


def _finding(row: dict, recommendations: dict, thresholds: dict,
             *, weight: Optional[float] = None,
             lift: Optional[float] = None,
             cfg: Optional[dict] = None,
             platform: Optional[dict] = None) -> Optional[dict]:
    """None when the signal is satisfied, or when it was never measured.

    An unmeasured platform must never appear as a critical finding. Scoring it
    zero and calling that critical would blame a client for our missing API key,
    which is how a report loses its credibility in one line.
    """
    if row.get("state") in ("unmeasured", "not_applicable"):
        return None
    severity = _severity(row["ratio"], thresholds)
    if severity is None:
        return None
    cfg = cfg or {}
    sid = row["id"]

    # Critical is reserved for signals that stop AI visibility outright. Without
    # this gate a normal page raised ten criticals out of twenty-four findings,
    # and a word that describes almost everything describes nothing.
    if severity == "critical" and sid not in set(cfg.get("blockers", {}).get("ids", [])):
        severity = "warning"

    limited = bool(platform and sid in (platform.get("limited") or []))
    weight = row["weight"] if weight is None else weight
    scored = round(weight * row["ratio"], 1)

    parts = [f"Scoring {scored:g} of {weight:g} points. "
             f"Measured by: {row['how_measured']}."]

    # What we actually saw. The detail dict is computed for every signal and was
    # previously discarded at report time, which left every finding an assertion
    # rather than something a client could check against their own page.
    seen = _evidence_line(row)
    if seen:
        parts.append(f"What we saw: {seen}.")

    means = (cfg.get("meaning") or {}).get(sid) or {}
    if means.get("means"):
        parts.append(f"What it means: {means['means']}")
    if means.get("risk"):
        parts.append(f"If you leave it: {means['risk']}")

    if limited:
        # A lift this person cannot collect is a false promise, so it is not
        # made. The score still counts it, because an agent hitting the page is
        # blocked either way and pretending otherwise would flatter the number.
        parts.append(f"Platform limit: {platform['name']} controls this and you "
                     f"cannot change it from inside the platform. It still "
                     f"counts against the score, because an assistant reading "
                     f"the page is affected either way.")
    elif lift and round(lift) >= 1:
        parts.append(f"Closing this adds about {round(lift)} points to the "
                     f"AI Visibility Score.")

    recommendation = recommendations.get(sid, "")
    if limited and platform.get("workaround"):
        recommendation = f"{platform['workaround']} (Ideally: {recommendation})"

    return {
        "title": row["title"],
        "severity": severity,
        "description": " ".join(parts),
        "recommendation": recommendation,
        "_id": sid,
        "_ratio": row["ratio"],
        # Suppressed for platform-limited signals so the simulator and the
        # action plan cannot promise it either.
        "_lift": 0.0 if limited else (round(lift, 1) if lift else 0.0),
        "_limited": limited,
    }


def _evidence_line(row: dict) -> str:
    """The detail dict as one readable clause, or empty when it says nothing."""
    detail = row.get("detail") or {}
    if not isinstance(detail, dict):
        return ""
    out = []
    for key, value in detail.items():
        if key.startswith("_") or value == {}:
            continue
        if value is None:
            # Absence is the evidence on a signal that scored zero for a missing
            # thing. Skipping it left exactly those findings with nothing to show.
            out.append(f"{key.replace('_', ' ')}: none found")
        elif isinstance(value, list):
            if not value:
                out.append(f"{key.replace('_', ' ')}: none")
                continue
            shown = ", ".join(str(v) for v in value[:4])
            if len(value) > 4:
                shown += f" and {len(value) - 4} more"
            out.append(f"{key.replace('_', ' ')}: {shown}")
        elif isinstance(value, bool):
            out.append(f"{key.replace('_', ' ')}: {'yes' if value else 'no'}")
        elif isinstance(value, (int, float, str)):
            text = str(value)
            if not text.strip():
                continue
            out.append(f"{key.replace('_', ' ')}: {text[:120]}")
        if len(out) >= 5:
            break
    return "; ".join(out)


def build_envelope(agent: dict, future: dict, social: Optional[dict],
                   *, domain: str, cfg: Optional[dict] = None,
                   seo: Optional[dict] = None,
                   evidence: Optional[dict] = None) -> dict:
    """Pure. Three score dicts in, one audit-data.json envelope out."""
    cfg = cfg or load_config()
    rcfg, recs = cfg["report"], cfg["recommendations"]
    thresholds = rcfg["severity_thresholds"]

    # Which CMS built the page decides whether a recommendation is an
    # instruction or a workaround. None when unrecognised: an unknown platform
    # must not inherit another one's excuses.
    platform = detect_platform(evidence, cfg) if evidence else None

    agent_score = agent.get("score", 0)
    # Computed here when absent so canned fixtures and the gather-failure stub
    # take the same path as a real run.
    lenses = agent.get("lenses") or lens_scores(agent.get("signals", []), cfg)
    social_low = bool(social and social.get("low_confidence"))

    # The AI Visibility Score. A component that cannot carry a number drops out
    # and the rest are renormalised pro rata, so the headline stays out of 100
    # rather than being quietly marked down for our own measurement gap.
    w = rcfg["headline_weights"]
    parts = {lid: lens["score"] for lid, lens in lenses.items()
             if lens["applicable"]}
    if social and not social_low:
        parts["social_surface"] = social["social_surface"]
    voting = {lid: lens for lid, lens in lenses.items() if lid in parts}
    kept = sum(w[k] for k in parts)
    # Everything dropped out: a page with nothing an assistant or an agent could
    # look at. That is a real answer, not a division to guard against.
    headline = round(sum(w[k] / kept * v for k, v in parts.items())) if kept else 0
    # The arithmetic as data, not just as a sentence. "Why is it 69?" is
    # answered with the four columns below rather than with the word "weighted".
    basis_rows = [{"component": k, "weight": w[k],
                   "normalised_weight": round(w[k] / kept, 3),
                   "score": parts[k],
                   "contribution": round(w[k] / kept * parts[k], 1)}
                  for k in sorted(parts, key=lambda k: -w[k])]
    named = ", ".join(f"{k.replace('_', ' ')} {w[k] / kept:.0%}"
                      for k in sorted(parts, key=lambda k: -w[k]))
    basis = (f"AI Visibility Score: {named}" if kept else
             "AI Visibility Score: nothing could be measured on this page")
    for lid, lens in lenses.items():
        if not lens["applicable"]:
            gone = ", ".join(lens["dropped_signals"]) or "every signal"
            basis += (f". {lens['title']} does not apply to this page: "
                      f"{gone} had nothing to measure, leaving too little of "
                      f"the lens to carry a number")
    if social_low:
        basis += (". The social citation surface dropped out: it could not be "
                  "measured well enough to carry a headline number, so the "
                  "remaining weights were renormalised")
    elif not social:
        basis += ". The social citation surface was not measured on this run"

    categories, findings = [], []

    unavailable = set(agent.get("unavailable") or [])
    for lens_id, lens in lenses.items():
        # A2 belongs to both lenses on purpose, so it is reported in both. A
        # signal that dropped out raises no finding: there is no fault to fix.
        skip = unavailable | set(lens["dropped_signals"])
        rows = [r for r in agent["signals"] if r["id"] in set(lens["signals"])]
        lens_findings = [f for f in (
            _finding(r, recs, thresholds, cfg=cfg, platform=platform,
                     lift=_headline_lift(r["id"], r["ratio"], voting, w, kept,
                                         cfg["weights"]))
            for r in rows if r["id"] not in skip) if f]
        categories.append({
            "name": lens["title"],
            # google_report renders this straight into the client's HTML, so a
            # lens that does not apply shows 0 with the reason spelled out
            # rather than a bare None that would print as "null".
            "score": lens["score"] if lens["applicable"] else 0,
            "applicable": lens["applicable"],
            "not_applicable_reason": (
                None if lens["applicable"] else
                f"This page has nothing for this lens to measure "
                f"({', '.join(lens['dropped_signals'])} did not apply), so it "
                f"carries no score and does not affect the AI Visibility Score."),
            "owner": lens["owner"],
            "what_works": [r["title"] for r in rows
                           if r["ratio"] >= 1.0 and r["id"] not in skip],
            "findings": [{k: v for k, v in f.items() if not k.startswith("_")}
                         for f in lens_findings],
        })
        findings += lens_findings

    if social:
        social_findings = []
        for row in social["platforms"]:
            # Base weight, not the post-redistribution weight: telling a client
            # Wikidata is worth 50 points because two other platforms could not
            # be measured is arithmetically true and reads as nonsense.
            finding = _finding(row, recs, thresholds, cfg=cfg,
                               weight=row.get("base_weight", row["weight"]))
            if not finding:
                continue
            if row.get("actions"):
                finding["recommendation"] = " ".join(row["actions"])
            social_findings.append(finding)

        skipped = social.get("redistributed") or []
        if skipped:
            names = {r["id"]: r["title"] for r in social["platforms"]}
            reasons = "; ".join(
                f"{names.get(p, p)}: {r['how_measured']}"
                for p in skipped for r in social["platforms"] if r["id"] == p)
            social_findings.append({
                "title": "Measurement coverage",
                "severity": "info",
                "description": (f"{social['coverage']:.0%} of this category could "
                                f"be measured. Not measured: {reasons}. Their "
                                f"weight was shared across the platforms that "
                                f"could be measured, so the score stays out of "
                                f"100. This is a gap in our instrumentation, "
                                f"not a fault on the site."),
                "recommendation": ("Close the measurement gap before treating "
                                   "this category's score as comparable."),
                "_id": "coverage", "_ratio": social["coverage"],
            })

        categories.append({
            "name": "Social citation surface",
            "score": social["social_surface"],
            "what_works": [r["title"] for r in social["platforms"]
                           if r["ratio"] >= 1.0 and r.get("state") == "measured"],
            "findings": [{k: v for k, v in f.items() if not k.startswith("_")}
                         for f in social_findings],
        })
        findings += social_findings

    # Supporting evidence, reported after the lenses and the social surface and
    # never folded into the headline. Its findings carry no lift, because a lift
    # is a promise about the AI Visibility Score and SEO cannot move it.
    if seo and seo.get("signals"):
        unmeasured = set(seo.get("unmeasured") or [])
        seo_findings = [f for f in (_finding(r, seo_recs(cfg), thresholds,
                                             cfg=cfg, platform=platform)
                                    for r in seo["signals"]
                                    if r["id"] not in unmeasured) if f]
        categories.append({
            "name": "SEO Technical Baseline (supporting evidence)",
            "score": seo["seo"],
            "owner": "seo",
            # Printed into the client's own findings file. A limits list that
            # only exists in the JSON is a limits list the client never reads.
            "scope": seo.get("scope"),
            "cannot_say": seo.get("cannot_say"),
            "caveat": seo.get("performance_note"),
            "what_works": [r["title"] for r in seo["signals"] if r["ratio"] >= 1.0],
            "findings": [{k: v for k, v in f.items() if not k.startswith("_")}
                         for f in seo_findings],
        })
        findings += seo_findings

    # Future readiness never produces critical or warning findings. Absence of a
    # draft standard is an opportunity, so every row lands in Phase 3.
    future_findings = []
    for row in future.get("probes", []):
        if row["weight"] == 0 or row["ratio"] >= 1.0:
            continue
        future_findings.append({
            "title": row["title"],
            "severity": "info",
            "description": f"{row['standard']}. {row['how_measured']}.",
            "recommendation": recs.get(row["id"], ""),
            "_id": row["id"],
            "_ratio": row["ratio"],
        })
    categories.append({
        "name": "Future readiness (opportunity only)",
        "score": future.get("future_readiness", 0),
        "what_works": [r["title"] for r in future.get("probes", [])
                       if r["status"] == "present" and r["weight"]],
        "findings": [{k: v for k, v in f.items() if not k.startswith("_")}
                     for f in future_findings],
    })
    findings += future_findings

    # A2 is deliberately reported inside both lenses, but the action plan should
    # ask for it once, not twice.
    findings = list({f["_id"]: f for f in findings}.values())
    by_severity = {s: [f for f in findings if f["severity"] == s]
                   for s in SEVERITY_ORDER}
    # Cheapest band first, then by what it is worth inside the band. Lift alone
    # would put "create a Wikidata item" above "publish a sitemap": months of
    # outside-party work ahead of ten minutes, because the months are worth
    # more points. That ordering is accurate and useless.
    effort, ranks = cfg["effort"], cfg["effort"]["_ranks"]

    def order(f: dict) -> tuple:
        band = effort.get(f["_id"], "project")
        return (ranks.get(band, 1), -f.get("_lift", 0.0))

    phases = []
    for phase in rcfg["phases"]:
        ranked = sorted(by_severity.get(phase["severity"], []), key=order)
        items = [f"{f['title']} ({effort.get(f['_id'], 'project')}): "
                 f"{f['recommendation']}"
                 for f in ranked if f["recommendation"]]
        if items:
            phases.append({"name": phase["name"], "timeframe": phase["timeframe"],
                           "items": items})

    # Cumulative projection, recomputed rather than summed. Lifts are not
    # additive: each one changes its lens's earned points, and the lens's share
    # of the headline is renormalised, so adding four lifts overstates the
    # result. This replays the arithmetic with each fix applied in turn.
    plan = []
    fixed_points = {lid: 0.0 for lid in voting}
    for f in sorted((x for x in findings if x.get("_lift", 0) > 0 and not x.get("_limited")),
                    key=order)[:8]:
        sid = f["_id"]
        gain = (1.0 - f["_ratio"]) * cfg["weights"].get(sid, 0)
        for lid, lens in voting.items():
            if sid in lens["signals"]:
                fixed_points[lid] += gain
        projected = round(sum(
            w[lid] / kept * min(100, lens["score"] + 100 * fixed_points[lid] / lens["possible"])
            for lid, lens in voting.items())
            + (w["social_surface"] / kept * parts["social_surface"]
               if "social_surface" in parts else 0))
        plan.append({"id": sid, "title": f["title"],
                     "effort": effort.get(sid, "project"),
                     "gain": round(f["_lift"], 1), "projected": projected})

    quick = [p for p in plan if p["effort"] == "quick"]
    quick_target = quick[-1]["projected"] if quick else headline

    return {
        "summary": {
            "health_score": headline,
            # A page a non-technical reader can act on without decoding a table.
            "executive_summary": _executive_summary(
                domain, headline, lenses, plan, quick_target, platform, social_low),
            "playbook": plan,
            # Deliberately absent: google_report renders business_type into
            # "for a <type> site", which no phrase describing this audit fits.
            "business_type": None,
            # Dicts, not strings: google_report's _finding_severity() reads
            # severity off a dict but hardcodes "Info" for a plain string, so
            # strings made every critical issue render as "Info: ...".
            "top_findings": [{"title": f["title"],
                              "severity": f["severity"].capitalize()}
                             for f in by_severity["critical"]
                             + by_severity["warning"]][:8],
            "quick_wins": [f["recommendation"] for f in by_severity["critical"]
                           if f["recommendation"]][:5],
        },
        "categories": categories,
        "action_plan": {"phases": phases},
        "artifacts": {"findings_dir": "findings", "screenshots_dir": "screenshots"},
        "bof": {
            "scoring_version": SCORING_VERSION,
            "signals_last_reviewed": cfg["last_reviewed"],
            "headline_basis": basis,
            "headline_basis_rows": basis_rows,
            "headline_confidence": "full" if len(parts) == len(w) else "partial",
            "dropped": sorted(set(w) - set(parts)),
            # Our own namespace, so google_report never renders these. The UI in
            # L3 and the delta in L7 read them as data rather than reparsing
            # sentences out of the findings.
            "top_lifts": [{"id": f["_id"], "title": f["title"],
                           "severity": f["severity"], "lift": f["_lift"]}
                          for f in sorted(findings, key=lambda f: -f.get("_lift", 0.0))
                          if f.get("_lift", 0.0) >= 1][:8],
            # None means the lens did not apply to this page, which is a
            # different fact from a score of zero and is stored as such.
            "engine_readability": lenses["engine_readability"]["score"],
            "engine_band": lenses["engine_readability"]["band"],
            "agent_operability": lenses["agent_operability"]["score"],
            "operability_band": lenses["agent_operability"]["band"],
            # Every signal's class, so a later run can tell a real regression
            # from noise. The report directory is rewritten on each run of the
            # same domain, so this is the only per-signal history that survives.
            "signal_classes": _signal_classes(agent, seo, social, thresholds),
            "platform": platform["id"] if platform else None,
            "platform_name": platform["name"] if platform else None,
            # id and title together: "B3" means nothing to the person reading
            # the start-here page, which is the whole audience for this list.
            "platform_limited": [{"id": f["_id"], "title": f["title"]}
                                 for f in sorted(findings, key=lambda x: x["_id"])
                                 if f.get("_limited")],
            "lenses_not_applicable": sorted(
                lid for lid, l in lenses.items() if not l["applicable"]),
            "lens_signals_dropped": {lid: l["dropped_signals"]
                                     for lid, l in lenses.items()
                                     if l["dropped_signals"]},
            # The flat twelve-signal score the two lenses are cut from. Kept so
            # any future drift between the parts and the whole is visible.
            "agent_readiness": agent_score,
            "agent_band": agent.get("band"),
            "future_readiness": future.get("future_readiness", 0),
            "social_surface": social.get("social_surface") if social else None,
            "social_coverage": social.get("coverage") if social else None,
            "social_low_confidence": social_low,
            # Reported beside the headline, never inside it. There is a check in
            # self_check() asserting that moving these from 0 to 100 leaves the
            # AI Visibility Score byte-identical.
            "seo": seo.get("seo") if seo else None,
            "seo_band": seo.get("band") if seo else None,
            "seo_field": seo.get("seo_field") if seo else None,
            "seo_field_reason": seo.get("seo_field_reason") if seo else None,
            "seo_scope": seo.get("scope") if seo else None,
            "seo_cannot_say": seo.get("cannot_say") if seo else None,
            "seo_performance_note": seo.get("performance_note") if seo else None,
            "domain": domain,
            "generated": date.today().isoformat(),
            "google_ai_overviews": ("not covered: no public API exists. Gemini API "
                                    "output is a different surface and is not "
                                    "presented as AI Overviews."),
        },
    }


#: Verbatim from google_report's template. If a suite upgrade reflows this by so
#: much as a space the replacement misses, and because the rule is required the
#: run stops rather than shipping the false claim.
_SUITE_SOURCES_TABLE = """<tbody>
      <tr><td>PageSpeed Insights API</td>
          <td>Lighthouse lab audit (mobile emulation, Moto G Power, slow 4G)</td>
          <td>Real-time</td></tr>
      <tr><td>Chrome UX Report (CrUX)</td>
          <td>28-day rolling field data from real Chrome users</td>
          <td>Daily ~04:00 UTC</td></tr>
      <tr><td>CrUX History API</td>
          <td>25-week p75 trend data per metric</td>
          <td>Weekly</td></tr>
      <tr><td>Google Search Console</td>
          <td>Search Analytics (clicks, impressions, CTR, position)</td>
          <td>2-3 day lag</td></tr>
      <tr><td>URL Inspection API</td>
          <td>Per-URL index status, coverage state, crawl info</td>
          <td>Real-time (2,000/day)</td></tr>
    </tbody>"""


def _sources_table() -> str:
    """What this audit actually reads. Nothing else belongs in the table."""
    rows = [
        ("The page itself, rendered",
         "One headless Chromium render, plus the same URL fetched with a "
         "plain non-browser client to compare",
         "Every run"),
        ("robots.txt",
         "Parsed and tested per citation crawler with a real robots parser",
         "Every run"),
        ("XML sitemap",
         "Declared in robots.txt or found at /sitemap.xml, parsed for URLs",
         "Every run"),
        ("Accessibility tree",
         "Chromium's own ARIA snapshot of the rendered page, which is what an "
         "agent navigates by",
         "Every run"),
        ("Wikidata API",
         "Whether an entity exists for this brand or domain, and whether the "
         "site links to it",
         "Every run"),
        ("Draft agent standards",
         "MCP discovery paths, WebMCP, NLWeb and UCP probed at their "
         "well-known locations; absence is never scored as a fault",
         "Every run"),
    ]
    body = "\n".join(
        f"      <tr><td>{s}</td>\n          <td>{d}</td>\n"
        f"          <td>{f}</td></tr>" for s, d, f in rows)
    return f"<tbody>\n{body}\n    </tbody>"


class BrandingError(RuntimeError):
    """A required branding string could not be replaced. Do not ship the PDF."""


def rebrand(html: str, envelope: dict, cfg: dict) -> str:
    """Replace google_report's own identity with ours.

    Required rules are the byline and footer. If either cannot be replaced,
    this raises rather than producing a client deliverable carrying a third
    party's name. Optional rules are wording only; a miss is logged, not fatal.
    """
    b = cfg["report"]["branding"]
    meta = envelope["bof"]
    footer = (f"Report generated by {b['author']} &mdash; agent visibility model "
              f"v{meta['scoring_version']}, signals reviewed "
              f"{meta['signals_last_reviewed']} &mdash; ")

    rules = [
        (True, '<div class="subtitle">Prepared by Claude SEO</div>',
         f'<div class="subtitle">Prepared by {b["author"]}</div>'),
        (True, "Report generated by Claude SEO &mdash; Google SEO Intelligence "
               "Skill &mdash; ", footer),
        (False, '<div class="badge">Full SEO Audit Report</div>',
         f'<div class="badge">{b["badge"]}</div>'),
        (False, '<div class="score-label">SEO Health Score</div>',
         f'<div class="score-label">{b["score_label"]}</div>'),
        (False, '<div class="label">SEO Health Score</div>',
         f'<div class="label">{b["score_label"]}</div>'),
        (False, "This report presents a comprehensive SEO audit of", b["intro"]),
        # The boilerplate claims performance and visual evidence this audit
        # never gathers. Accuracy is the entire positioning; do not ship it.
        (False, "Findings combine technical, content, schema, performance, "
                "visual, and search-readiness evidence as available.",
         b["evidence_sentence"]),
        # Required. The template ships a Data Sources table naming PageSpeed
        # Insights, CrUX, CrUX History, Search Console and the URL Inspection
        # API. This audit calls none of them: with no Google API key PSI returns
        # 429 RESOURCE_EXHAUSTED, and the rest need OAuth. Listing them is a
        # claim to evidence we never gathered, in a document whose whole selling
        # point is not overreaching. It ships corrected or it does not ship.
        (True, _SUITE_SOURCES_TABLE, _sources_table()),
        (True, "Methodology based on Google Web Vitals thresholds, Search "
               "Console documentation, and Lighthouse scoring algorithms.",
         "Scored from one rendered page plus its robots.txt and sitemap. No "
         "crawl, no field performance data, no Search Console access. Every "
         "signal is presence, syntax, a count, a threshold or HTTP semantics, "
         "so the same page scores the same way twice."),
    ]

    missing_required, missing_optional = [], []
    for required, needle, replacement in rules:
        if needle not in html:
            (missing_required if required else missing_optional).append(needle)
            continue
        html = html.replace(needle, replacement)

    if missing_required:
        raise BrandingError(
            "claude-seo changed its branding markup, so this report would carry "
            "its name instead of yours. Not writing a PDF.\n"
            f"  could not find: {missing_required}\n"
            "  fix the strings in signals.json report.branding rules, "
            "then re-run.")
    if missing_optional:
        print(f"  note: {len(missing_optional)} wording rule(s) no longer match; "
              f"the report is yours but reads slightly off")
    if "Claude SEO" in html:
        raise BrandingError("'Claude SEO' still appears in the report after "
                            "rebranding; refusing to write the PDF")
    return html


def html_to_pdf(html_path: pathlib.Path, pdf_path: pathlib.Path,
                *, timeout_ms: int = 60000) -> pathlib.Path:
    """Print a local HTML file to PDF with the bundled Chromium.

    Navigates by file:// rather than set_content so the relative chart images
    google_report writes into charts/ still resolve. Blocks http and https so a
    report can never phone out while being printed, while leaving file:// free.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("http://**", lambda route, request: route.abort())
            page.route("https://**", lambda route, request: route.abort())
            page.goto(html_path.resolve().as_uri(), wait_until="load",
                      timeout=timeout_ms)
            page.pdf(path=str(pdf_path), format="A4", print_background=True,
                     margin={"top": "14mm", "bottom": "16mm",
                             "left": "12mm", "right": "12mm"})
        finally:
            browser.close()
    return pdf_path


def write_report(envelope: dict, domain: str, out_dir: pathlib.Path) -> dict:
    """Envelope on disk, HTML through google_report, PDF through Chromium."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "findings").mkdir(exist_ok=True)
    # Rerunning into the same directory must not leave a category behind that
    # this run no longer produces. A stale findings file in a client deliverable
    # is worse than a missing one.
    for stale in (out_dir / "findings").glob("*.md"):
        stale.unlink()

    data_path = out_dir / "audit-data.json"
    data_path.write_text(json.dumps(envelope, indent=2, ensure_ascii=False),
                         encoding="utf-8")

    summary, meta = envelope["summary"], envelope["bof"]
    lines = ["# Start here", "", summary["executive_summary"], ""]
    if summary.get("playbook"):
        lines += [
            "## What to do, cheapest first", "",
            "Each row assumes the ones above it are done, so the projected "
            "score is cumulative rather than a sum of the individual gains.", "",
            "| # | Fix | Effort | Projected score |",
            "|---|-----|--------|-----------------|",
        ]
        for i, p in enumerate(summary["playbook"], 1):
            lines.append(f"| {i} | {p['title']} | {p['effort']} | {p['projected']} |")
        lines.append("")
    if meta.get("platform_limited"):
        lines += [
            f"## What {meta['platform_name']} will not let you change", "",
            "These count against the score, because an assistant reading the "
            "page is affected either way, but they are not yours to fix from "
            "inside the platform:", "",
        ]
        lines += [f"- {x['title']}" for x in meta["platform_limited"]] + [""]
    (out_dir / "findings" / "00-start-here.md").write_text(
        "\n".join(lines), encoding="utf-8")

    for category in envelope["categories"]:
        slug = category["name"].lower().split("(")[0].strip().replace(" ", "-")
        score_line = ("Score: not applicable to this page"
                      if category.get("applicable") is False
                      else f"Score: {category['score']}/100")
        lines = [f"# {category['name']}", "", score_line, ""]
        if category.get("not_applicable_reason"):
            lines += [category["not_applicable_reason"], ""]
        if category.get("scope"):
            lines += ["## What this covers", "", category["scope"], ""]
        if category.get("caveat"):
            lines += [category["caveat"], ""]
        if category.get("cannot_say"):
            lines += ["## What this cannot tell you", ""]
            lines += [f"- {c}" for c in category["cannot_say"]] + [""]
        if category["what_works"]:
            lines += ["## What works", ""]
            lines += [f"- {w}" for w in category["what_works"]] + [""]
        if category["findings"]:
            lines += ["## Findings", ""]
            for f in category["findings"]:
                lines += [f"### {f['title']} ({f['severity']})", "",
                          f["description"], ""]
                if f["recommendation"]:
                    lines += [f"**Fix:** {f['recommendation']}", ""]
        (out_dir / "findings" / f"{slug}.md").write_text("\n".join(lines),
                                                         encoding="utf-8")

    result = generate_report("full", envelope, domain, str(out_dir),
                             output_format="html")
    html_files = [pathlib.Path(f) for f in (result.get("files") or [])
                  if str(f).endswith(".html")]
    if not html_files:
        return {"error": f"no HTML produced: {result.get('error')}",
                "audit_data": str(data_path)}

    html_path = html_files[0]
    html_path.write_text(
        rebrand(html_path.read_text(encoding="utf-8"), envelope, load_config()),
        encoding="utf-8")

    pdf_path = html_path.with_suffix(".pdf")
    html_to_pdf(html_path, pdf_path)
    return {"audit_data": str(data_path), "html": str(html_path),
            "pdf": str(pdf_path), "findings_dir": str(out_dir / "findings"),
            "pdf_bytes": pdf_path.stat().st_size if pdf_path.is_file() else 0}


STEPS = ["render and gather", "engine and operability", "seo baseline",
         "future readiness", "social surface", "report envelope", "pdf"]


class Cancelled(RuntimeError):
    """The run was asked to stop at a step boundary."""


def run(url: str, *, brand: Optional[str] = None, out: Optional[str] = None,
        social: bool = True, quota_budget: Optional[int] = None,
        on_step=None) -> dict:
    """``on_step(n, label)`` is called at each of the seven boundaries.

    Default prints, so the CLI is unchanged. bof.worker passes a hook that
    writes the heartbeat and raises Cancelled when the row asks it to stop.
    """
    from bof import agent_ready, future_ready, seo_core, social_surface

    domain = urlparse(url if "://" in url else f"https://{url}").netloc
    out_dir = pathlib.Path(out or ".") / f"{domain}-audit"

    def mark(n: int) -> None:
        label = STEPS[n - 1]
        if on_step is None:
            print(f"[{n}/{len(STEPS)}] {label}")
        else:
            on_step(n, label)

    # gather() and score() split explicitly rather than through audit(), because
    # the SEO baseline scores this same evidence. Rendering the page twice to
    # produce two scores is the one thing this machine cannot afford.
    mark(1)
    evidence = agent_ready.gather(url)
    if evidence.get("error"):
        return {"error": evidence["error"]}

    mark(2)
    agent = agent_ready.score(evidence)

    mark(3)
    seo_result = seo_core.score(evidence)

    mark(4)
    future = future_ready.audit(url)

    mark(5)
    social_result = None
    if social and brand:
        same_as = next((r["detail"].get("same_as", [])
                        for r in agent["signals"] if r["id"] == "C2"), [])
        social_result = social_surface.audit(brand, domain, site_same_as=same_as,
                                             quota_budget=quota_budget)

    mark(6)
    envelope = build_envelope(agent, future, social_result, domain=domain,
                              seo=seo_result, evidence=evidence)
    mark(7)
    return {**write_report(envelope, domain, out_dir), "envelope": envelope}


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def _canned_scores() -> tuple:
    # All twelve, because a real run always emits all twelve and a lens's
    # confidence is judged over its whole denominator. A four-signal stand-in
    # made the canned envelope behave in ways no real audit can.
    ratios = {"A1": 1.0, "A2": 1.0, "A3": 1.0,
              "B1": 0.1, "B2": 1.0, "B3": 0.6,
              "C1": 1.0, "C2": 0.5, "C3": 0.0,
              "D1": 1.0, "D2": 0.8, "D3": 0.0}
    titles = {"A1": "Citation crawlers allowed in robots.txt",
              "A2": "Non-browser clients get equal access",
              "A3": "Sitemap discoverable and valid",
              "B1": "Content present without JavaScript",
              "B2": "Primary content near the top of the DOM",
              "B3": "Passages sized for citation",
              "C1": "Structured data present and typed",
              "C2": "Organization identity declared with sameAs",
              "C3": "Entity anchored to Wikidata",
              "D1": "Real controls, not clickable divs",
              "D2": "Interactive elements have accessible names",
              "D3": "Form inputs are labelled"}
    weights = {"A1": 10, "A2": 8, "A3": 7, "B1": 15, "B2": 5, "B3": 5,
               "C1": 12, "C2": 6, "C3": 7, "D1": 10, "D2": 10, "D3": 5}
    # Real detail dicts, because the report now surfaces them as "what we saw"
    # and a fixture with empty details cannot exercise that path.
    details = {
        "A1": {"blocked": [], "allowed": ["OAI-SearchBot", "PerplexityBot"]},
        "A2": {"plain_status": 200, "browser_status": 200, "plain_words": 412},
        "A3": {"discovered": True, "valid": True, "loc_count": 14},
        "B1": {"raw_words": 40, "rendered_words": 412},
        "B2": {"offset_ratio": 0.08},
        "B3": {"sections": 6, "in_range": 4},
        "C1": {"nodes": 2, "types": ["Organization", "Article"]},
        "C2": {"same_as": ["https://linkedin.com/company/demo"]},
        "C3": {"declared_qid": None, "found_qid": None, "looked_up": True},
        "D1": {"real": 12, "div_onclick": 0},
        "D2": {"interactive": 14, "named": 11},
        "D3": {"fields": 3, "labelled": 0},
    }
    signals = [{"id": sid, "title": titles[sid], "weight": weights[sid],
                "ratio": ratios[sid],
                "points": round(weights[sid] * ratios[sid], 2),
                "how_measured": "canned", "detail": details[sid],
                "vacuous": False}
               for sid in titles]
    agent = {
        "score": round(sum(s["points"] for s in signals)), "band": "C",
        "url": "https://demo.test/", "scoring_version": SCORING_VERSION,
        "signals": signals}
    future = {
        "future_readiness": 0, "probes": [
            {"id": "webmcp", "title": "WebMCP tools registered in the page",
             "standard": "navigator.modelContext", "weight": 20, "ratio": 0.0,
             "points": 0.0, "status": "absent",
             "how_measured": "no navigator.modelContext usage detected", "detail": {}},
            {"id": "llmstxt", "title": "llms.txt present", "standard": "no standing",
             "weight": 0, "ratio": 0.0, "points": 0.0, "status": "absent",
             "how_measured": "no llms.txt", "detail": {}},
        ]}
    social = {
        "social_surface": 34, "coverage": 0.75, "low_confidence": False,
        "redistributed": ["youtube"],
        "platforms": [
            {"id": "youtube", "title": "YouTube presence and citability",
             "state": "unmeasured", "base_weight": 30, "weight": 0.0,
             "ratio": 0.0, "points": 0.0,
             "how_measured": "not measured (no YouTube API key configured)",
             "detail": {}, "actions": []},
            {"id": "wikidata", "title": "Wikidata entity", "state": "measured",
             "base_weight": 15, "weight": 50.0, "ratio": 0.0, "points": 0.0,
             "how_measured": "Wikidata item and its claims", "detail": {},
             "actions": ["Create a Wikidata item."]},
            {"id": "linkedin", "title": "LinkedIn company page linkage",
             "state": "measured", "base_weight": 10, "weight": 33.3, "ratio": 1.0,
             "points": 33.3, "how_measured": "declared and resolves",
             "detail": {}, "actions": []},
        ]}
    return agent, future, social


def self_check() -> int:
    cfg = load_config()
    failures = []
    agent, future, social = _canned_scores()

    env = build_envelope(agent, future, social, domain="demo.test", cfg=cfg)

    for key in ("summary", "categories", "action_plan", "artifacts"):
        if key not in env:
            failures.append(f"envelope missing {key!r}, google_report needs it")
    for key in ("health_score", "business_type", "top_findings", "quick_wins"):
        if key not in env["summary"]:
            failures.append(f"summary missing {key!r}")
    bad = [c["name"] for c in env["categories"]
           if not {"name", "score", "what_works", "findings"} <= set(c)]
    if bad:
        failures.append(f"categories missing required keys: {bad}")
    if not failures:
        print("  ok    envelope carries every key google_report reads")

    # Recorded from a run, like the fixture bands.
    eng, ops = 66, 79
    got = (env["bof"]["engine_readability"], env["bof"]["agent_operability"])
    if got != (eng, ops):
        failures.append(f"canned lens scores {got}, expected {(eng, ops)}")
    else:
        print(f"  ok    lenses reported separately ({eng} readability, {ops} operability)")
    if env["bof"]["agent_readiness"] == round(0.75 * eng + 0.33 * ops):
        failures.append("the flat score was derived by adding the lenses, which "
                        "double-counts A2's 8 points")

    expected = round(0.40 * eng + 0.35 * 34 + 0.25 * ops)
    if env["summary"]["health_score"] != expected:
        failures.append(f"headline {env['summary']['health_score']}, expected {expected}")
    else:
        print(f"  ok    headline blends the three components 40/35/25 ({expected})")

    names = [c["name"] for c in env["categories"]]
    want_names = ["AI engine readability", "AI agent operability"]
    if names[:2] != want_names:
        failures.append(f"categories lead with {names[:2]}, expected {want_names}")
    else:
        print("  ok    the two lenses are reported as separate categories")

    blind = dict(social, low_confidence=True, coverage=0.45)
    env_blind = build_envelope(agent, future, blind, domain="demo.test", cfg=cfg)
    renormalised = round((0.40 * eng + 0.25 * ops) / 0.65)  # 71
    if env_blind["summary"]["health_score"] != renormalised:
        failures.append(f"low-confidence social gave "
                        f"{env_blind['summary']['health_score']}, expected "
                        f"{renormalised} from renormalising the two lenses")
    elif "dropped out" not in env_blind["bof"]["headline_basis"]:
        failures.append("social dropped out of the headline without the report saying so")
    else:
        print(f"  ok    low-confidence social drops out and the rest "
              f"renormalise ({renormalised})")

    # A finding that only states a score is an assertion. These three lines are
    # what make it checkable, actionable and honest about who can act.
    sample = [f for c in env["categories"] for f in c["findings"]]
    for label, needle in (("what it means", "What it means:"),
                          ("the risk of ignoring it", "If you leave it:"),
                          ("the evidence behind it", "What we saw:")):
        missing = [f["title"] for f in sample if needle not in f["description"]]
        if len(missing) > len(sample) // 3:
            failures.append(f"{len(missing)} of {len(sample)} findings do not say "
                            f"{label}: {missing[:3]}")
    if not any("do not say" in f for f in failures):
        print(f"  ok    findings carry the measurement, the meaning, the risk "
              f"and the evidence ({len(sample)} findings)")

    crits = [f["title"] for f in sample if f["severity"] == "critical"]
    blockers = cfg["blockers"]["ids"]
    if len(crits) > len(blockers):
        failures.append(f"{len(crits)} critical findings but only "
                        f"{len(blockers)} signals can block: {crits}")
    else:
        print(f"  ok    critical is reserved for real blockers "
              f"({len(crits)} raised, at most {len(blockers)} possible)")

    exec_text = env["summary"]["executive_summary"]
    if str(env["summary"]["health_score"]) not in exec_text:
        failures.append("the executive summary does not quote the real headline")
    elif env["summary"].get("playbook") and not all(
            p["projected"] >= env["summary"]["health_score"]
            for p in env["summary"]["playbook"]):
        failures.append("a projected score in the playbook is below today's score")
    else:
        print("  ok    the executive summary quotes computed numbers, not constants")

    # Category findings have their underscore keys stripped, so this reads the
    # bof namespace instead: anything the platform forbids must be absent from
    # top_lifts and from the playbook, which are what promise points.
    forbidden = {x["id"] for x in env["bof"]["platform_limited"]}
    limited_with_lift = sorted(
        forbidden & ({t["id"] for t in env["bof"]["top_lifts"]}
                     | {p["id"] for p in env["summary"]["playbook"]}))
    if limited_with_lift:
        failures.append(f"platform-limited findings still promise a lift the "
                        f"owner cannot collect: {limited_with_lift}")
    else:
        print("  ok    a fix the platform forbids never promises headline points")

    rows = env["bof"]["headline_basis_rows"]
    summed = round(sum(r["contribution"] for r in rows))
    if abs(summed - env["summary"]["health_score"]) > 1:
        failures.append(f"basis rows sum to {summed}, headline is "
                        f"{env['summary']['health_score']}")
    else:
        print(f"  ok    the headline is the sum of its published basis "
              f"({len(rows)} rows)")

    ownerless = [c["name"] for c in env["categories"][:2] if not c.get("owner")]
    if ownerless:
        failures.append(f"lens categories with no owner to route them to: {ownerless}")
    else:
        owners = ", ".join(f"{c['name']} -> {c['owner']}" for c in env["categories"][:2])
        print(f"  ok    lens findings are routable ({owners})")

    # A page with no controls, no accessible names and no form fields has
    # nothing for D1, D2 or D3 to judge. Absent is not correct: they drop out,
    # leaving A2 alone, which is too little of the lens to carry a number.
    hollow = dict(agent, signals=[dict(r, ratio=1.0, points=float(r["weight"]),
                                       not_applicable=r["id"].startswith("D"))
                                  for r in agent["signals"]])
    hollow.pop("lenses", None)
    env_hollow = build_envelope(hollow, future, None, domain="hollow.test", cfg=cfg)
    voted = [r["component"] for r in env_hollow["bof"]["headline_basis_rows"]]
    ops = env_hollow["bof"]["agent_operability"]
    if "agent_operability" in voted:
        failures.append("an operability lens with nothing to measure still "
                        "voted on the headline")
    elif ops is not None:
        failures.append(f"a lens with nothing to measure reported {ops} rather "
                        f"than no score; absent is not the same as perfect")
    elif "does not apply" not in env_hollow["bof"]["headline_basis"]:
        failures.append("a lens dropped out without the report saying so")
    else:
        print("  ok    a lens with nothing to measure reports no score, not 100")

    # Lift is a promise about the headline. Sorting by it alone puts months of
    # outside-party work above a ten-minute fix, because the months pay more.
    effort = cfg["effort"]
    ranks = effort["_ranks"]
    out_of_order = []
    for phase in env["action_plan"]["phases"]:
        bands = [ranks.get(item.split("(")[-1].split(")")[0], 1)
                 for item in phase["items"]]
        if bands != sorted(bands):
            out_of_order.append((phase["name"], bands))
    if out_of_order:
        failures.append(f"a phase lists a costlier fix before a cheaper one: "
                        f"{out_of_order}")
    else:
        print("  ok    every phase lists quick fixes before projects and campaigns")

    # The canned run happens to contain no campaign item, so assert the ordering
    # against a set that spans all three bands rather than against luck. The
    # campaign here carries the highest lift, which is exactly the case
    # lift-only sorting got wrong.
    spanning = [{"_id": i, "_lift": lift, "severity": "critical", "title": i,
                 "recommendation": "x", "description": "x"}
                for i, lift in [("C3", 2.0), ("wikipedia", 9.0), ("A1", 1.0),
                                ("A3", 4.0)]]
    ordered = sorted(spanning, key=lambda f: (ranks[effort[f["_id"]]],
                                              -f["_lift"]))
    bands = [ranks[effort[f["_id"]]] for f in ordered]
    if bands != sorted(bands):
        failures.append(f"three-band ordering is wrong: "
                        f"{[(f['_id'], effort[f['_id']]) for f in ordered]}")
    elif ordered[0]["_id"] != "A3":
        failures.append(f"the highest-lift quick fix is not first: "
                        f"{ordered[0]['_id']}")
    elif ordered[-1]["_id"] != "wikipedia":
        failures.append("the highest-lift campaign did not sort last")
    else:
        print("  ok    a 9-point campaign sorts below a 4-point quick fix "
              "(quick, quick, project, campaign)")

    # Taking one signal to full marks must move the headline by exactly the lift
    # the report printed against it, or the number is decoration.
    target = "B1"
    row = next(r for r in agent["signals"] if r["id"] == target)
    blend = cfg["report"]["headline_weights"]
    lenses_now = lens_scores(agent["signals"], cfg)
    predicted = _headline_lift(target, row["ratio"], lenses_now, blend,
                               sum(blend.values()), cfg["weights"])
    if not predicted:
        failures.append(f"{target} promised no lift at all")
    fixed = dict(agent, signals=[dict(r, ratio=1.0, points=float(r["weight"]))
                                 if r["id"] == target else r
                                 for r in agent["signals"]])
    fixed.pop("lenses", None)
    env_fixed = build_envelope(fixed, future, social, domain="demo.test", cfg=cfg)
    moved = env_fixed["summary"]["health_score"] - env["summary"]["health_score"]
    if abs(moved - predicted) > 1:
        failures.append(f"{target} promised {predicted:.1f} points and delivered {moved}")
    else:
        print(f"  ok    {target}'s promised lift is what fixing it pays "
              f"({predicted:.1f} predicted, {moved} delivered)")

    # The assertion the whole product is positioned on. Every competitor sells an
    # SEO audit with a GEO section bolted on; if this check ever fails, this has
    # become one of them.
    def _seo(n):
        return {"seo": n, "band": "A" if n else "F", "seo_field": None,
                "seo_field_reason": "no Google API key", "scope": "test",
                "cannot_say": [], "performance_note": "",
                "unmeasured": [],
                "signals": [{"id": "E1", "title": "Indexable", "weight": 22,
                             "ratio": n / 100, "points": 22 * n / 100,
                             "how_measured": "canned", "detail": {}}]}

    perfect = build_envelope(agent, future, social, domain="demo.test", cfg=cfg,
                             seo=_seo(100))
    absent = build_envelope(agent, future, social, domain="demo.test", cfg=cfg,
                            seo=_seo(0))
    moved = [k for k in ("health_score",)
             if perfect["summary"][k] != absent["summary"][k]]
    if moved:
        failures.append(f"SEO moved the AI Visibility Score: {moved}. "
                        f"{perfect['summary']['health_score']} with perfect SEO, "
                        f"{absent['summary']['health_score']} with none")
    elif perfect["bof"]["headline_basis_rows"] != absent["bof"]["headline_basis_rows"]:
        failures.append("SEO changed the headline's basis rows")
    elif env["summary"]["health_score"] != perfect["summary"]["health_score"]:
        failures.append("adding an SEO category at all moved the headline")
    else:
        print(f"  ok    SEO cannot move the AI Visibility Score "
              f"(100 and 0 both give {perfect['summary']['health_score']})")

    names = [c["name"] for c in perfect["categories"]]
    if not names[-2].startswith("SEO"):
        failures.append(f"SEO is not reported as a secondary panel: {names}")
    else:
        print("  ok    SEO reports after the lenses and the social surface")

    # The product is AI visibility. A site with nothing an assistant or an agent
    # can use must score badly, whatever else is true of it.
    dead = {"score": 0, "band": "F", "url": "https://dead.test/",
            "scoring_version": SCORING_VERSION,
            "signals": [dict(r, ratio=0.0, points=0.0) for r in agent["signals"]]}
    env_dead = build_envelope(dead, future, None, domain="dead.test", cfg=cfg)
    if env_dead["summary"]["health_score"] != 0:
        failures.append(f"zero AI visibility scored "
                        f"{env_dead['summary']['health_score']}, expected 0")
    else:
        print("  ok    zero AI visibility scores 0, whatever else is true")

    future_sev = {f["severity"] for c in env["categories"]
                  if c["name"].startswith("Future") for f in c["findings"]}
    if future_sev - {"info"}:
        failures.append(f"future readiness raised non-info findings: {future_sev}")
    else:
        print("  ok    future readiness never raises a critical or warning")

    llms = [f for c in env["categories"] for f in c["findings"]
            if "llms.txt" in f["title"]]
    if llms:
        failures.append("llms.txt produced a finding despite carrying weight 0")
    else:
        print("  ok    llms.txt raises no finding")

    unfixable = [f"{c['name']}/{f['title']}" for c in env["categories"]
                 for f in c["findings"] if not f["recommendation"].strip()]
    if unfixable:
        failures.append(f"findings with no recommendation: {unfixable}")
    else:
        print("  ok    every finding carries a recommendation")

    if not env["action_plan"]["phases"]:
        failures.append("action plan has no phases")
    else:
        print(f"  ok    action plan built {len(env['action_plan']['phases'])} phase(s)")

    # An unmeasured platform scored zero would otherwise read as critical, which
    # blames the client for our missing API key.
    all_findings = [f for c in env["categories"] for f in c["findings"]]
    blamed = [f["title"] for f in all_findings
              if "not measured" in f["description"] and f["severity"] != "info"]
    if blamed:
        failures.append(f"unmeasured platforms raised non-info findings: {blamed}")
    else:
        print("  ok    unmeasured platforms never raise a critical finding")

    if not any(f["title"] == "Measurement coverage" for f in all_findings):
        failures.append("redistribution happened but was not disclosed in findings")
    else:
        print("  ok    redistribution is disclosed in the report body")

    tops = env["summary"]["top_findings"]
    mislabelled = [t for t in tops
                   if not isinstance(t, dict)
                   or t.get("severity") not in ("Critical", "Warning")]
    if mislabelled:
        failures.append(f"top findings would render as 'Info:' regardless of "
                        f"severity: {mislabelled}")
    else:
        print(f"  ok    top findings carry their real severity ({len(tops)} shown)")

    # The worker's whole view of progress is this hook, and the store's
    # step_total is 7. If the two ever disagree the UI shows a bar that stops
    # at 6 of 7 forever.
    from bof import store
    if len(STEPS) != store.STEP_TOTAL:
        failures.append(f"{len(STEPS)} step labels against store.STEP_TOTAL "
                        f"{store.STEP_TOTAL}")
    else:
        print(f"  ok    the progress hook and the run store agree on "
              f"{len(STEPS)} steps")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bof-report-"))
    try:
        out = write_report(env, "demo.test", tmp)
        if out.get("error"):
            failures.append(out["error"])
        else:
            pdf = pathlib.Path(out["pdf"])
            head = pdf.read_bytes()[:5] if pdf.is_file() else b""
            if head != b"%PDF-":
                failures.append(f"output is not a PDF (starts {head!r})")
            elif out["pdf_bytes"] <= 20480:
                failures.append(f"PDF is {out['pdf_bytes']} bytes, expected over 20 KB")
            else:
                print(f"  ok    PDF written, {out['pdf_bytes'] // 1024} KB, valid header")
            if not pathlib.Path(out["findings_dir"]).glob("*.md"):
                failures.append("no per-category findings markdown written")
            else:
                print("  ok    per-category findings markdown written")

            html = pathlib.Path(out["html"]).read_text(encoding="utf-8")
            leaked = [s for s in ("Claude SEO", "Google SEO Intelligence",
                                  "SEO Health Score") if s in html]
            if leaked:
                failures.append(f"third-party branding left in the deliverable: {leaked}")
            elif cfg["report"]["branding"]["author"] not in html:
                failures.append("our own byline is missing from the deliverable")
            else:
                print("  ok    deliverable carries our branding, not the suite's")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll report self-checks passed.")
    return 0


# --------------------------------------------------------------------------

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", nargs="?")
    ap.add_argument("--brand", help="brand name; enables the social surface")
    ap.add_argument("--out", default=".", help="directory to write <domain>-audit into")
    ap.add_argument("--no-social", action="store_true")
    ap.add_argument("--quota-budget", type=int)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)

    if args.self_check:
        return self_check()
    if not args.url:
        ap.error("a url is required unless --self-check is given")

    out = run(args.url, brand=args.brand, out=args.out,
              social=not args.no_social, quota_budget=args.quota_budget)
    if out.get("error"):
        print(f"error: {out['error']}")
        return 1

    bof_meta = out["envelope"]["bof"]
    print(f"\nheadline {out['envelope']['summary']['health_score']}/100 "
          f"({bof_meta['headline_basis']})")
    print(f"  engine readability {bof_meta['engine_readability']}/100 band {bof_meta['engine_band']}")
    print(f"  agent operability  {bof_meta['agent_operability']}/100 band {bof_meta['operability_band']}")
    if bof_meta["social_surface"] is not None:
        print(f"  social surface     {bof_meta['social_surface']}/100 "
              f"({bof_meta['social_coverage']:.0%} measured)")
    print(f"  future readiness   {bof_meta['future_readiness']}/100 (opportunity only)")
    if bof_meta["seo"] is not None:
        print(f"  seo baseline       {bof_meta['seo']}/100 band {bof_meta['seo_band']} "
              f"(supporting evidence, not in the headline)")
        print(f"  seo field data     {bof_meta['seo_field_reason']}")
    print(f"\n  {out['pdf']}  ({out['pdf_bytes'] // 1024} KB)")
    print(f"  {out['audit_data']}")
    print(f"  {out['findings_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
