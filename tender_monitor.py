#!/usr/bin/env python3
"""
Global Tender Monitor — single-shot runner.

Each invocation:
  1. Loads previously-seen tender IDs from state (per-region shard)
  2. Calls Gemini with Google Search grounding enabled and the
     monitoring system prompt
  3. Parses the returned JSON, dedupes against state
  4. Writes per-run result files (JSON + CSV) and updates state
  5. Updates a manifest the dashboard polls for auto-refresh
  6. Optionally posts a Slack/Teams webhook for the first tender
     in the run whose deadline is within 7 days

Schedule it externally (cron, Task Scheduler, GitHub Actions, AWS
EventBridge -> Lambda, etc.) — see README.md.

Environment:
  GEMINI_API_KEY        required (or GOOGLE_API_KEY)
  TENDER_MODEL          optional, defaults to gemini-2.5-flash
                        (free tier; use gemini-2.5-pro for higher quality)
  TENDER_MAX_TOKENS     optional, defaults to 8000
  TENDER_REGIONS        optional CSV, e.g. "India,Middle East,Europe"
                        defaults to all regions
  TENDER_SHARD          optional shard tag (per-region cron isolation).
                        Defaults to a slug derived from TENDER_REGIONS,
                        or "global". Drives state/result file names so
                        each market lead can run their own cron job
                        without stepping on each other.
  TENDER_WEBHOOK_URL    optional Slack or Teams incoming webhook URL
  TENDER_WEBHOOK_TYPE   "slack" (default) or "teams"
  TENDER_VERIFY_URLS    "1" (default) issues a HEAD/GET to each source_url
                        and adds a url_status field; "0" disables.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    sys.stderr.write("google-genai SDK not installed. Run: pip install google-genai\n")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths

ROOT = Path(__file__).resolve().parent
PROMPT_FILE = ROOT / "system_prompt.md"
OUTPUT_ROOT = ROOT / "tender_results"
LOG_FILE = ROOT / "tender_monitor.log"
OUTPUT_ROOT.mkdir(exist_ok=True)

SEEN_IDS_TO_SEND = 500
CSV_COLUMNS = [
    "id", "title", "region", "country", "tendering_authority",
    "domain_industry", "service_bucket", "scope_summary",
    "deadline_utc", "published_utc", "value_amount", "value_currency",
    "source_url", "confidence",
]


# ---------------------------------------------------------------------------
# Utilities

def slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s.strip().lower()).strip("_")
    return s or "global"


LOG_MAX_LINES = 500


def log(msg: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def rotate_log() -> None:
    """Keep the log file to the last LOG_MAX_LINES lines."""
    if not LOG_FILE.exists():
        return
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    if len(lines) > LOG_MAX_LINES:
        LOG_FILE.write_text("\n".join(lines[-LOG_MAX_LINES:]) + "\n", encoding="utf-8")


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"WARN: state file {path.name} unreadable, starting fresh")
    return {"seen_ids": [], "last_run_utc": None, "total_runs": 0}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def hours_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    last = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 3600.0


def parse_deadline(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def strip_fences(text: str) -> str:
    """Pull the largest top-level JSON object out of the model's reply,
    tolerating extra ```json fences or trailing prose."""
    text = text.strip()
    # Remove every fence marker (```json or ```) — they're not part of JSON.
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE).strip()
    # Find the outermost {...} by brace-matching; return the longest match.
    best = ""
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0: start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                chunk = text[start:i+1]
                if len(chunk) > len(best):
                    best = chunk
                start = -1
    return best or text


# ---------------------------------------------------------------------------
# Grounding metadata + URL verification

def extract_grounding(response) -> list[dict]:
    """Pull the {uri, title} list of sources Gemini actually retrieved via
    Google Search. These are the URLs we can trust — anything inside the
    JSON body is model-synthesized and may be hallucinated."""
    out = []
    try:
        cands = getattr(response, "candidates", None) or []
        if not cands:
            return out
        gm = getattr(cands[0], "grounding_metadata", None)
        if not gm:
            return out
        for ch in getattr(gm, "grounding_chunks", None) or []:
            web = getattr(ch, "web", None)
            if not web:
                continue
            uri = getattr(web, "uri", None)
            title = getattr(web, "title", None)
            if uri:
                out.append({"uri": uri, "title": title or ""})
    except Exception as e:
        log(f"WARN: could not read grounding metadata: {e}")
    return out


def verify_url(url: str, timeout: float = 6.0) -> tuple[str, str | None]:
    """Probe a URL. Returns (status, final_url):
      status in {"ok", "redirect_ok", "broken", "skipped"}.
      final_url is set when redirects landed somewhere reachable."""
    if not url or not url.startswith(("http://", "https://")):
        return ("skipped", None)
    headers = {
        "User-Agent": "Mozilla/5.0 (TenderMonitor/1.0; +https://example.invalid)",
        "Accept": "*/*",
    }
    # Try HEAD first; some portals reject HEAD so fall back to a small GET.
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final = resp.geturl()
                code = resp.status
                if 200 <= code < 400:
                    return ("redirect_ok" if final != url else "ok", final)
                return ("broken", final)
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 501):
                continue  # some servers refuse HEAD; try GET
            return ("broken", None)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if method == "HEAD":
                continue
            return ("broken", None)
        except Exception:
            return ("broken", None)
    return ("broken", None)


# ---------------------------------------------------------------------------
# CSV + manifest

def write_csv(path: Path, tenders: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for t in tenders:
            row = {k: t.get(k, "") for k in CSV_COLUMNS}
            # Flatten nested estimated_value into CSV columns
            ev = t.get("estimated_value") or {}
            if isinstance(ev, dict):
                row["value_amount"] = ev.get("amount") or ""
                row["value_currency"] = ev.get("currency") or ""
            # flatten anything non-scalar
            for k, v in list(row.items()):
                if isinstance(v, (list, dict)):
                    row[k] = json.dumps(v, ensure_ascii=False)
            w.writerow(row)


def update_manifest(shard: str, json_name: str, run_ts: str, count: int) -> None:
    """Append the new run to tender_results/index.json so the dashboard
    can poll for new files without needing a directory listing."""
    manifest_path = OUTPUT_ROOT / "index.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {"runs": []}
    else:
        manifest = {"runs": []}

    rel_path = f"{shard}/{json_name}"
    runs = [r for r in manifest.get("runs", []) if r.get("file") != rel_path]
    runs.append({
        "file": rel_path,
        "shard": shard,
        "run_utc": run_ts,
        "tenders_found": count,
    })
    runs.sort(key=lambda r: r.get("run_utc", ""))
    # keep last 500 entries
    manifest["runs"] = runs[-500:]
    manifest["updated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    # Atomic write: write to temp file then rename to avoid partial reads
    tmp_path = manifest_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp_path.replace(manifest_path)


# ---------------------------------------------------------------------------
# Webhook

def first_urgent(tenders: list[dict], days: int = 7) -> dict | None:
    cutoff = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    urgent = []
    for t in tenders:
        d = parse_deadline(t.get("deadline_utc"))
        if d and dt.datetime.now(dt.timezone.utc) <= d <= cutoff:
            urgent.append((d, t))
    if not urgent:
        return None
    urgent.sort(key=lambda x: x[0])
    return urgent[0][1]


def post_webhook(url: str, kind: str, tender: dict, shard: str) -> None:
    title = tender.get("title", "(untitled)")
    authority = tender.get("tendering_authority", "")
    country = tender.get("country", "")
    deadline = tender.get("deadline_utc", "")
    bucket = tender.get("service_bucket", "")
    source = tender.get("source_url", "")
    value = tender.get("value_amount") or ""
    currency = tender.get("value_currency", "")
    value_str = f"{currency} {value}".strip() if value else ""

    if kind == "teams":
        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": f"Tender alert ({shard})",
            "themeColor": "E06A5F",
            "title": f"Tender deadline within 7 days — {shard}",
            "sections": [{
                "activityTitle": title,
                "facts": [
                    {"name": "Authority", "value": authority},
                    {"name": "Country",   "value": country},
                    {"name": "Bucket",    "value": bucket},
                    {"name": "Deadline",  "value": deadline},
                    {"name": "Value",     "value": value_str or "—"},
                ],
                "markdown": True,
            }],
            "potentialAction": [{
                "@type": "OpenUri",
                "name": "Open source",
                "targets": [{"os": "default", "uri": source}],
            }] if source else [],
        }
    else:  # slack
        text_lines = [
            f"*Tender deadline within 7 days* — shard `{shard}`",
            f"*{title}*",
            f"_{authority} · {country} · {bucket}_",
            f"Deadline: `{deadline}`" + (f"  ·  Value: `{value_str}`" if value_str else ""),
        ]
        if source:
            text_lines.append(f"<{source}|Open source>")
        payload = {"text": "\n".join(text_lines)}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        log(f"webhook ({kind}) posted: {title[:60]}")
    except urllib.error.URLError as e:
        log(f"WARN: webhook post failed: {e}")


# ---------------------------------------------------------------------------
# Main

def run() -> int:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        log("FATAL: GEMINI_API_KEY (or GOOGLE_API_KEY) not set")
        return 2

    model = os.environ.get("TENDER_MODEL", "gemini-2.5-flash")
    try:
        max_tokens = int(os.environ.get("TENDER_MAX_TOKENS", "8000"))
    except ValueError:
        max_tokens = 8000
        log("WARN: invalid TENDER_MAX_TOKENS, defaulting to 8000")
    regions = os.environ.get("TENDER_REGIONS", "all")
    shard = os.environ.get("TENDER_SHARD") or slugify(regions if regions != "all" else "global")

    shard_dir = OUTPUT_ROOT / shard
    shard_dir.mkdir(exist_ok=True)
    state_file = ROOT / f"tender_state_{shard}.json"

    # Migrate legacy single-state file the first time we run sharded.
    legacy = ROOT / "tender_state.json"
    if legacy.exists() and not state_file.exists() and shard == "global":
        state_file.write_bytes(legacy.read_bytes())
        log("migrated legacy tender_state.json -> tender_state_global.json")

    if not PROMPT_FILE.exists():
        log(f"FATAL: {PROMPT_FILE} missing")
        return 2
    system_prompt = PROMPT_FILE.read_text(encoding="utf-8")

    state = load_state(state_file)
    hrs = hours_since(state.get("last_run_utc"))
    seen = state.get("seen_ids", [])
    seen_to_send = seen[-SEEN_IDS_TO_SEND:]

    if hrs is None:
        lookback_hint = "first run — use lookback_days = 7"
    else:
        lookback_hint = f"hours_since_last_run = {hrs:.2f}"

    user_msg = (
        f"Run a tender check.\n"
        f"{lookback_hint}\n"
        f"Regions: {regions}\n"
        f"seen_ids (do NOT return any tender whose id is in this list): "
        f"{json.dumps(seen_to_send)}\n\n"
        f"Search all 8 service buckets across the in-scope regions. "
        f"Return the JSON object only — no prose, no markdown fences."
    )

    log(f"Calling {model}  shard={shard}  seen_ids cached: {len(seen)}, sent: {len(seen_to_send)}")

    client = genai.Client(api_key=api_key)
    response = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=model,
                contents=user_msg,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=max_tokens,
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                    # Disable thinking budget — flash burns most of max_output_tokens
                    # on thinking, leaving the JSON reply truncated mid-string.
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            break
        except Exception as e:
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                log(f"WARN: Gemini API error (attempt {attempt+1}/3): {e} — retrying in {wait}s")
                time.sleep(wait)
            else:
                log(f"FATAL: Gemini API error after 3 attempts: {e}")
                return 3

    try:
        raw = (response.text or "").strip()
    except Exception as e:
        log(f"FATAL: could not read response text (safety filter?): {e}")
        return 3
    cleaned = strip_fences(raw)

    try:
        result = json.loads(cleaned, strict=False)
    except json.JSONDecodeError as e:
        log(f"FATAL: model did not return valid JSON: {e}")
        debug_path = shard_dir / f"DEBUG_{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.txt"
        debug_path.write_text(raw, encoding="utf-8")
        log(f"  raw response saved to {debug_path}")
        return 4

    seen_set = set(seen)
    new_tenders = []
    new_ids = []
    for t in result.get("tenders", []):
        tid = t.get("id")
        if not tid or tid in seen_set:
            continue
        new_tenders.append(t)
        seen_set.add(tid)
        new_ids.append(tid)

    # Attach the verified URLs that Google Search actually returned.
    # These are vertexaisearch.cloud.google.com redirect URLs that resolve
    # to the real source — the dashboard can show them as the "trusted"
    # alternative to the model-synthesized source_url.
    grounding = extract_grounding(response)
    result["grounding_urls"] = grounding

    # Verify each tender's model-supplied source_url (off via TENDER_VERIFY_URLS=0).
    verify = os.environ.get("TENDER_VERIFY_URLS", "1") != "0"
    if verify and new_tenders:
        log(f"verifying {len(new_tenders)} source URL(s)...")
        ok = broken = 0
        for t in new_tenders:
            status, final = verify_url(t.get("source_url") or "")
            t["url_status"] = status
            if final and final != t.get("source_url"):
                t["source_url_resolved"] = final
            if status == "broken":
                broken += 1
                # Demote confidence — the model invented an unreachable URL.
                if t.get("confidence") == "high":
                    t["confidence"] = "low"
            elif status in ("ok", "redirect_ok"):
                ok += 1
        log(f"  url verification: {ok} ok, {broken} broken")

    result["tenders"] = new_tenders
    result["tenders_found"] = len(new_tenders)
    result["shard"] = shard

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    fname_base = f"tenders_{now_iso.replace(':', '-')}"
    json_path = shard_dir / f"{fname_base}.json"
    csv_path = shard_dir / f"{fname_base}.csv"

    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    write_csv(csv_path, new_tenders)
    update_manifest(shard, json_path.name, now_iso, len(new_tenders))

    state["seen_ids"] = seen + new_ids
    state["last_run_utc"] = now_iso
    state["total_runs"] = state.get("total_runs", 0) + 1
    save_state(state_file, state)

    # Webhook: first tender with deadline within 7 days
    webhook_url = os.environ.get("TENDER_WEBHOOK_URL", "").strip()
    if webhook_url and new_tenders:
        urgent = first_urgent(new_tenders, days=7)
        if urgent:
            kind = os.environ.get("TENDER_WEBHOOK_TYPE", "slack").lower()
            post_webhook(webhook_url, kind, urgent, shard)
        else:
            log("no tender with deadline within 7 days — webhook skipped")

    log(f"DONE: {len(new_tenders)} new tender(s) -> {json_path.relative_to(ROOT)}")
    rotate_log()
    return 0


if __name__ == "__main__":
    sys.exit(run())
