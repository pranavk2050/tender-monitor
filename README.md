# Global Tender Monitor

A scheduled script that asks **Gemini** (with Google Search grounding) to
find newly published tenders across 8 digital-engineering service buckets
worldwide, deduplicates them against prior runs, writes per-shard JSON +
CSV files, and feeds a static dashboard you can host for free on GitHub
Pages.

Live example: <https://pranavk2050.github.io/tender-monitor/>

## What you get

- **Targeted search** across ~80 named portals (CPPP, GeM, IREPS, ONGC,
  TED, sam.gov, ted.europa.eu, evergabe-online.de, tenderned.nl, ccgp.gov.cn,
  ungm, World Bank, IFC, UNDP, …) plus any portal Google indexes — see
  `system_prompt.md` § 2 for the full list.
- **Per-region sharding** so each market lead can run their own state file
  without stepping on each other (India, Middle East, Europe, APAC, Africa,
  Australia/NZ, USA, Canada, Latin America by default).
- **Anti-hallucination guardrails**: the prompt forbids URL invention; the
  script verifies every `source_url` with a HEAD/GET probe; it surfaces
  Gemini's actual Google-Search grounding URLs as a trusted fallback; and
  it auto-demotes confidence for URLs that don't respond.
- **CSV + JSON output** per run, with a manifest the dashboard polls.
- **Slack / Teams webhook** that pings the first urgent (≤7 days) tender
  per run.
- **A static dashboard** (`dashboard.html`) with auto-refresh, filters,
  shard view, hide-closed toggle, NEW-badge highlighting for live arrivals,
  per-card verification status, and CSV/JSON export of the visible slice.
- **Free deployment path**: GitHub Actions runs the script daily, commits
  results back, and GitHub Pages serves the dashboard — $0/mo for public
  repos.

## Files

| File                              | Purpose                                                   |
| --------------------------------- | --------------------------------------------------------- |
| `system_prompt.md`                | Instructions Gemini follows on every run. Edit to tune.   |
| `tender_monitor.py`               | The runner. One run = one cron tick.                      |
| `dashboard.html`                  | Static UI. Open over HTTP for auto-refresh.               |
| `index.html`                      | Pages-friendly redirect → `dashboard.html`.               |
| `.github/workflows/tender-monitor.yml` | The free-deployment workflow.                        |
| `tender_state_<shard>.json`       | Auto-created. Per-shard seen IDs + last-run timestamp.    |
| `tender_results/<shard>/`         | Auto-created. JSON + CSV per run, grouped by shard.       |
| `tender_results/index.json`       | Auto-created manifest the dashboard polls for new runs.   |
| `tender_monitor.log`              | Auto-created. Append-only run log.                        |

## Setup (local)

```bash
pip install google-genai
export GEMINI_API_KEY=...        # free key: https://aistudio.google.com/apikey
python tender_monitor.py         # one shard, default = global
```

Then open the dashboard:

```bash
python -m http.server 8000       # in the project root
# browse to http://localhost:8000/dashboard.html
# click "Auto-refresh: off" → "Auto-refresh: on"
```

(Auto-refresh needs `http://`. Opening `dashboard.html` via `file://` works
for static view + drag-drop ingest, but `fetch()` is blocked by browsers
on `file://`.)

## Configuration (env vars)

| Variable               | Default              | Notes                                                                                  |
| ---------------------- | -------------------- | -------------------------------------------------------------------------------------- |
| `GEMINI_API_KEY`       | *required*           | Or set `GOOGLE_API_KEY`.                                                              |
| `TENDER_MODEL`         | `gemini-2.5-flash`   | Free-tier friendly. `gemini-2.5-pro` is better but has tighter RPD on free.            |
| `TENDER_MAX_TOKENS`    | `8000`               | Output budget. Thinking is disabled so the full budget goes to JSON.                   |
| `TENDER_REGIONS`       | `all`                | CSV — what the model searches for (`"India"`, `"Europe,Middle East"`, etc.).           |
| `TENDER_SHARD`         | slug of regions      | Filenaming/state isolation; only matters when you want to override.                    |
| `TENDER_WEBHOOK_URL`   | *unset*              | Slack or Teams incoming webhook URL.                                                   |
| `TENDER_WEBHOOK_TYPE`  | `slack`              | `slack` or `teams`.                                                                    |
| `TENDER_VERIFY_URLS`   | `1`                  | Set to `0` to skip the HEAD/GET probe on each `source_url`.                            |

## Free deployment — GitHub Actions + GitHub Pages

A ready-to-use workflow lives at
[`.github/workflows/tender-monitor.yml`](.github/workflows/tender-monitor.yml).
It runs **once daily at 06:00 UTC**, executes one job per region shard
serially, and commits the results back to `main`. GitHub Pages serves the
dashboard so anyone with the URL can view it. Public repos get unlimited
Actions minutes; total cost is $0.

**One-time setup (5 minutes):**

1. **Create a public GitHub repo** (private also works; capped at 2000
   Actions min/month, still ~50× over budget here).
2. **Push:**
   ```bash
   git init && git add . && git commit -m "initial"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. **Add the API key secret** — Settings → Secrets and variables →
   Actions → New repository secret. Name: `GEMINI_API_KEY`. Value: your
   key from <https://aistudio.google.com/apikey>.
4. *(Optional)* Add `TENDER_WEBHOOK_URL` as a second secret to enable
   webhook alerts.
5. **Enable Pages** — Settings → Pages → *Source: Deploy from a branch*,
   *Branch: main*, *Folder: / (root)* → Save.
6. **Trigger the first run** — Actions tab → tender-monitor → Run
   workflow. ~5 minutes total (each shard runs serially).
7. **Open** `https://<your-username>.github.io/<repo-name>/`.

**Free-tier quota math:** `gemini-2.5-flash` free tier is **~20 requests
per project per day**. Nine shards × once-daily = 9 requests/day, leaving
~11 calls/day of headroom for manual reruns, debugging, or one bonus shard
run. If you push beyond that, enable Cloud Billing for the project (~$0.30
for this workload — effectively free) or move to `gemini-2.5-flash-lite`.

**Self-keeps-alive:** GitHub disables scheduled workflows after 60 days of
repo inactivity. Each run commits results back, which counts as activity —
so the workflow re-arms itself.

## Other scheduling options

### Linux / macOS — cron

```cron
# Once daily, all regions. Match cadence to your free-tier RPD.
0 6 * * * GEMINI_API_KEY=... /usr/bin/python3 /opt/tender_monitor/tender_monitor.py

# Independent shards
0 6 * * * GEMINI_API_KEY=... TENDER_REGIONS="India"  TENDER_SHARD=india  python3 .../tender_monitor.py
0 6 * * * GEMINI_API_KEY=... TENDER_REGIONS="Europe" TENDER_SHARD=europe python3 .../tender_monitor.py
```

### Windows — Task Scheduler

Create a Basic Task: Trigger = Daily, Action = `python.exe`, arguments =
`C:\tender_monitor\tender_monitor.py`, environment variables include
`GEMINI_API_KEY`.

### AWS Lambda + EventBridge

Package `tender_monitor.py` with the `google-genai` SDK, replace `load_state`
/ `save_state` to persist in S3 or DynamoDB, schedule via an EventBridge
rule. The verification step needs outbound HTTP, so don't run inside a
locked-down VPC without a NAT.

## Region sharding

Each invocation reads `TENDER_SHARD` (or derives a slug from
`TENDER_REGIONS`) and writes its state/results to a shard-specific path:

```
tender_state_india.json
tender_state_middle_east.json
tender_results/
  india/        tenders_*.json  +  tenders_*.csv
  middle_east/  tenders_*.json  +  tenders_*.csv
  index.json    ← manifest of all runs, used by the dashboard
```

Give each market lead their own cron — independent state, independent
webhook, independent dedup history:

```cron
0 6 * * * TENDER_SHARD=india        TENDER_REGIONS="India"       python3 tender_monitor.py
0 6 * * * TENDER_SHARD=middle_east  TENDER_REGIONS="Middle East" python3 tender_monitor.py
0 6 * * * TENDER_SHARD=europe       TENDER_REGIONS="Europe"      python3 tender_monitor.py
```

A legacy `tender_state.json` (pre-shard) is auto-migrated to
`tender_state_global.json` on the first run with the default shard.

## Slack / Teams webhook

Set `TENDER_WEBHOOK_URL` and each run posts the **first new tender whose
deadline is within 7 days**. Other tenders still land in the JSON/CSV
output — the webhook is just the "act today" nudge, not a firehose.

```bash
export TENDER_WEBHOOK_URL="https://hooks.slack.com/services/T.../B.../..."
export TENDER_WEBHOOK_TYPE=slack    # or teams
```

For GitHub Actions, add `TENDER_WEBHOOK_URL` as a repo secret; the workflow
picks it up automatically.

## CSV export

Every run writes `tenders_<timestamp>.csv` next to the JSON, one row per
new tender:

```
id, title, region, country, tendering_authority, domain_industry,
service_bucket, scope_summary, deadline_utc, published_utc,
value_amount, value_currency, source_url, confidence
```

The dashboard's **Export CSV** button does the same for whatever is
currently visible (post-filter, post-sort).

## URL verification + grounded sources

LLMs hallucinate URLs. This pipeline has three layers of defence:

1. **Prompt clause** (`system_prompt.md` § 5) forbids URL invention —
   Gemini must either quote a URL verbatim from its search results or
   fall back to a portal landing page with reduced confidence.
2. **Probe every URL** — after the model responds, the script HEAD/GETs
   each `source_url` with a 6-second timeout. The result is stored on
   the tender as `url_status ∈ {"ok", "redirect_ok", "broken", "skipped"}`.
   Broken URLs auto-demote `confidence: high` → `low`.
3. **Grounding metadata** — Gemini's `grounding_metadata.grounding_chunks`
   gives the URLs Google Search actually retrieved. The script stores
   them as `grounding_urls` in the run JSON, and the dashboard surfaces
   them in each tender's drawer under **Grounded sources**. These are
   guaranteed-real URLs (vertexaisearch redirect URLs that resolve to the
   true source), so when a `source_url` is broken, the user has a
   trustworthy fallback.

The dashboard reflects this on each card:
- `✓ verified` — green, URL responded
- `✗ broken` — red, URL did not respond (CTA in drawer becomes "Open
  grounded source" instead)
- *(no badge)* — verification was skipped or data predates the feature

Disable verification with `TENDER_VERIFY_URLS=0` if your runner is on a
restrictive network or you want runs to finish faster.

## Dashboard features

Open the URL, click **Auto-refresh: off → on**, leave the tab open.

- **Auto-refresh** polls `tender_results/index.json` every 60s. New cards
  arrive flagged **NEW** (5-minute gold badge with a brief pulse). The
  button status shows last poll time and turns red if 3 polls fail in a
  row.
- **Hide closed** (default on) filters out tenders past their deadline.
- **Shard filter** appears automatically when you have shards that mean
  more than "slugified region" (e.g., `emea` covering Europe+ME+Africa)
  — otherwise it stays hidden as redundant.
- **Bucket chips** are multi-select. Empty selection = all buckets.
- **Search** is full-text across title / authority / country / scope.
- **Sort by** deadline / published / value / title.
- **Export JSON / CSV** for the currently visible slice.
- **Cards** show a confidence dot (high/medium/low), URL verification
  status, bucket tag, days-until-deadline color (red for ≤7d, gold for
  ≤30d), and the tender source link.
- **Drawer** opens on card click: full scope, key points, identifiers,
  resolved-URL diff if redirects fired, and the grounded source list.
  `Esc` closes the drawer.
- **Drag-and-drop** a `.json` file anywhere on the window to ingest a
  one-off export from a colleague.
- **localStorage** keeps your loaded data across reloads.

## Tuning

- **Coverage vs cost.** Gemini's Google Search grounding decides how many
  searches to run; there's no `max_uses` knob. To bias coverage, add
  portals to `system_prompt.md` § 2 or sharpen the query examples in § 3.
- **Region focus.** Narrow `TENDER_REGIONS` per cron job to reduce noise
  per shard, and rely on `TENDER_SHARD` to keep their state isolated.
- **Model.** `gemini-2.5-flash` is the default; for tighter extraction
  with higher token cost, set `TENDER_MODEL=gemini-2.5-pro`.
- **Prompt drift.** Edit `system_prompt.md` and re-run. Keep the JSON
  schema (§ 4) stable or the dashboard will break.
- **Cadence vs quota.** Free-tier flash is 20 RPD. Once daily × 9 shards
  uses ~half of that. If you need higher cadence, enable Cloud Billing
  on the Gemini project — usage is bounded enough that the bill is
  effectively zero.

## Caveats — important

- **Tenders are messy.** Many portals require login, render through JS,
  or hide details behind PDFs. Gemini will catch the public-facing
  notices and aggregator listings, but a portion of relevant tenders
  will only be visible via direct portal logins. The portal list in
  `system_prompt.md` § 2 is free/public only — by design. If you want
  exhaustive coverage you'll eventually want a paid aggregator
  subscription wired into the same dedup layer.
- **Verify before bidding.** Treat every result as a lead, not a source
  of truth. The `confidence` field and the ✓ verified badge are hints.
  Always open the source link and confirm details on the issuing
  authority's official portal before committing time or money.
- **Cron skew.** GitHub Actions cron is ±15 min jittery and can skip
  during peak load. Fine for daily cadence; not fine if you need
  sub-hour precision.
- **API key hygiene.** Use repo secrets, never commit keys. Rotate
  immediately if a key shows up in a screenshot, chat transcript, or
  log dump.
