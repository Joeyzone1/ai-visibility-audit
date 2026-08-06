# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""Upgrade tripwire for the claude-seo coupling. Run after any suite upgrade.

    python -m bof.test_suite_contract            # includes network checks
    python -m bof.test_suite_contract --offline  # skips network and browser

Asserts every suite callable bof depends on still exists, still accepts the
arguments bof passes, and still returns the shape bof reads. If this passes on
a new suite version, add that version to TESTED_VERSIONS in bof/suite.py.

Runs every check and reports all failures, rather than stopping at the first:
on an upgrade you want the whole list, not one line of it.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys

# The whole point of this file is to run against untested versions.
os.environ.setdefault("BOF_ALLOW_UNTESTED_SUITE", "1")

from bof import suite  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def _binds(fn, *args, **kwargs) -> None:
    """Assert fn accepts these arguments, without calling it."""
    inspect.signature(fn).bind(*args, **kwargs)


@check
def paths_exist():
    assert suite.SCRIPTS.is_dir(), f"missing scripts dir: {suite.SCRIPTS}"
    assert suite.BROWSERS.is_dir(), f"missing bundled browsers: {suite.BROWSERS}"
    chromium = list(suite.BROWSERS.glob("chromium*"))
    assert chromium, f"no chromium* under {suite.BROWSERS}; run claude-seo setup"


@check
def playwright_path_is_pinned():
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(suite.BROWSERS), (
        "PLAYWRIGHT_BROWSERS_PATH was already set to something else; "
        "renders will look for a browser bof did not install"
    )


@check
def dependencies_exist():
    missing = []
    for module_name, names in suite.DEPENDENCIES.items():
        mod = importlib.import_module(module_name)
        for name in names:
            fn = getattr(mod, name, None)
            if not callable(fn):
                missing.append(f"{module_name}.{name}")
    assert not missing, f"gone or not callable: {missing}"


@check
def signatures_accept_our_arguments():
    from agent_ux_check import analyze_accessibility_tree, analyze_html
    from google_report import generate_report
    from parse_html import parse_html
    from render_page import render_page
    from ucp_check import audit_site
    from url_safety import (make_safe_playwright_route_handler,
                            safe_requests_get, validate_url_strict)

    _binds(render_page, "https://example.com", mode="always", viewport="desktop",
           timeout_ms=20000, block_resources=["image"], extract_content=True,
           extract_accessibility=True)
    _binds(parse_html, "<html></html>", base_url="https://example.com")
    _binds(validate_url_strict, "https://example.com")
    _binds(safe_requests_get, "https://example.com", timeout=10)
    _binds(make_safe_playwright_route_handler, blocked_resource_types={"image"})
    _binds(audit_site, "https://example.com", probe_endpoints=False, timeout=10)
    _binds(analyze_html, "<html></html>")
    _binds(analyze_accessibility_tree, {"role": "WebArea"})
    _binds(generate_report, "full", {}, "example.com", ".", output_format="pdf")

    from content_quality import analyse as content_analyse
    from preload_check import analyse as preload_analyse
    _binds(content_analyse, "some text")
    _binds(preload_analyse, "<html></html>", {"content-type": "text/html"})


@check
def seo_helpers_return_what_we_read():
    """bof.seo_core turns these into 18 of its 100 points."""
    from content_quality import analyse as content_analyse
    from preload_check import analyse as preload_analyse

    got = set(content_analyse("A sentence with several words in it, repeated."))
    missing = suite.CONTENT_QUALITY_KEYS - got
    assert not missing, f"content_quality no longer returns: {sorted(missing)}"
    assert "word_count" not in got, (
        "content_quality now returns word_count; seo_core deliberately takes the "
        "300-word floor from parse_html instead, so check which one is right")

    got = set(preload_analyse("<html></html>", {}))
    missing = suite.PRELOAD_CHECK_KEYS - got
    assert not missing, f"preload_check no longer returns: {sorted(missing)}"


@check
def google_api_key_is_not_a_placeholder():
    """A truthy placeholder makes every gated script send it and get a 400
    instead of degrading cleanly. Verified live once; this stops it recurring."""
    from bof.seo_core import valid_api_key
    from google_auth import get_api_key

    key = get_api_key()
    assert key is None or valid_api_key(key), (
        f"the configured Google API key is neither absent nor well formed "
        f"({key[:6] if key else key!r}...): fix ~/.config/claude-seo/google-api.json")


@check
def parse_html_returns_what_we_read():
    from parse_html import parse_html

    got = parse_html(
        '<html><head><title>T</title></head>'
        '<body><h1>H</h1><h2>Section</h2></body></html>',
        base_url="https://example.com",
    )
    for key in ("title", "h1", "h2", "schema", "word_count"):
        assert key in got, f"parse_html no longer returns {key!r}"
    assert got["title"] == "T", got["title"]


@check
def url_safety_still_blocks_loopback():
    from url_safety import URLSafetyError, validate_url_strict

    for blocked in ("http://127.0.0.1/", "http://localhost/",
                    "http://169.254.169.254/"):
        try:
            validate_url_strict(blocked)
        except (URLSafetyError, ValueError):
            continue
        raise AssertionError(f"SSRF guard let {blocked} through")


@check
def render_page_returns_sixteen_keys():
    """Network. Plain fetch path, no browser."""
    from render_page import render_page

    got = render_page("https://example.com", mode="never", extract_content=False)
    assert set(got) == suite.RENDER_PAGE_KEYS, (
        f"render_page shape drifted.\n"
        f"  added:   {sorted(set(got) - suite.RENDER_PAGE_KEYS)}\n"
        f"  removed: {sorted(suite.RENDER_PAGE_KEYS - set(got))}"
    )
    assert got["status_code"] == 200, got["status_code"]
    assert got["raw_content"], "raw_content empty; the SSR ratio depends on it"


render_page_returns_sixteen_keys.network = True


@check
def bundled_chromium_actually_launches():
    """Network plus browser. This is the check that proves the path pin works."""
    from render_page import render_page

    got = render_page(
        "https://example.com",
        mode="always",
        timeout_ms=30000,
        block_resources=["image", "font", "media", "stylesheet"],
        extract_accessibility=True,
    )
    assert not got["error"], got["error"]
    assert got["render_engine"], (
        f"no render engine used; diagnostics: {got['render_diagnostics']}"
    )
    assert got["content"], "rendered content empty"
    # Not asserted: got["accessibility_tree"]. See aria_snapshot_is_available.


bundled_chromium_actually_launches.network = True


@check
def aria_snapshot_is_available():
    """bof captures its own accessibility view. Do not use render_page's.

    Playwright 1.61 removed ``page.accessibility`` entirely. render_page wraps
    that call in a bare ``except Exception`` and sets the key to None, so
    ``accessibility_tree`` is silently always None on this install, and the
    suite's own agent_ux_check reports tree_present=false on every run.
    ``locator.aria_snapshot()`` is the supported replacement. It returns a YAML
    string of roles and accessible names, not a dict tree, so
    ``analyze_accessibility_tree`` cannot consume it either.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(
                '<body><main><button>Save</button><button></button>'
                '<a href="/x">Docs</a></main></body>',
                wait_until="load",
            )
            snap = page.locator("body").aria_snapshot()
        finally:
            browser.close()

    assert snap, "aria_snapshot returned nothing"
    assert 'button "Save"' in snap, f"named control missing from snapshot:\n{snap}"
    assert "- button\n" in snap or snap.rstrip().endswith("- button"), (
        f"unnamed control not distinguishable; the named-interactive ratio "
        f"depends on that distinction:\n{snap}"
    )


aria_snapshot_is_available.network = True


@check
def a_pdf_engine_works():
    """One of the two PDF paths must work. On Windows it is Chromium.

    WeasyPrint needs GTK natives (libgobject-2.0-0) that pip cannot install, so
    google_report's ``--format pdf`` raises here. Chromium ``page.pdf()`` needs
    no system install and renders the same HTML google_report already emits.
    """
    try:
        import weasyprint  # noqa: F401
    except Exception as weasy_exc:
        weasy = f"unavailable ({type(weasy_exc).__name__})"
    else:
        weasy = "available"

    import tempfile
    from pathlib import Path

    from playwright.sync_api import sync_playwright

    out = Path(tempfile.gettempdir()) / "bof_contract_probe.pdf"
    out.unlink(missing_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content("<h1>probe</h1>", wait_until="load")
            page.pdf(path=str(out), format="A4", print_background=True)
        finally:
            browser.close()

    ok = out.is_file() and out.read_bytes()[:5] == b"%PDF-"
    assert ok, f"neither PDF engine works (weasyprint {weasy}, chromium failed)"
    print(f"        weasyprint {weasy}; using chromium page.pdf()")
    out.unlink(missing_ok=True)


a_pdf_engine_works.network = True


def main() -> int:
    offline = "--offline" in sys.argv
    print(f"claude-seo {suite.VERSION} at {suite.SUITE}")
    if suite.VERSION not in suite.TESTED_VERSIONS:
        print(f"  note: {suite.VERSION} is not in TESTED_VERSIONS yet")

    failures = []
    for fn in CHECKS:
        if offline and getattr(fn, "network", False):
            print(f"  skip  {fn.__name__} (offline)")
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report every failure, not the first
            failures.append((fn.__name__, exc))
            print(f"  FAIL  {fn.__name__}: {exc}")
        else:
            print(f"  ok    {fn.__name__}")

    if failures:
        print(f"\n{len(failures)} of {len(CHECKS)} checks failed. "
              f"Do not add {suite.VERSION!r} to TESTED_VERSIONS.")
        return 1
    print(f"\nAll checks passed. Safe to add {suite.VERSION!r} to TESTED_VERSIONS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
