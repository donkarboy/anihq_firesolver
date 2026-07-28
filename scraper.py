import requests
import re
import json
import os
import sys
import time

FLARESOLVERR  = "http://localhost:8191/v1"
URL_LIST_FILE = os.environ.get("URL_LIST_FILE", "anihq2.txt")
LIMIT         = int(os.environ.get("LIMIT", "1000"))
OUTPUT_FILE   = "media_stream.json"

# ── Load URL list ───────────────────────────────────────────────────────────
print(f"Reading URL list from: {URL_LIST_FILE}")
with open(URL_LIST_FILE, "r") as f:
    all_urls = [line.strip() for line in f if line.strip().startswith("http")]

print(f"Total URLs in file  : {len(all_urls)}")

# ── Load existing results (to skip already done) ────────────────────────────
if os.path.exists(OUTPUT_FILE):
    with open(OUTPUT_FILE, "r") as f:
        try:
            existing = json.load(f)
            if not isinstance(existing, list):
                existing = [existing]
        except json.JSONDecodeError:
            existing = []
else:
    existing = []

done_urls = {e["main_url"] for e in existing}
pending   = [u for u in all_urls if u not in done_urls]

print(f"Already scraped     : {len(done_urls)}")
print(f"Pending URLs        : {len(pending)}")
print(f"Limit this run      : {LIMIT}")
print("-" * 60)

to_scrape = pending[:LIMIT]
if not to_scrape:
    print("Nothing to scrape — all URLs already done.")
    sys.exit(0)

# ── Helper: extract title from URL slug ─────────────────────────────────────
def slug_to_title(url):
    slug = url.rstrip("/").split("/watch/")[-1]
    # strip trailing -episode-N-english-dubbed/subbed
    slug = re.sub(r"-episode-\d+.*$", "", slug)
    return slug.replace("-", " ").title()

# ── Scrape loop ─────────────────────────────────────────────────────────────
failed  = []
added   = 0

for idx, url in enumerate(to_scrape, 1):
    print(f"[{idx}/{len(to_scrape)}] {url}")

    try:
        resp = requests.post(
            FLARESOLVERR,
            json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ERROR: FlareSolverr request failed — {e}")
        failed.append({"url": url, "reason": str(e)})
        continue

    status    = data.get("status", "unknown")
    cf_status = data.get("solution", {}).get("status", "unknown")
    html      = data.get("solution", {}).get("response", "")

    if status != "ok":
        print(f"  SKIP: FlareSolverr status={status}")
        failed.append({"url": url, "reason": f"status={status}"})
        continue

    # Extract iframe src only
    iframe_srcs = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)

    if not iframe_srcs:
        print(f"  WARN: No iframe src found (CF={cf_status})")
        failed.append({"url": url, "reason": "no iframe found"})
        # Still record so we don't retry endlessly
        existing.append({
            "main_url":  url,
            "media_url": None,
            "mal_id":    "",
            "title":     slug_to_title(url),
            "status":    "no_iframe",
        })
        added += 1
        continue

    for src in iframe_srcs:
        print(f"  iframe → {src}")
        existing.append({
            "main_url":  url,
            "media_url": src,
            "mal_id":    "",
            "title":     slug_to_title(url),
        })
        added += 1

    # Save after every URL so progress is never lost
    with open(OUTPUT_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    # Be polite — small delay between requests
    time.sleep(1)

# ── Final save & summary ─────────────────────────────────────────────────────
with open(OUTPUT_FILE, "w") as f:
    json.dump(existing, f, indent=2)

print("\n" + "=" * 60)
print(f"Run complete.")
print(f"  Scraped this run : {len(to_scrape)}")
print(f"  New entries added: {added}")
print(f"  Failed           : {len(failed)}")
print(f"  Total in file    : {len(existing)}")

if failed:
    print("\nFailed URLs:")
    for item in failed:
        print(f"  {item['url']}  ({item['reason']})")

if len(failed) == len(to_scrape):
    print("ERROR: Every URL failed.")
    sys.exit(1)
