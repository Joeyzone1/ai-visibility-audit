# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Future readiness: agent surfaces this site has published ahead of the curve.

    python -m bof.future_ready https://example.com [--json] [--no-browser]
    python -m bof.future_ready --self-check

Everything here is draft or early-adoption, so **absence is never a failure**.
This score is additive only and can never reduce the agent-readiness score in
``agent_ready.py``. It answers a different question: how far ahead of the
market is this site, and what could it publish that nobody else has yet.

That posture is copied from the suite's own ``ucp_check.py``, which reports a
missing profile as a forward-looking opportunity and exits 0 either way.

llms.txt is probed and reported at weight **zero**. Google states Search
ignores it. The competing open-source scorer assigns it 18 of 100. Carrying it
at zero with the evidence attached is the point: it is an exhibit, not a signal.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from typing import Optional
from urllib.parse import quote, urljoin, urlparse

from bof import SCORING_VERSION
from bof import suite  # noqa: F401  # must precede every suite import
from bof.agent_ready import load_config

from ucp_check import audit_site  # noqa: E402
from url_safety import URLSafetyError, safe_requests_get, validate_url_strict  # noqa: E402

PRESENT, ABSENT, ERROR = "present", "absent", "error"
STATUSES = frozenset({PRESENT, ABSENT, ERROR})

#: Installed before any page script. Records WebMCP registrations that the page
#: makes, whether or not the browser ships the API natively.
_WEBMCP_INIT = """
(() => {
  const rec = {tools: [], contexts: 0, stubbed: false, native: false};
  window.__bofWebMCP = rec;
  const name = (t) => (t && (t.name || t.id)) ? String(t.name || t.id) : 'unnamed';
  const shim = {
    registerTool(t) { rec.tools.push(name(t)); return Promise.resolve(); },
    unregisterTool() { return Promise.resolve(); },
    provideContext(c) {
      rec.contexts += 1;
      if (c && Array.isArray(c.tools)) c.tools.forEach(t => rec.tools.push(name(t)));
      return Promise.resolve();
    }
  };
  if ('modelContext' in navigator) { rec.native = true; return; }
  try {
    Object.defineProperty(navigator, 'modelContext',
                          {value: shim, configurable: true, writable: true});
    rec.stubbed = true;
  } catch (e) { rec.error = String(e); }
})();
"""


def _root(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _fetch_json(url: str, timeout: int) -> dict:
    """GET a URL and try to read it as JSON. Never raises."""
    out = {"url": url, "status_code": None, "json": None, "valid_json": False,
           "error": None, "bytes": 0, "content_type": None, "text_head": ""}
    try:
        resp = safe_requests_get(url, timeout=timeout,
                                 headers={"User-Agent": "bof-audit/0.1"})
        out["status_code"] = resp.status_code
        out["bytes"] = len(resp.content or b"")
        out["content_type"] = resp.headers.get("Content-Type")
        if resp.status_code == 200:
            out["text_head"] = (resp.text or "")[:400]
            try:
                out["json"] = resp.json()
                out["valid_json"] = True
            except ValueError:
                out["error"] = "200 but body is not JSON"
    except Exception as exc:  # noqa: BLE001 - an unreachable probe is a result
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _is_soft_404(hit: dict) -> bool:
    """200 with an HTML body at a machine-readable path is a catch-all route.

    Single-page apps and Next.js sites answer every unknown path with the app
    shell. Treating that as a broken manifest would put an error on three rows
    of nearly every modern client's report. It is an ordinary absence.
    """
    ctype = (hit.get("content_type") or "").lower()
    head = (hit.get("text_head") or "").lstrip()[:200].lower()
    return ("html" in ctype
            or head.startswith("<!doctype html")
            or head.startswith("<html"))


def _dns_txt(name: str, timeout: int = 8) -> dict:
    """TXT lookup via nslookup.

    dnspython is not in the suite venv, and installing it would change
    requirements.txt and force a full claude-seo re-setup. nslookup ships with
    Windows and every mainstream Linux net-tools install.

    ponytail: subprocess + substring match. Swap for dnspython if DNS-based MCP
    discovery ever leaves draft and a client actually depends on it.
    """
    out = {"name": name, "records": [], "error": None}
    try:
        proc = subprocess.run(["nslookup", "-type=TXT", name],
                              capture_output=True, text=True, timeout=timeout)
        for line in proc.stdout.splitlines():
            if "text =" in line or '"' in line and "MCP" in line.upper():
                out["records"].append(line.strip())
    except Exception as exc:  # noqa: BLE001 - no DNS just means we use the path probe
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# --------------------------------------------------------------------------
# probes. Each returns {status, ratio, how, detail}.
# --------------------------------------------------------------------------

def _score_wellknown_json(hit: dict, expected: list) -> tuple:
    """Shared shape for the three MCP discovery documents."""
    if hit["error"] and hit["status_code"] is None:
        return ERROR, 0.0, f"could not reach the document ({hit['error']})"
    if hit["status_code"] != 200:
        return ABSENT, 0.0, f"no document served (HTTP {hit['status_code']})"
    if _is_soft_404(hit):
        return ABSENT, 0.0, ("the origin answers this path with its HTML app "
                             "shell, a catch-all route rather than a manifest")
    if not hit["valid_json"]:
        return ERROR, 0.0, "served a document but it is not valid JSON"
    body = hit["json"]
    keys = set(body) if isinstance(body, dict) else set()
    matched = sorted(keys & set(expected))
    if matched:
        return PRESENT, 1.0, f"valid JSON carrying {', '.join(matched)}"
    return PRESENT, 0.5, "valid JSON, but none of the expected keys are present"


def probe_wellknown(pid: str, ev: dict, cfg: dict) -> dict:
    hit = (ev.get("fetches") or {}).get(pid) or {}
    expected = cfg["expected_keys"].get(pid, [])
    status, ratio, how = _score_wellknown_json(hit, expected)
    detail = {k: hit.get(k) for k in ("url", "status_code", "valid_json", "error")}
    if pid == "mcp_uri_discovery":
        dns = ev.get("dns_txt") or {}
        detail["dns_txt"] = dns
        if status != PRESENT and dns.get("records"):
            status, ratio = PRESENT, 1.0
            how = f"advertised by DNS TXT at {dns.get('name')}"
    return {"status": status, "ratio": ratio, "how": how, "detail": detail}


def probe_webmcp(ev: dict, cfg: dict) -> dict:
    w = ev.get("webmcp") or {}
    if w.get("error"):
        return {"status": ERROR, "ratio": 0.0,
                "how": f"could not inspect the page ({w['error']})", "detail": w}
    if not w.get("checked"):
        return {"status": ERROR, "ratio": 0.0,
                "how": "browser inspection skipped", "detail": w}
    tools = w.get("tools") or []
    if tools:
        return {"status": PRESENT, "ratio": 1.0,
                "how": f"page registered {len(tools)} WebMCP tool(s)", "detail": w}
    if w.get("native") or w.get("contexts"):
        return {"status": PRESENT, "ratio": 0.5,
                "how": "the WebMCP API was touched but no tools were registered",
                "detail": w}
    if w.get("mentions_in_source"):
        return {"status": PRESENT, "ratio": 0.5,
                "how": "modelContext appears in page source but registered no tools",
                "detail": w}
    return {"status": ABSENT, "ratio": 0.0,
            "how": "no navigator.modelContext usage detected", "detail": w}


def probe_nlweb(ev: dict, cfg: dict) -> dict:
    hit = (ev.get("fetches") or {}).get("nlweb") or {}
    if hit.get("error") and hit.get("status_code") is None:
        return {"status": ERROR, "ratio": 0.0,
                "how": f"could not reach the ask endpoint ({hit['error']})",
                "detail": hit}
    if hit.get("status_code") != 200:
        return {"status": ABSENT, "ratio": 0.0,
                "how": f"no ask endpoint (HTTP {hit.get('status_code')})",
                "detail": {k: hit.get(k) for k in ("url", "status_code")}}
    if not hit.get("valid_json"):
        return {"status": ABSENT, "ratio": 0.0,
                "how": "/ask answered but not with JSON, so it is an ordinary page",
                "detail": {k: hit.get(k) for k in ("url", "status_code")}}
    body = json.dumps(hit.get("json"))[:4000]
    schema_ish = "@type" in body or "schema.org" in body
    return {"status": PRESENT,
            "ratio": 1.0 if schema_ish else 0.5,
            "how": ("ask endpoint answers with schema.org JSON" if schema_ish
                    else "ask endpoint answers with JSON but no schema.org vocabulary"),
            "detail": {"url": hit.get("url"), "schema_org_vocabulary": schema_ish}}


def probe_ucp(ev: dict, cfg: dict) -> dict:
    ucp = ev.get("ucp") or {}
    if ucp.get("unavailable"):
        return {"status": ERROR, "ratio": 0.0,
                "how": f"UCP check unavailable ({ucp.get('unavailable')})",
                "detail": ucp}
    if not ucp.get("profile_present"):
        return {"status": ABSENT, "ratio": 0.0,
                "how": "no /.well-known/ucp profile", "detail": ucp}
    parse = ucp.get("parse") or {}
    caps = parse.get("capabilities") or []
    return {"status": PRESENT,
            "ratio": 1.0 if (parse.get("valid_json") and caps) else 0.5,
            "how": f"UCP profile with {len(caps)} declared capability(ies)",
            "detail": {"version": parse.get("version"),
                       "capabilities": [c.get("id") for c in caps][:10],
                       "issues": parse.get("issues")}}


def probe_llmstxt(ev: dict, cfg: dict) -> dict:
    hit = (ev.get("fetches") or {}).get("llmstxt") or {}
    note = cfg["llmstxt_note"]
    if hit.get("status_code") == 200 and _is_soft_404(hit):
        return {"status": ABSENT, "ratio": 0.0,
                "how": "the origin answers /llms.txt with its HTML app shell",
                "detail": {"url": hit.get("url"), "note": note}}
    if hit.get("status_code") == 200 and hit.get("bytes"):
        return {"status": PRESENT, "ratio": 1.0,
                "how": "llms.txt is served (scored at zero, see note)",
                "detail": {"url": hit.get("url"), "bytes": hit.get("bytes"),
                           "note": note}}
    if hit.get("error") and hit.get("status_code") is None:
        return {"status": ERROR, "ratio": 0.0,
                "how": f"could not reach /llms.txt ({hit['error']})",
                "detail": {"note": note}}
    return {"status": ABSENT, "ratio": 0.0,
            "how": "no llms.txt (scored at zero, so this costs nothing)",
            "detail": {"note": note}}


PROBES = [
    ("mcp_server_card", lambda ev, cfg: probe_wellknown("mcp_server_card", ev, cfg)),
    ("mcp_manifest", lambda ev, cfg: probe_wellknown("mcp_manifest", ev, cfg)),
    ("mcp_uri_discovery", lambda ev, cfg: probe_wellknown("mcp_uri_discovery", ev, cfg)),
    ("webmcp", probe_webmcp),
    ("nlweb", probe_nlweb),
    ("ucp", probe_ucp),
    ("llmstxt", probe_llmstxt),
]


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def score(evidence: dict, cfg: Optional[dict] = None) -> dict:
    """Pure, additive, and never negative."""
    cfg = cfg or load_config()["future_ready"]
    weights, titles = cfg["weights"], cfg["titles"]
    assert sum(weights.values()) == 100, f"weights sum to {sum(weights.values())}"

    rows = []
    for pid, fn in PROBES:
        out = fn(evidence, cfg)
        assert out["status"] in STATUSES, f"{pid} returned status {out['status']!r}"
        ratio = max(0.0, min(1.0, float(out["ratio"])))
        rows.append({
            "id": pid,
            "title": titles[pid],
            "standard": cfg["standards"][pid],
            "weight": weights[pid],
            "status": out["status"],
            "ratio": round(ratio, 4),
            "points": round(weights[pid] * ratio, 2),
            "how_measured": out["how"],
            "detail": out.get("detail", {}),
        })

    total = round(sum(r["points"] for r in rows))
    published = [r["id"] for r in rows if r["status"] == PRESENT and r["weight"]]
    return {
        "url": evidence.get("url"),
        "scoring_version": SCORING_VERSION,
        "future_readiness": total,
        "posture": "opportunity",
        "published_surfaces": published,
        "probes": rows,
        "errors": [r["id"] for r in rows if r["status"] == ERROR],
    }


# --------------------------------------------------------------------------
# gathering
# --------------------------------------------------------------------------

def inspect_webmcp(url: str, *, timeout_ms: int = 30000) -> dict:
    """Navigate with a recorder installed and report what the page registered."""
    out = {"checked": False, "tools": [], "contexts": 0, "native": False,
           "stubbed": False, "mentions_in_source": False, "error": None}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        out["error"] = "playwright unavailable"
        return out

    # ponytail: its own browser launch. P4 can share one session across the
    # aria snapshot and this if audit wall-clock ever matters; sequential
    # launches are the safe choice on a machine with ~1.8 GB free.
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.add_init_script(_WEBMCP_INIT)
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(1500)  # let registration run after load
                rec = page.evaluate("window.__bofWebMCP || null") or {}
                out.update({k: rec.get(k, out[k]) for k in
                            ("tools", "contexts", "native", "stubbed")})
                out["mentions_in_source"] = "modelContext" in (page.content() or "")
                out["checked"] = True
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def gather(url: str, *, timeout: int = 15, browser: bool = True) -> dict:
    cfg = load_config()["future_ready"]
    try:
        normalised, _ip = validate_url_strict(url)
    except (URLSafetyError, ValueError) as exc:
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}

    root = _root(normalised)
    paths = cfg["paths"]
    fetches = {pid: _fetch_json(urljoin(root, path), timeout)
               for pid, path in paths.items() if pid != "nlweb"}
    fetches["nlweb"] = _fetch_json(
        urljoin(root, paths["nlweb"]) + "?query=what+is+this+site&streaming=false",
        timeout)

    try:
        ucp = audit_site(root, probe_endpoints=False, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        ucp = {"unavailable": f"{type(exc).__name__}: {exc}"}

    return {
        "url": normalised,
        "fetches": fetches,
        "dns_txt": _dns_txt(f"_mcp.{urlparse(normalised).netloc}"),
        "ucp": ucp,
        "webmcp": (inspect_webmcp(normalised) if browser
                   else {"checked": False, "error": "browser inspection disabled"}),
        "error": None,
    }


def audit(url: str, **kwargs) -> dict:
    ev = gather(url, **kwargs)
    if ev.get("error"):
        return {"url": url, "future_readiness": 0, "probes": [],
                "published_surfaces": [], "errors": [], "error": ev["error"],
                "scoring_version": SCORING_VERSION}
    return score(ev)


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def _absent_evidence() -> dict:
    """Every probe absent. The floor the monotonicity check builds on."""
    return {
        "url": "https://absent.test/",
        "fetches": {pid: {"url": f"https://absent.test{p}", "status_code": 404,
                          "json": None, "valid_json": False, "error": None,
                          "bytes": 0}
                    for pid, p in load_config()["future_ready"]["paths"].items()},
        "dns_txt": {"name": "_mcp.absent.test", "records": [], "error": None},
        "ucp": {"profile_present": False, "parse": {}},
        "webmcp": {"checked": True, "tools": [], "contexts": 0, "native": False,
                   "stubbed": True, "mentions_in_source": False, "error": None},
    }


def _make_present(ev: dict, pid: str) -> dict:
    """Flip exactly one probe to a fully present state."""
    ev = json.loads(json.dumps(ev))
    if pid in ("mcp_server_card", "mcp_manifest", "mcp_uri_discovery"):
        ev["fetches"][pid].update(status_code=200, valid_json=True,
                                  json={"name": "demo", "endpoint": "https://x/mcp",
                                        "capabilities": {}})
    elif pid == "webmcp":
        ev["webmcp"].update(tools=["search_products", "book_appointment"])
    elif pid == "nlweb":
        ev["fetches"]["nlweb"].update(
            status_code=200, valid_json=True,
            json={"results": [{"@type": "Thing", "name": "demo"}]})
    elif pid == "ucp":
        ev["ucp"] = {"profile_present": True,
                     "parse": {"valid_json": True, "version": "0.1",
                               "capabilities": [{"id": "dev.ucp.shopping.cart"}],
                               "issues": []}}
    elif pid == "llmstxt":
        ev["fetches"]["llmstxt"].update(status_code=200, bytes=512)
    return ev


def self_check() -> int:
    cfg = load_config()["future_ready"]
    failures = []

    total_weight = sum(cfg["weights"].values())
    if total_weight != 100:
        failures.append(f"weights sum to {total_weight}, not 100")
    else:
        print("  ok    weights sum to 100")

    if cfg["weights"].get("llmstxt") != 0:
        failures.append(f"llms.txt weight is {cfg['weights'].get('llmstxt')}, must be 0")
    else:
        print("  ok    llms.txt carries weight 0")

    if set(cfg["weights"]) != {pid for pid, _ in PROBES}:
        failures.append("signals.json weights and the PROBES registry disagree")
    else:
        print("  ok    every probe has a weight")

    base_ev = _absent_evidence()
    base = score(base_ev, cfg)
    if base["future_readiness"] != 0:
        failures.append(f"all-absent evidence scored {base['future_readiness']}, not 0")
    else:
        print("  ok    all-absent evidence scores 0")

    bad = [r["id"] for r in base["probes"] if r["status"] not in STATUSES]
    if bad:
        failures.append(f"probes returned a status outside {sorted(STATUSES)}: {bad}")
    else:
        print(f"  ok    every probe reports one of {sorted(STATUSES)}")

    # Opportunity can never subtract: flipping any single probe on must not
    # lower the total, and flipping all of them on must reach exactly 100.
    regressions = []
    for pid, _ in PROBES:
        got = score(_make_present(base_ev, pid), cfg)["future_readiness"]
        if got < base["future_readiness"]:
            regressions.append(f"{pid} lowered the total to {got}")
    if regressions:
        failures.append("; ".join(regressions))
    else:
        print("  ok    no probe can lower the total (monotonic)")

    everything = base_ev
    for pid, _ in PROBES:
        everything = _make_present(everything, pid)
    full = score(everything, cfg)
    if full["future_readiness"] != 100:
        failures.append(f"all-present evidence scored {full['future_readiness']}, not 100")
    else:
        print("  ok    all-present evidence scores 100")

    # A catch-all route answering 200 with the app shell must read as absent,
    # not as a broken manifest. Real case: docs sites on Next.js.
    soft = json.loads(json.dumps(base_ev))
    for pid in ("mcp_server_card", "mcp_manifest", "mcp_uri_discovery", "llmstxt"):
        soft["fetches"][pid].update(
            status_code=200, bytes=48000, valid_json=False,
            content_type="text/html; charset=utf-8",
            text_head='<!DOCTYPE html><html lang="en"><head><title>Docs</title>')
    soft_rows = {r["id"]: r["status"] for r in score(soft, cfg)["probes"]}
    wrong = {pid: st for pid, st in soft_rows.items()
             if pid in ("mcp_server_card", "mcp_manifest", "mcp_uri_discovery",
                        "llmstxt") and st != ABSENT}
    if wrong:
        failures.append(f"soft-404 HTML read as something other than absent: {wrong}")
    else:
        print("  ok    HTML catch-all routes read as absent, not error")

    once, twice = score(everything, cfg), score(everything, cfg)
    if json.dumps(once, sort_keys=True) != json.dumps(twice, sort_keys=True):
        failures.append("two scorings of the same evidence differ")
    else:
        print("  ok    scoring is deterministic")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll future-readiness self-checks passed.")
    return 0


# --------------------------------------------------------------------------

def browser_check() -> int:
    """Positive control for the WebMCP recorder. Needs Chromium, no network.

    The live probe only ever reports absence until some site actually ships
    WebMCP, so without this the 20 points behind it would be untested. Serves a
    page that registers two tools and asserts we catch both.
    """
    page = ("<html><body><script>"
            "navigator.modelContext.provideContext({tools:["
            "{name:'search_docs'},{name:'book_demo'}]});"
            "navigator.modelContext.registerTool({name:'check_stock'});"
            "</script></body></html>")
    got = inspect_webmcp("data:text/html," + quote(page))

    failures = []
    if not got.get("checked"):
        failures.append(f"recorder never ran: {got.get('error')}")
    if sorted(got.get("tools") or []) != ["book_demo", "check_stock", "search_docs"]:
        failures.append(f"registered tools not captured: {got.get('tools')}")
    else:
        print("  ok    recorder captured 3 registered tools")

    row = probe_webmcp({"webmcp": got}, load_config()["future_ready"])
    if row["status"] != PRESENT or row["ratio"] != 1.0:
        failures.append(f"a page with tools scored {row['status']} at {row['ratio']}")
    else:
        print("  ok    a page registering tools scores present at full weight")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nWebMCP positive control passed.")
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-browser", action="store_true",
                    help="skip the WebMCP page inspection")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--browser-check", action="store_true",
                    help="positive control for the WebMCP recorder (needs Chromium)")
    args = ap.parse_args(argv)

    if args.browser_check:
        return browser_check()
    if args.self_check:
        return self_check()
    if not args.url:
        ap.error("a url is required unless --self-check is given")

    result = audit(args.url, timeout=args.timeout, browser=not args.no_browser)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if result.get("error"):
        print(f"error: {result['error']}")
        return 1

    print(result["url"])
    print(f"future readiness {result['future_readiness']}/100 "
          f"(opportunity only, never subtracts from agent readiness)")
    for row in result["probes"]:
        flag = {PRESENT: "+", ABSENT: ".", ERROR: "?"}[row["status"]]
        print(f"  {flag} {row['points']:>5.1f}/{row['weight']:<3} {row['title']}")
        print(f"      {row['standard']}")
        print(f"      {row['how_measured']}")
    if not result["published_surfaces"]:
        print("\n  No agent surfaces published yet. Every line above is an "
              "opportunity, not a fault.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
