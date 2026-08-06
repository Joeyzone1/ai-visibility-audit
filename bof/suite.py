# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""The only module in bof that knows claude-seo exists.

Import this before importing anything from the suite::

    import bof.suite  # noqa: F401
    from render_page import render_page

It does two things: pins ``PLAYWRIGHT_BROWSERS_PATH`` to the suite's bundled
Chromium (scripts launched outside ``claude-seo run`` will not find it
otherwise), and puts the suite's flat ``scripts/`` dir on ``sys.path``.

We import rather than vendor because ``url_safety.py`` is SSRF and
DNS-rebinding defense that receives upstream fixes. Forking it would silently
rot. The cost of importing is signature drift on upgrade, and
``bof/test_suite_contract.py`` is the entire mitigation for that.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

#: Suite root. Override with CLAUDE_SEO_HOME if the install moves.
SUITE = pathlib.Path(
    os.environ.get(
        "CLAUDE_SEO_HOME", pathlib.Path.home() / ".claude" / "skills" / "seo"
    )
)
SCRIPTS = SUITE / "scripts"
BROWSERS = SUITE / "ms-playwright"

#: Suite versions bof has been contract-tested against. Add a version here
#: only after ``python -m bof.test_suite_contract`` passes on it.
TESTED_VERSIONS = {"2.2.4"}

#: Suite callables bof depends on, as module -> names. The contract test
#: asserts each of these still exists and still takes the arguments we pass.
DEPENDENCIES = {
    "url_safety": ("validate_url_strict", "safe_requests_get",
                   "make_safe_playwright_route_handler"),
    "render_page": ("render_page",),
    "parse_html": ("parse_html",),
    "ucp_check": ("audit_site",),
    "agent_ux_check": ("analyze_html", "analyze_accessibility_tree"),
    "google_report": ("generate_report",),
    # bof.seo_core. A signature or return-key change here moves SEO scores
    # silently and invalidates every stored delta, so both are asserted below.
    "content_quality": ("analyse",),
    "preload_check": ("analyse",),
    "google_auth": ("get_api_key",),
}

#: Return keys bof.seo_core reads off the two SEO helpers. content_quality
#: reports ``tokens``, not ``word_count``: the 300-word floor comes from
#: parse_html instead, and this asserts that stays true.
CONTENT_QUALITY_KEYS = frozenset({"overall_quality", "tokens", "flags",
                                  "information_density"})
PRELOAD_CHECK_KEYS = frozenset({"score", "preload_hints", "prerender_links",
                                "lcp_resource_hints"})

#: Exact keys ``render_page()`` returns. bof reads content, raw_content and
#: accessibility_tree off this; a missing key means a silent scoring change.
RENDER_PAGE_KEYS = frozenset({
    "url", "status_code", "content", "raw_content", "is_spa", "extracted_text",
    "publication_date", "accessibility_tree", "headers", "redirect_chain",
    "console_errors", "render_diagnostics", "render_engine", "render_ms",
    "mode_used", "error",
})


class SuiteError(RuntimeError):
    """claude-seo is missing, or is a version bof has not been tested against."""


def _read_version() -> str:
    meta = SUITE / "runtime-plugin.json"
    if not meta.is_file():
        raise SuiteError(
            f"claude-seo not found at {SUITE}.\n"
            f"Install it, or point CLAUDE_SEO_HOME at the install."
        )
    try:
        return json.loads(meta.read_text(encoding="utf-8"))["version"]
    except (ValueError, KeyError) as exc:
        raise SuiteError(f"{meta} is not a readable plugin manifest: {exc}") from exc


VERSION = _read_version()

if VERSION not in TESTED_VERSIONS and not os.environ.get("BOF_ALLOW_UNTESTED_SUITE"):
    raise SuiteError(
        f"claude-seo {VERSION} is untested (bof knows {sorted(TESTED_VERSIONS)}).\n"
        f"  1. Run: python -m bof.test_suite_contract\n"
        f"  2. If it passes, add {VERSION!r} to TESTED_VERSIONS in bof/suite.py\n"
        f"To bypass for one run: set BOF_ALLOW_UNTESTED_SUITE=1"
    )

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BROWSERS))

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
