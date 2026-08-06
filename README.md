# AI Visibility Audit

**Can AI assistants read, quote and operate your site?**

A deterministic audit of one page. No LLM in the scoring path, no API keys
required, and the same page scores the same way twice.

Most tools in this space are an SEO audit with a GEO section bolted on. This is
the inverse. SEO is measured, reported, and structurally prevented from moving
the headline number. There is a test that asserts it.

---

## The number

```
AI Visibility Score = 0.40 x engine readability
                    + 0.35 x social citation surface
                    + 0.25 x agent operability
```

Four lenses, three in the headline:

| Lens | Question | In the headline |
|---|---|---|
| **AI engine readability** | Can an assistant reach the page, parse it without running JavaScript, and lift a quotable passage out of it? | 40% |
| **Social citation surface** | Are you present where citations actually come from? | 35% |
| **AI agent operability** | Can an agent acting for a user find the controls, name them and fill the forms? | 25% |
| **Future readiness** | MCP, WebMCP, NLWeb, UCP. All draft standards. | never, opportunity only |
| **SEO Technical Baseline** | Indexability, metadata, structure. | never, supporting evidence |

A component that cannot be measured with confidence drops out and the remaining
weights renormalise, so the headline always stays out of 100 and the report says
what carried it.

## What you get

```
$ python -m bof.report https://example.com --brand "Example" --out ./audits

[1/7] render and gather
[2/7] engine and operability
[3/7] seo baseline
[4/7] future readiness
[5/7] social surface
[6/7] report envelope
[7/7] pdf

headline 69/100 (AI Visibility Score: engine readability 62%, agent
operability 38%. The social citation surface dropped out: it could not be
measured well enough to carry a headline number, so the remaining weights
were renormalised)
  engine readability 64/100 band C
  agent operability  78/100 band B
  social surface      0/100 (30% measured)
  future readiness    0/100 (opportunity only)
  seo baseline       66/100 band C (supporting evidence, not in the headline)
```

Plus a branded PDF, an `audit-data.json` with every signal and its evidence, and
a start-here page written for whoever is paying rather than whoever is
implementing:

```
Example.com scores 69 out of 100 for AI visibility. Broken down: assistants
reading and quoting you, 64 out of 100; agents acting on your site, 78 out of
100. 2 of the fixes below take under an hour each. Doing only those takes the
score to about 80.

| # | Fix                                        | Effort  | Projected |
|---|--------------------------------------------|---------|-----------|
| 1 | Sitemap discoverable and valid             | quick   | 75        |
| 2 | Organization identity declared with sameAs | quick   | 80        |
| 3 | Entity anchored to Wikidata                | project | 86        |
```

The projected scores are cumulative and recomputed, not summed. Lifts are not
additive: each fix changes its lens's share of a renormalised headline, so
adding them up overstates the result.

## Three rules it will not break

**Absent is not the same as correct.** A page with no buttons, no accessible
names and no form fields used to take full marks on the three signals that
measure them and score 84 for operability. That is a number about the page's
emptiness. Signals with nothing to measure now drop out of their lens and the
rest renormalise; if too little survives, the lens reports no score at all
rather than a flattering one.

**Unmeasured is not the same as zero.** If a platform can't be checked because a
key is missing, that is our instrumentation gap, not the site's fault. It is
reported as unmeasured and its weight is redistributed across what could be
measured.

**A promise you cannot collect is not made.** The audit detects the platform.
On Substack, Medium, Wix and Squarespace, signals the owner cannot change from
inside the platform are marked, carry a workaround instead of an instruction,
and stop claiming a headline lift nobody can collect. They still count against
the score, because an assistant reading the page is affected either way.

## What a finding looks like

> Scoring 0 of 7 points. Measured by: looked for a Sitemap directive then
> /sitemap.xml, parsed for `<loc>`. **What we saw:** discovered: no; valid: no;
> loc count: 0. **What it means:** A sitemap is a table of contents listing
> every page you want found. It is how a crawler discovers pages nothing links
> to yet, which usually means your newest work. **If you leave it:** Recent
> posts stay undiscovered for weeks, so you are cited on old material and
> invisible on current material. Closing this adds about 6 points to the AI
> Visibility Score.

Measurement, evidence, meaning, consequence. The lift is exact arithmetic over
the weights, not an estimate, and the action plan is ordered by effort band
first so a ten-minute fix outranks a three-month one worth more points.

## Install

Requires [claude-seo](https://github.com/AgriciDaniel/claude-seo) (MIT), which
supplies the renderer, the HTML parser, the URL safety layer and a bundled
Chromium. Install it first, then:

```bash
git clone https://github.com/Joeyzone1/ai-visibility-audit
cd ai-visibility-audit
```

Everything runs under claude-seo's virtualenv. Do not add packages to it:
`runtime.py` compares a `requirements_sha256` and any change forces a full
re-setup. That constraint is why the web UI is stdlib `http.server` rather than
FastAPI.

```bash
"$HOME/.claude/skills/seo/.venv/Scripts/python.exe" -m bof.test_suite_contract
```

If that passes, the suite is wired up correctly.

## Use

```bash
# one audit, straight to a PDF
python -m bof.report https://example.com --brand "Example" --out ./audits

# or the web UI on http://localhost:8610
python serve_ui.py

# background runs, watched from anywhere
python -m bof.audit https://example.com --brand "Example"
python -m bof.audit --watch <run_id>
python -m bof.audit --list

# a watchlist audited on a schedule, one at a time
python -m bof.monitor --add https://example.com --brand "Example"
python -m bof.monitor --run
tools\schedule.ps1 install          # Windows, daily

# what changed since last time
python -m bof.trend
```

Set your own byline by creating `bof/branding.local.json`, which is gitignored:

```json
{"author": "Your Company"}
```

## Design notes

**One run at a time, enforced by the database.** An audit launches Chromium up
to four times sequentially. Two concurrent runs is an out-of-memory question,
not a performance one, so a second start is refused by a unique index rather
than by application code.

**Detached worker, not a thread.** A thread dies silently when its parent
restarts and orphans Chromium. A detached child keeps writing progress to
SQLite and the UI reconnects to whatever is already running.

**Heartbeat, never PID liveness.** `os.kill(pid, 0)` is unreliable on Windows.
A run with no heartbeat for 180 seconds is marked stalled.

**A material change gate on trends.** Re-auditing an unchanged page does not
produce the same number twice. Under 3 points and with no signal changing class,
the previous number is what gets shown. A one-point drop with a signal newly
critical is material; a two-point rise with nothing reclassified is not.

## Checks

Nine suites, all offline except the contract test:

```bash
python -m bof.test_suite_contract
python -m bof.store --self-check
python -m bof.serve --self-check
python -m bof.trend --self-check
python -m bof.agent_ready --self-check
python -m bof.future_ready --self-check
python -m bof.social_surface --self-check
python -m bof.seo_core --self-check
python -m bof.report --self-check
```

Among the things they assert: each lens normalises to 100 at full marks; a
signal appearing in two lenses is counted in both; a dropped signal renormalises
the rest; a page an assistant cannot use scores 0; a campaign-effort fix never
outranks a quick one; and the SEO score set to 100 and set to 0 produce a
byte-identical headline.

## Known limits

- **One page, not a crawl.** Plus its robots.txt and sitemap. It is a page
  audit and says so.
- **No measured performance.** PageSpeed Insights without an API key returns
  `429 RESOURCE_EXHAUSTED`, so the SEO baseline contains a static hint proxy and
  nothing more. The report says so rather than implying otherwise.
- **Reddit is not measured.** It needs an OAuth script app. Its 25 points
  redistribute.
- **YouTube and Core Web Vitals both need one Google API key.** Without it, the
  two largest measurement gaps stay open.
- **Future readiness scores near zero for almost everyone.** It cannot rank
  sites against each other yet. Its value is the roadmap, not the comparison.

## Licence

AGPL-3.0. You can use, modify and self-host this freely. If you run a modified
version as a network service, you must publish your changes.

Depends on claude-seo (MIT). See [NOTICE](NOTICE).
