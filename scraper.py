import requests
import re
import json
import os
import sys
import time

FLARESOLVERR    = "http://localhost:8191/v1"
URL_LIST_FILE   = os.environ.get("URL_LIST_FILE", "anihq2.txt")
LIMIT           = int(os.environ.get("LIMIT", "1000"))
OUTPUT_FILE     = "media_stream.json"
NO_IFRAME_FILE  = "no_iframe_found.txt"
PROCESSED_FILE  = "urls_already_processed.txt"
MAX_FILE_BYTES  = 3 * 1024 * 1024   # 3 MB

# ── Helpers ──────────────────────────────────────────────────────────────────

def slug_to_title(url):
    slug = url.rstrip("/").split("/watch/")[-1]
    slug = re.sub(r"-episode-\d+.*$", "", slug)
    return slug.replace("-", " ").title()

def load_text_set(path):
    """Return set of non-empty lines from a text file."""
    if not os.path.exists(path):
        return set()
    with open(path, "r") as f:
        return {line.strip() for line in f if line.strip()}

def append_line(path, line, max_bytes=MAX_FILE_BYTES):
    """
    Append a single line to a rolling text file.
    When the file hits max_bytes, rotate: rename to .1, .2 … and start fresh.
    """
    if os.path.exists(path) and os.path.getsize(path) >= max_bytes:
        # find next free rotation index
        idx = 1
        while os.path.exists(f"{path}.{idx}"):
            idx += 1
        os.rename(path, f"{path}.{idx}")
    with open(path, "a") as f:
        f.write(line.rstrip("\n") + "\n")

def load_json_list(path):
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        try:
            data = json.load(f)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            return []

def save_json(path, data, max_bytes=MAX_FILE_BYTES):
    """
    Save JSON list. If file would exceed max_bytes, rotate old file and
    start a new one (current entries only go into the new file so the
    working file stays small; history is in the rotated copies).
    """
    content = json.dumps(data, indent=2)
    if len(content.encode()) >= max_bytes and os.path.exists(path):
        idx = 1
        while os.path.exists(f"{path}.{idx}"):
            idx += 1
        os.rename(path, f"{path}.{idx}")
        # keep only the newest batch in the active file
        data = [e for e in data if e.get("_new")]
        for e in data:
            e.pop("_new", None)
        content = json.dumps(data, indent=2)
    with open(path, "w") as f:
        f.write(content)

# ── Load URL list ─────────────────────────────────────────────────────────────
print(f"Reading URL list from : {URL_LIST_FILE}")
with open(URL_LIST_FILE, "r") as f:
    all_urls = [line.strip() for line in f if line.strip().startswith("http")]

print(f"Total URLs in file    : {len(all_urls)}")

# ── Load already-processed & no-iframe sets ───────────────────────────────────
processed_urls  = load_text_set(PROCESSED_FILE)
no_iframe_urls  = load_text_set(NO_IFRAME_FILE)
done_urls       = processed_urls | no_iframe_urls

# Also honour existing media_stream.json entries
existing        = load_json_list(OUTPUT_FILE)
done_urls      |= {e["main_url"] for e in existing}

pending = [u for u in all_urls if u not in done_urls]

print(f"Already processed     : {len(processed_urls)}")
print(f"No-iframe (skipped)   : {len(no_iframe_urls)}")
print(f"Pending               : {len(pending)}")
print(f"Limit this run        : {LIMIT}")
print("-" * 60)

to_scrape = pending[:LIMIT]
if not to_scrape:
    print("Nothing to scrape — all URLs already done.")
    sys.exit(0)

# ── Scrape loop ───────────────────────────────────────────────────────────────
new_entries    = []
failed_urls    = []
success_count  = 0

for idx, url in enumerate(to_scrape, 1):
    print(f"[{idx}/{len(to_scrape)}] {url}")

    # ── Try with JS rendering first (renderjs) ────────────────────────────
    iframe_srcs = []
    for use_js in (True, False):
        payload = {
            "cmd":        "request.get",
            "url":        url,
            "maxTimeout": 60000,
        }
        if use_js:
            payload["returnOnlyCookies"] = False
            payload["render"]            = True    # asks FlareSolverr to render JS

        try:
            resp = requests.post(FLARESOLVERR, json=payload, timeout=90)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  ERROR: {e}")
            failed_urls.append(url)
            break

        status = data.get("status", "unknown")
        html   = data.get("solution", {}).get("response", "")

        if status != "ok":
            print(f"  SKIP: status={status}")
            failed_urls.append(url)
            break

        iframe_srcs = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)

        # also check data-src (lazy-load pattern)
        if not iframe_srcs:
            iframe_srcs = re.findall(r'<iframe[^>]+data-src=["\']([^"\']+)["\']', html, re.IGNORECASE)

        if iframe_srcs:
            break   # found — no need for second attempt

        if use_js:
            print(f"  JS render: no iframe. Trying plain fetch…")

    if not iframe_srcs and url not in failed_urls:
        print(f"  WARN: No iframe found — saving to {NO_IFRAME_FILE}")
        append_line(NO_IFRAME_FILE, url)
        failed_urls.append(url)
        continue

    if url in failed_urls:
        continue

    # ── Save successful result ────────────────────────────────────────────
    for src in iframe_srcs:
        print(f"  iframe → {src}")
        entry = {
            "main_url":  url,
            "media_url": src,
            "mal_id":    "",
            "title":     slug_to_title(url),
            "_new":      True,
        }
        new_entries.append(entry)
        existing.append(entry)

    append_line(PROCESSED_FILE, url)
    success_count += 1

    # Save JSON after every successful URL
    save_json(OUTPUT_FILE, existing)

    time.sleep(1)

# ── Final JSON save (strips _new markers) ────────────────────────────────────
for e in existing:
    e.pop("_new", None)
save_json(OUTPUT_FILE, existing)

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"Run complete.")
print(f"  Scraped this run   : {len(to_scrape)}")
print(f"  Successful         : {success_count}")
print(f"  No iframe / failed : {len(failed_urls)}")
print(f"  Total in JSON      : {len(existing)}")
print(f"  {NO_IFRAME_FILE}  — {os.path.getsize(NO_IFRAME_FILE) if os.path.exists(NO_IFRAME_FILE) else 0} bytes")
print(f"  {PROCESSED_FILE} — {os.path.getsize(PROCESSED_FILE) if os.path.exists(PROCESSED_FILE) else 0} bytes")

if success_count == 0 and to_scrape:
    print("ERROR: No URLs succeeded.")
    sys.exit(1)
