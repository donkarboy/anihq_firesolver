import requests
import re
import json
import os
import sys
import time

# ── Configuration ─────────────────────────────────────────────────────────────
FLARESOLVERR      = "http://localhost:8191/v1"
URL_LIST_FILE     = os.environ.get("URL_LIST_FILE", "anihq2.txt")
LIMIT             = int(os.environ.get("LIMIT", "1000"))
OUTPUT_FILE       = "media_stream.json"
NO_MEDIA_FILE     = "no_media_found.txt"
MEDIA_FOUND_FILE  = "media_found.txt"
PROCESSED_FILE    = "already_processed_urls.txt"
MAX_BYTES         = 5 * 1024 * 1024  # 5 MB per file before rotation

# Custom set config — set by workflow; empty string means "don't use".
# Rule: if CUSTOM_SET_FILE is provided, ONLY those URLs are scraped.
# The bulk list is NEVER loaded when a custom set file is active.
CUSTOM_SET_FILE   = os.environ.get("CUSTOM_SET_FILE", "").strip()
CUSTOM_SET_ONLY   = bool(CUSTOM_SET_FILE)   # automatic — no separate env var needed

# ── URL parsing helpers ───────────────────────────────────────────────────────

def parse_watch_url(url):
    """
    Parse an AniHQ watch URL into its components.

    Handles all observed suffix variants after the episode number:
      -english-dubbed   → dub
      -english-subbed   → sub
      -dubbed / -dub    → dub
      -subbed / -sub    → sub
      (nothing)         → sub  (default)

    Returns a dict:
      {
        "anime_key":  str,   # normalised slug, e.g. "naruto-shippuuden"
        "title":      str,   # human title,     e.g. "Naruto Shippuuden"
        "episode":    int,   # episode number
        "media_type": str,   # "dub" or "sub"
      }
    or None if the URL doesn't contain /watch/ or an episode number.
    """
    m = re.search(r'/watch/(.+?)/?$', url.rstrip('/'))
    if not m:
        return None

    slug = m.group(1).lower()

    # Split on "-episode-" (maxsplit=1) so slugs containing "episode" still work
    parts = slug.split("-episode-", 1)
    if len(parts) != 2:
        return None

    anime_slug = parts[0]   # e.g. "naruto-shippuuden"
    after_ep   = parts[1]   # e.g. "485-english-dubbed" or "1-dub" or "3"

    ep_match = re.match(r'^(\d+)(?:-(.+))?$', after_ep)
    if not ep_match:
        return None

    episode_num = int(ep_match.group(1))
    suffix      = ep_match.group(2) or ""

    DUB_RE = re.compile(r'^(english[-\s]dubbed?|dubbed?|dub)$', re.IGNORECASE)
    SUB_RE = re.compile(r'^(english[-\s]subbed?|subbed?|sub)$', re.IGNORECASE)

    if DUB_RE.match(suffix):
        media_type = "dub"
    elif SUB_RE.match(suffix) or suffix == "":
        media_type = "sub"
    else:
        media_type = "sub"
        print(f"  [warn] Unrecognised suffix {suffix!r} in {url!r} — treating as sub")

    title = anime_slug.replace("-", " ").title()

    return {
        "anime_key":  anime_slug,
        "title":      title,
        "episode":    episode_num,
        "media_type": media_type,
    }


def build_episode_url(template_url, episode_num):
    """
    Replace the episode number inside a template URL with `episode_num`.

    The template URL always contains the episode number embedded in the path
    segment that follows "-episode-".  We locate that segment and swap it.

    Example:
      template : https://anihq.cc/watch/naruto-shippuuden-episode-1-english-dubbed/
      episode  : 42
      result   : https://anihq.cc/watch/naruto-shippuuden-episode-42-english-dubbed/
    """
    # Replace the digit(s) immediately after "-episode-" with the new number
    new_url = re.sub(
        r'(-episode-)(\d+)',
        lambda m: f"{m.group(1)}{episode_num}",
        template_url,
        count=1
    )
    return new_url


# ── Custom set parser ─────────────────────────────────────────────────────────

def parse_custom_set_file(path):
    """
    Parse custom_url_extract_set.txt and return an ordered list of URLs
    to scrape, with deduplication preserving first-seen order.

    File format (one set per line):
      set_N: <template_url_with_episode_1> <start> to <end>

    Examples:
      set_1: https://anihq.cc/watch/naruto-shippuuden-episode-1-english-dubbed/ 1 to 500
      set_2: https://anihq.cc/watch/naruto-shippuuden-episode-1-english-subbed/ 1 to 500
      set_3: https://anihq.cc/watch/death-note-episode-1-english-dubbed/ 1 to 37

    Rules:
      - Lines starting with # are comments and are skipped.
      - Blank lines are skipped.
      - The template URL must contain "-episode-<digit>" somewhere.
      - Episode range is inclusive on both ends: "1 to 500" → episodes 1…500.
      - If start > end the range is silently skipped.
      - Duplicate generated URLs (same set seen twice) are deduplicated.
    """
    if not os.path.exists(path):
        print(f"[custom_set] File not found: {path}")
        return []

    urls      = []
    seen      = set()
    set_count = 0
    url_count = 0

    print(f"\n[custom_set] Parsing {path}")

    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, 1):
            line = raw_line.strip()

            # Skip blank lines and comments
            if not line or line.startswith("#"):
                continue

            # Expected format:  set_N: <url> <start> to <end>
            # The label "set_N:" is optional — we accept any "word:" prefix
            # or lines that start directly with http
            #
            # Regex captures:
            #   group 1: optional "set_N: " prefix  (non-capturing wrapper)
            #   group 2: the template URL
            #   group 3: start episode number
            #   group 4: end episode number
            m = re.match(
                r'^(?:\w+\s*:\s*)?'           # optional "set_N:" label
                r'(https?://\S+)'             # template URL (no spaces)
                r'\s+(\d+)\s+to\s+(\d+)'     # " 1 to 500"
                r'\s*$',
                line,
                re.IGNORECASE
            )

            if not m:
                print(f"  [custom_set] Line {lineno}: unrecognised format — skipping: {line!r}")
                continue

            template_url = m.group(1).rstrip('/')
            ep_start     = int(m.group(2))
            ep_end       = int(m.group(3))

            if ep_start > ep_end:
                print(f"  [custom_set] Line {lineno}: start > end ({ep_start} > {ep_end}) — skipping")
                continue

            # Validate that the template URL actually contains "-episode-<digit>"
            if not re.search(r'-episode-\d+', template_url, re.IGNORECASE):
                print(f"  [custom_set] Line {lineno}: template URL has no episode marker — skipping: {template_url!r}")
                continue

            set_count += 1
            added_this_set = 0

            for ep in range(ep_start, ep_end + 1):
                generated = build_episode_url(template_url, ep)
                if generated not in seen:
                    seen.add(generated)
                    urls.append(generated)
                    added_this_set += 1
                else:
                    print(f"  [custom_set] Line {lineno}: ep {ep} duplicate — skipped")

            url_count += added_this_set
            print(f"  [custom_set] Line {lineno}: {added_this_set} URLs added "
                  f"(ep {ep_start}–{ep_end}) → {template_url}")

    print(f"[custom_set] Total: {set_count} set(s), {url_count} unique URLs generated\n")
    return urls


# ── File rotation helpers ─────────────────────────────────────────────────────

def rotate(path):
    """Rename path → path.1 (or .2, .3 …) to archive an oversized file."""
    idx = 1
    while os.path.exists(f"{path}.{idx}"):
        idx += 1
    os.rename(path, f"{path}.{idx}")
    print(f"  [rotate] {path} → {path}.{idx}")
    return f"{path}.{idx}"


def append_line(path, line):
    """Append a single URL line; rotate the file first if it would exceed MAX_BYTES."""
    line = line.rstrip("\n") + "\n"
    cur_size = os.path.getsize(path) if os.path.exists(path) else 0
    if cur_size + len(line.encode()) > MAX_BYTES:
        rotate(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _all_copies(path):
    """Return every existing copy of a file: base + rotated siblings (.1 .2 …)."""
    copies = []
    if os.path.exists(path):
        copies.append(path)
    idx = 1
    while os.path.exists(f"{path}.{idx}"):
        copies.append(f"{path}.{idx}")
        idx += 1
    return copies


def load_text_set(path):
    """Load all lines from a file and all its rotated copies into a set."""
    lines = set()
    for p in _all_copies(path):
        with open(p, "r", encoding="utf-8") as f:
            lines.update(l.strip() for l in f if l.strip())
    return lines


# ── JSON persistence (grouped format) ─────────────────────────────────────────

def load_anime_map(path):
    """
    Load media_stream.json into an in-memory dict keyed by anime_key.
    Internal fields (_anime_key, _new) are preserved for re-use within the run.
    """
    if not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            print(f"  [warn] Could not parse {path} — starting fresh.")
            return {}

    if not isinstance(data, list):
        return {}

    anime_map = {}
    for entry in data:
        key = entry.get("_anime_key") or _title_to_key(entry.get("title", ""))
        if key:
            anime_map[key] = entry

    return anime_map


def _title_to_key(title):
    """Convert a human title back to a slug key (best-effort reverse)."""
    return title.lower().replace(" ", "-")


def _count_episodes(entry):
    """Return the highest episode number found in the entry's media_url keys."""
    max_ep = 0
    for k in entry:
        m = re.match(r'media_url_(?:dub|sub)_ep_(\d+)$', k)
        if m:
            max_ep = max(max_ep, int(m.group(1)))
    return max_ep


def _sorted_ep_keys(entry):
    """
    Return all media_url_*_ep_* keys sorted by episode number first,
    then dub before sub — matching the target format's dub/sub alternating pattern.
    """
    keys = [k for k in entry if re.match(r'media_url_(?:dub|sub)_ep_\d+$', k)]

    def sort_key(k):
        m = re.match(r'media_url_(dub|sub)_ep_(\d+)$', k)
        return (int(m.group(2)), 0 if m.group(1) == "dub" else 1)

    return sorted(keys, key=sort_key)


def anime_map_to_list(anime_map):
    """Convert the in-memory dict → sorted JSON-ready list."""
    entries = sorted(anime_map.values(), key=lambda e: e.get("serial_no", 9999999))

    result = []
    for i, entry in enumerate(entries, 1):
        out = {
            "serial_no":      i,
            "title":          entry.get("title", ""),
            "mal_id":         entry.get("mal_id", ""),
            "total_episodes": _count_episodes(entry),
            "tmdb_id":        entry.get("tmdb_id", ""),
        }
        for k in _sorted_ep_keys(entry):
            out[k] = entry[k]
        result.append(out)

    return result


def save_anime_map(path, anime_map):
    """
    Serialise anime_map → JSON list.
    Rotates the file and starts a fresh one if size exceeds MAX_BYTES.
    """
    output_list = anime_map_to_list(anime_map)

    full_list = []
    for entry, raw in zip(
        output_list,
        sorted(anime_map.values(), key=lambda e: e.get("serial_no", 9999999))
    ):
        enriched = dict(entry)
        enriched["_anime_key"] = raw.get("_anime_key", _title_to_key(entry["title"]))
        full_list.append(enriched)

    content = json.dumps(full_list, indent=2, ensure_ascii=False)

    if len(content.encode("utf-8")) > MAX_BYTES:
        rotate(path)
        fresh = [e for e in full_list if anime_map.get(e["_anime_key"], {}).get("_new")]
        for e in fresh:
            e.pop("_new", None)
        content = json.dumps(fresh, indent=2, ensure_ascii=False)
    else:
        for e in full_list:
            e.pop("_new", None)
        content = json.dumps(full_list, indent=2, ensure_ascii=False)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def processed_urls_from_map(anime_map):
    """
    Return a set of (anime_key, episode, type) tuples for every stream URL
    already stored in the JSON — used for O(1) dedup without re-reading text logs.
    """
    done = set()
    for key, entry in anime_map.items():
        for k in entry:
            m = re.match(r'media_url_(dub|sub)_ep_(\d+)$', k)
            if m and entry[k]:
                done.add((key, int(m.group(2)), m.group(1)))
    return done


# ── Build master URL queue ────────────────────────────────────────────────────

custom_urls = []
if CUSTOM_SET_FILE:
    # ── CUSTOM MODE ───────────────────────────────────────────────────────────
    # When a custom set file is provided, ONLY those URLs are scraped.
    # The bulk list is never opened, never read, never touched.
    print(f"[mode] CUSTOM SET — only scraping URLs from: {CUSTOM_SET_FILE}")
    print("[mode] Bulk URL list is IGNORED.")
    custom_urls = parse_custom_set_file(CUSTOM_SET_FILE)
    if not custom_urls:
        print("[custom_set] No URLs generated — check the file format.")
        sys.exit(0)
    all_urls = custom_urls
    print(f"Total URLs to process      : {len(all_urls)}")
else:
    # ── BULK MODE ─────────────────────────────────────────────────────────────
    # No custom set file → normal bulk scrape from the URL list file.
    print(f"[mode] BULK — reading URL list from: {URL_LIST_FILE}")
    if not os.path.exists(URL_LIST_FILE):
        print(f"ERROR: Bulk URL file not found: {URL_LIST_FILE}")
        sys.exit(1)
    with open(URL_LIST_FILE, "r", encoding="utf-8") as f:
        all_urls = [l.strip() for l in f if l.strip().startswith("http")]
    print(f"Total bulk URLs in file    : {len(all_urls)}")

# ── Load existing state ───────────────────────────────────────────────────────

processed_urls  = load_text_set(PROCESSED_FILE)
no_media_set    = load_text_set(NO_MEDIA_FILE)
media_found_set = load_text_set(MEDIA_FOUND_FILE)
anime_map       = load_anime_map(OUTPUT_FILE)
json_done       = processed_urls_from_map(anime_map)


def is_done(url):
    """Return True if this URL has already been fully processed."""
    if url in processed_urls or url in no_media_set or url in media_found_set:
        return True
    parsed = parse_watch_url(url)
    if parsed and (parsed["anime_key"], parsed["episode"], parsed["media_type"]) in json_done:
        return True
    return False


pending = [u for u in all_urls if not is_done(u)]

print(f"\nAlready in text logs       : {len(processed_urls | no_media_set | media_found_set)}")
print(f"Anime entries in JSON      : {len(anime_map)}")
print(f"Pending (not yet scraped)  : {len(pending)}")
print(f"Limit this run             : {LIMIT}")
print("-" * 60)

to_scrape = pending[:LIMIT]
if not to_scrape:
    print("Nothing to scrape — all URLs already done.")
    sys.exit(0)

# ── Scrape loop ───────────────────────────────────────────────────────────────
success_count  = 0
no_media_count = 0
error_count    = 0

next_serial = max((e.get("serial_no", 0) for e in anime_map.values()), default=0) + 1

for idx, url in enumerate(to_scrape, 1):
    print(f"[{idx}/{len(to_scrape)}] {url}")

    parsed = parse_watch_url(url)
    if not parsed:
        print(f"  SKIP: URL doesn't match expected /watch/{{slug}}-episode-{{N}}-{{type}} pattern")
        append_line(NO_MEDIA_FILE, url)
        append_line(PROCESSED_FILE, url)
        no_media_count += 1
        continue

    anime_key  = parsed["anime_key"]
    title      = parsed["title"]
    episode    = parsed["episode"]
    media_type = parsed["media_type"]

    print(f"  title={title!r}  ep={episode}  type={media_type}")

    # ── Fetch via FlareSolverr ────────────────────────────────────────────────
    try:
        resp = requests.post(
            FLARESOLVERR,
            json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  ERROR (network): {e}")
        error_count += 1
        continue

    status    = data.get("status", "unknown")
    cf_status = data.get("solution", {}).get("status", "unknown")
    html      = data.get("solution", {}).get("response", "")

    if status != "ok":
        print(f"  SKIP: FlareSolverr status={status}")
        error_count += 1
        continue

    # ── Extract stream iframe ─────────────────────────────────────────────────
    iframe_srcs = re.findall(
        r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE
    )
    if not iframe_srcs:
        iframe_srcs = re.findall(
            r'<iframe[^>]+data-src=["\']([^"\']+)["\']', html, re.IGNORECASE
        )

    if not iframe_srcs:
        print(f"  NO MEDIA (CF={cf_status}) → {NO_MEDIA_FILE}")
        append_line(NO_MEDIA_FILE, url)
        append_line(PROCESSED_FILE, url)
        no_media_count += 1
        continue

    # Filter ad / social iframes, take first valid stream URL
    stream_url = None
    for src in iframe_srcs:
        if src.startswith("javascript:") or src.startswith("about:"):
            continue
        if "googlesyndication" in src or "doubleclick" in src:
            continue
        if "facebook.com/plugins" in src or "twitter.com/widgets" in src:
            continue
        stream_url = src
        break

    if not stream_url:
        print(f"  NO VALID STREAM in iframes: {iframe_srcs}")
        append_line(NO_MEDIA_FILE, url)
        append_line(PROCESSED_FILE, url)
        no_media_count += 1
        continue

    print(f"  stream → {stream_url}")

    # ── Upsert into anime_map ─────────────────────────────────────────────────
    if anime_key not in anime_map:
        anime_map[anime_key] = {
            "_anime_key": anime_key,
            "serial_no":  next_serial,
            "title":      title,
            "mal_id":     "",
            "tmdb_id":    "",
            "_new":       True,
        }
        next_serial += 1
    else:
        anime_map[anime_key]["_new"] = True

    anime_map[anime_key][f"media_url_{media_type}_ep_{episode}"] = stream_url

    append_line(MEDIA_FOUND_FILE, url)
    append_line(PROCESSED_FILE, url)
    success_count += 1

    save_anime_map(OUTPUT_FILE, anime_map)
    time.sleep(1)

# ── Final save ────────────────────────────────────────────────────────────────
for entry in anime_map.values():
    entry.pop("_new", None)
save_anime_map(OUTPUT_FILE, anime_map)

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Run complete.")
print(f"  Scraped this run   : {len(to_scrape)}")
print(f"  Media found        : {success_count}")
print(f"  No media           : {no_media_count}")
print(f"  Errors             : {error_count}")
print(f"  Total anime in JSON: {len(anime_map)}")

if error_count > 0 and error_count == len(to_scrape):
    print("ERROR: Every URL errored out.")
    sys.exit(1)
