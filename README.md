# Global Tender Monitor

A scheduled script that asks Gemini (with Google Search grounding) to find
newly published global tenders in 8 digital-engineering service buckets,
deduplicates them against prior runs, and writes JSON files ready for
dashboard ingestion.

## Files

| File                  | Purpose                                                       |
| --------------------- | ------------------------------------------------------------- |
| `system_prompt.md`    | The instructions Gemini follows on every run. Edit to tune.   |
| `tender_monitor.py`   | The runner. One run = one cron tick.                          |
| `tender_state_<shard>.json` | Auto-created. Per-shard seen IDs + last-run timestamp.  |
| `tender_results/<shard>/`   | Auto-created. JSON + CSV per run, grouped by shard.     |
| `tender_results/index.json` | Auto-created manifest the dashboard polls for new runs. |
| `tender_monitor.log`  | Auto-created. Append-only run log.                            |

## Setup

```bash
pip install google-genai
export GEMINI_API_KEY=...        # get one free at https://aistudio.google.com/apikey
python tender_monitor.py         # run once to sanity-check
```

Default model is `gemini-2.5-flash` (free tier, fast). Set
`TENDER_MODEL=gemini-2.5-pro` for higher-quality extraction at the cost of
free-tier rate limits. Both use Google Search grounding automatically.

First run uses a 7-day lookback. Subsequent runs use the elapsed time since
the previous run, so a missed cron tick won't cause gaps.

## How dedup works

`tender_state.json` accumulates the `id` (SHA-256 of source URL, truncated)
of every tender Gemini returns. On each run, the most recent 500 IDs are
sent back in the user message so the model knows not to repeat them. The
script also enforces dedup client-side after the model responds.

## Scheduling

### Linux / macOS — cron

```cron
# Every 4 hours (saner than hourly for tender portals)
0 */4 * * * GEMINI_API_KEY=... /usr/bin/python3 /opt/tender_monitor/tender_monitor.py

# Hourly, if you really want it
0 * * * * GEMINI_API_KEY=... /usr/bin/python3 /opt/tender_monitor/tender_monitor.py
```

### Windows — Task Scheduler

Create a Basic Task: Trigger = Daily, recur every 1 hour, Action = Start a
Program → `python.exe`, arguments = `C:\tender_monitor\tender_monitor.py`,
and add `GEMINI_API_KEY` to environment variables.

### GitHub Actions + GitHub Pages (free deployment, recommended)

A ready-to-use workflow lives at [`.github/workflows/tender-monitor.yml`](.github/workflows/tender-monitor.yml).
It runs every 4 hours, executes one job per region shard, and commits the
results back to the repo. GitHub Pages then serves the dashboard so anyone
with the URL can view it. Public repos get unlimited Actions minutes; this
costs nothing.

**One-time setup (5 minutes):**

1. **Create a GitHub repo** (public — public repos get free unlimited
   Actions minutes; private repos are capped at 2000 min/month, still
   plenty for this workload).
2. **Push the project:**
   ```bash
   cd D:\tender\tender_v1
   git init && git add . && git commit -m "initial"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
3. **Add your Gemini key** — Settings → Secrets and variables → Actions →
   *New repository secret*. Name: `GEMINI_API_KEY`. Value: your key from
   <https://aistudio.google.com/apikey>.
4. *(Optional)* Add `TENDER_WEBHOOK_URL` as a second secret to enable
   Slack/Teams alerts for urgent (≤7d) tenders.
5. **Enable GitHub Pages** — Settings → Pages → *Source: Deploy from a
   branch*, *Branch: main*, *Folder: / (root)* → Save.
6. **Trigger the first run** — Actions tab → *tender-monitor* workflow →
   *Run workflow* → Run. Wait ~5 minutes for all 9 shards to finish.
7. **Open your dashboard**:
   `https://<your-username>.github.io/<repo-name>/`
   The Auto-refresh toggle works against the live `tender_results/index.json`
   in the repo.

**How it stays alive:** GitHub disables scheduled workflows after 60 days of
repo inactivity. Each run commits results back, which counts as activity —
so this workflow self-keeps-alive as long as it's producing anything.

**Cost ceiling:** Free Gemini tier is generous; 9 shards × 6 runs/day ≈ 54
requests/day, well under the daily quota for `gemini-2.5-flash`.

### AWS Lambda + EventBridge

Package `tender_monitor.py` with the `google-genai` SDK, store state in S3 or
DynamoDB (swap the `load_state`/`save_state` functions), trigger via
EventBridge schedule rule.

## Recommended cadence

Hourly is technically supported but overkill — most tender portals publish
once or twice per business day, and you'll burn API budget on empty runs.
Recommended:

- **Active sourcing**: every 4 hours during business hours in your priority
  regions (e.g., cron `0 6,10,14,18 * * *` IST).
- **Background monitoring**: twice daily.
- **Hourly**: only if you have a confirmed need for sub-day reaction time.

## Region-sharded cron jobs

Each invocation reads `TENDER_SHARD` (or derives a slug from
`TENDER_REGIONS`) and writes its state/results to a shard-specific path:

```
tender_state_india.json
tender_state_middle_east.json
tender_results/
  india/tenders_*.json + tenders_*.csv
  middle_east/tenders_*.json + tenders_*.csv
  index.json   <-- manifest of all runs, used by the dashboard
```

Give each market lead their own cron job — independent state, independent
webhook, independent CSV/JSON export folder:

```cron
0 */4 * * * TENDER_SHARD=india        TENDER_REGIONS="India"       TENDER_WEBHOOK_URL=... python3 tender_monitor.py
0 */4 * * * TENDER_SHARD=middle_east  TENDER_REGIONS="Middle East" TENDER_WEBHOOK_URL=... python3 tender_monitor.py
0 */4 * * * TENDER_SHARD=europe       TENDER_REGIONS="Europe"      TENDER_WEBHOOK_URL=... python3 tender_monitor.py
```

A legacy `tender_state.json` (pre-shard) is auto-migrated to
`tender_state_global.json` on the first run with the default shard.

## Slack / Teams webhook

Set `TENDER_WEBHOOK_URL` (and optionally `TENDER_WEBHOOK_TYPE=teams`,
default `slack`) and each run will post the first new tender whose deadline
is within 7 days. Other tenders still land in the JSON/CSV output — the
webhook is just the "act today" nudge, not a firehose.

```bash
export TENDER_WEBHOOK_URL="https://hooks.slack.com/services/T.../B.../..."
export TENDER_WEBHOOK_TYPE=slack    # or teams
```

## CSV export

Every run writes `tenders_<timestamp>.csv` next to the JSON, with one row
per new tender and the columns:

```
id,title,region,country,tendering_authority,domain_industry,
service_bucket,scope_summary,deadline_utc,published_utc,
value_amount,value_currency,source_url,confidence
```

The dashboard's **Export CSV** button does the same for whatever is
currently visible (post-filter).

## Dashboard ingestion + auto-refresh

Each `tender_results/<shard>/tenders_*.json` file conforms to the schema in
`system_prompt.md` § 4. To feed a dashboard, point your loader at the folder
and treat new files as the delta. Tender IDs are stable across runs so you
can upsert by ID.

The bundled `dashboard.html` has an **Auto-refresh** toggle that polls
`tender_results/index.json` every 60 seconds and pulls in any new run files
it hasn't seen. Because browsers block `fetch()` on `file://`, serve the
folder over HTTP:

```bash
cd /path/to/tender_v1
python -m http.server 8000
# open http://localhost:8000/dashboard.html
```

Suggested minimal SQL schema:

```sql
CREATE TABLE tenders (
  id                  TEXT PRIMARY KEY,
  title               TEXT,
  region              TEXT,
  country             TEXT,
  tendering_authority TEXT,
  domain_industry     TEXT,
  service_bucket      TEXT,
  scope_summary       TEXT,
  key_points          JSONB,
  deadline_utc        TIMESTAMPTZ,
  published_utc       TIMESTAMPTZ,
  value_amount        NUMERIC,
  value_currency      TEXT,
  source_url          TEXT,
  confidence          TEXT,
  first_seen_utc      TIMESTAMPTZ DEFAULT now()
);
```

## Tuning

- **Coverage vs cost**: Gemini's Google Search grounding decides how many
  searches to run; there's no `max_uses` knob. To bias coverage, edit
  `system_prompt.md` to mention specific portals or to request more breadth.
- **Region focus**: set `TENDER_REGIONS=India,Middle East` to narrow scope
  (and cost) per cron job. You can run multiple cron jobs with different
  region/state pairs if you want isolated dashboards per region.
- **Prompt drift**: edit `system_prompt.md` and re-run. Add portals you know
  matter for your industry niche. Keep the JSON schema stable so your
  dashboard doesn't break.

## Caveats — important

- **Tender notices are messy.** Many portals require login, render through
  JavaScript, or hide details behind PDFs. Gemini's web search will catch the
  public-facing notices and aggregator listings, but a portion of relevant
  tenders will only be visible via direct portal logins. For exhaustive
  coverage, pair this with paid tender-aggregator subscriptions
  (TendersInfo, Bidnet, Global Tenders, etc.) and pipe their feeds into the
  same dedup layer.
- **Verify before bidding.** Treat every result as a lead, not a source of
  truth. The `confidence` field is a hint; always open `source_url` and
  confirm details on the issuing authority's official portal before
  committing time or money.
- **API limits / cost.** Gemini's free tier covers `gemini-2.5-flash` at a
  generous request/day quota that's enough for hourly runs of this script.
  If you hit rate limits, drop cadence (every 4h) or enable billing in
  Google AI Studio for the paid tier. `gemini-2.5-pro` is higher quality
  but free-tier RPM is much tighter — best on a paid key.
