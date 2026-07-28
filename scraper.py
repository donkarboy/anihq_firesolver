import requests
import re
import json
import os
import sys
import time

FLARESOLVERR         = "http://localhost:8191/v1"
URL_LIST_FILE        = os.environ.get("URL_LIST_FILE", "anihq2.txt")
LIMIT                = int(os.environ.get("LIMIT", "1000"))
OUTPUT_FILE          = "media_stream.json"
NO_MEDIA_FILE        = "no_media_found.txt"
MEDIA_FOUND_FILE     = "media_found.txt"
PROCESSED_FILE       = "already_processed_urls.txt"
MAX_BYTES            = 5 * 1024 * 1024  # 5 MB

# ── File helpers ─────────────────────────────────────────────────────────────

def rotate(path):
    """Rename path → path.1 (or .2, .3 …) and return the new archive name."""
    idx = 1
    while os.path.exists(f"{path}.{idx}"):
        idx += 1
    os.rename(path, f"{path}.{idx}")
    print(f"  [rotate] {path} → {path}.{idx}")
    return f"{path}.{idx}"

def append_line(path, line):
    """Append one URL line; rotate file first if it would exceed MAX_BYTES."""
    line = line.rstrip("\n") + "\n"
    cur_size = os.path.getsize(path) if os.path.exists(path) else 0
    if cur_size + len(line.encode()) > MAX_BYTES:
        rotate(path)
    with open(path, "a") as f:
        f.write(line)

def load_text_set(path):
    """Load all lines from a file AND any rotated copies (.1 .2 …)."""
    lines = set()
    for p in _all_copies(path):
        with open(p, "r") as f:
            lines.update(l.strip() for l in f if l.strip())
    return lines

def _all_copies(path):
    """Return existing copies: base file + rotated siblings."""
    copies = []
    if os.path.exists(path):
        copies.append(path)
    idx = 1
    while os.path.exists(f"{path}.{idx}"):
        copies.append(f"{path}.{idx}")
        idx += 1
    return copies

def load_json_list(path):
    """Load JSON list from base file (rotated copies are archived — not reloaded)."""
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        try:
            data = json.load(f)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            return []

def save_json(path, data):
    """Write JSON; rotate if over MAX_BYTES then start fresh with current batch."""
    content = json.dumps(data, indent=2)
    if len(content.encode()) > MAX_BYTES:
        rotate(path)
        # active file keeps only entries added this run
        data = [e for e in data if e.pop("_new", False)]
        content = json.dumps(data, indent=2)
    else:
        for e in data:
            e.pop("_new", None)
    with open(path, "w") as f:
        f.write(content)

def slug_to_title(url):
    slug = url.rstrip("/").split("/watch/")[-1]
    slug = re.sub(r"-episode-\d+.*$", "", slug)
    return slug.replace("-", " ").title()

# ── Load URL list ─────────────────────────────────────────────────────────────
print(f"Reading URL list from : {URL_LIST_FILE}")
with open(URL_LIST_FILE, "r") as f:
    all_urls = [l.strip() for l in f if l.strip().startswith("http")]
print(f"Total URLs in file    : {len(all_urls)}")

# ── Load already-processed sets ───────────────────────────────────────────────
processed_urls  = load_text_set(PROCESSED_FILE)   # successful + no-media
no_media_set    = load_text_set(NO_MEDIA_FILE)
media_found_set = load_text_set(MEDIA_FOUND_FILE)

# Also honour existing JSON entries
existing   = load_json_list(OUTPUT_FILE)
json_urls  = {e["main_url"] for e in existing}

done_urls  = processed_urls | no_media_set | media_found_set | json_urls
pending    = [u for u in all_urls if u not in done_urls]

print(f"Already processed     : {len(processed_urls)}")
print(f"No-media (logged)     : {len(no_media_set)}")
print(f"Media found (logged)  : {len(media_found_set)}")
print(f"Pending               : {len(pending)}")
print(f"Limit this run        : {LIMIT}")
print("-" * 60)

to_scrape = pending[:LIMIT]
if not to_scrape:
    print("Nothing to scrape — all URLs already done.")
    sys.exit(0)

# ── Scrape loop ───────────────────────────────────────────────────────────────
success_count = 0
no_media_count = 0
error_count = 0

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
        print(f"  ERROR: {e}")
        error_count += 1
        continue

    status    = data.get("status", "unknown")
    cf_status = data.get("solution", {}).get("status", "unknown")
    html      = data.get("solution", {}).get("response", "")

    if status != "ok":
        print(f"  SKIP: FlareSolverr status={status}")
        error_count += 1
        continue

    iframe_srcs = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)

    if not iframe_srcs:
        print(f"  NO MEDIA (CF={cf_status}) → {NO_MEDIA_FILE}")
        append_line(NO_MEDIA_FILE, url)
        append_line(PROCESSED_FILE, url)
        no_media_count += 1
        continue

    for src in iframe_srcs:
        print(f"  iframe → {src}")
        entry = {
            "main_url":  url,
            "media_url": src,
            "mal_id":    "",
            "title":     slug_to_title(url),
            "_new":      True,
        }
        existing.append(entry)

    append_line(MEDIA_FOUND_FILE, url)
    append_line(PROCESSED_FILE, url)
    success_count += 1

    # Save JSON after every successful URL
    save_json(OUTPUT_FILE, existing)

    time.sleep(1)

# ── Final JSON save ───────────────────────────────────────────────────────────
for e in existing:
    e.pop("_new", None)
save_json(OUTPUT_FILE, existing)

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"Run complete.")
print(f"  Scraped this run   : {len(to_scrape)}")
print(f"  Media found        : {success_count}")
print(f"  No media           : {no_media_count}")
print(f"  Errors             : {error_count}")
print(f"  Total in JSON      : {len(existing)}")

if error_count == len(to_scrape):
    print("ERROR: Every URL errored out.")
    sys.exit(1)
