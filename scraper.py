import requests
import re
import json
import os
import sys
import time

# ── Configuration ─────────────────────────────────────────────────────────────
FLARESOLVERR     = "http://localhost:8191/v1"
URL_LIST_FILE    = os.environ.get("URL_LIST_FILE", "anihq2.txt")
LIMIT            = int(os.environ.get("LIMIT", "1000"))
OUTPUT_FILE      = "media_stream.json"
NO_MEDIA_FILE    = "no_media_found.txt"
MEDIA_FOUND_FILE = "media_found.txt"
PROCESSED_FILE   = "already_processed_urls.txt"
MAX_BYTES        = 5 * 1024 * 1024  # 5 MB per file before rotation

# ── URL parsing helpers ───────────────────────────────────────────────────────

def parse_watch_url(url):
    """
    Parse an AniHQ watch URL into its components.

    Handles all observed suffix variants after the episode number:
      -english-dubbed        → dub
      -english-subbed        → sub
      -dubbed                → dub
      -subbed                → sub
      -dub                   → dub
      -sub                   → sub
      (nothing)              → sub  (default)

    Examples:
      .../watch/naruto-episode-220-english-dubbed/      → dub
      .../watch/naruto-shippuuden-episode-485-english-dubbed/ → dub
      .../watch/some-anime-episode-1-subbed/            → sub
      .../watch/some-anime-episode-1/                   → sub

    Returns a dict:
      {
        "anime_key":  str,   # normalised slug, e.g. "naruto-shippuuden"
        "title":      str,   # human title,     e.g. "Naruto Shippuuden"
        "episode":    int,   # episode number
        "media_type": str,   # "dub" or "sub"
      }
    or None if the URL doesn't contain /watch/ or an episode number.
    """
    # Isolate everything after /watch/
    m = re.search(r'/watch/(.+?)/?$', url.rstrip('/'))
    if not m:
        return None

    slug = m.group(1).lower()

    # Split on "-episode-" to separate anime slug from the rest
    # Use maxsplit=1 so anime slugs that contain "episode" are handled correctly
    parts = slug.split("-episode-", 1)
    if len(parts) != 2:
        return None

    anime_slug  = parts[0]   # e.g. "naruto-shippuuden"
    after_ep    = parts[1]   # e.g. "485-english-dubbed" or "1-dub" or "3"

    # Extract the leading integer (episode number) and the remaining suffix
    ep_match = re.match(r'^(\d+)(?:-(.+))?$', after_ep)
    if not ep_match:
        return None

    episode_num   = int(ep_match.group(1))          # e.g. 485
    suffix        = ep_match.group(2) or ""          # e.g. "english-dubbed" or ""

    # Map every known suffix pattern to dub / sub
    # Order matters: check longer/more-specific patterns first
    DUB_PATTERNS = re.compile(
        r'^(english[-\s]dubbed?|dubbed?|dub)$', re.IGNORECASE
    )
    SUB_PATTERNS = re.compile(
        r'^(english[-\s]subbed?|subbed?|sub)$', re.IGNORECASE
    )

    if DUB_PATTERNS.match(suffix):
        media_type = "dub"
    elif SUB_PATTERNS.match(suffix) or suffix == "":
        media_type = "sub"
    else:
        # Unknown suffix — log it but still parse; treat as sub
        media_type = "sub"
        print(f"  [warn] Unrecognised suffix {suffix!r} in {url!r} — treating as sub")

    # Build human-readable title from the anime slug
    title = anime_slug.replace("-", " ").title()

    return {
        "anime_key":  anime_slug,
        "title":      title,
        "episode":    episode_num,
        "media_type": media_type,
    }


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

    Returns:
      {
        "anime-slug": {
          "serial_no":      int,
          "title":          str,
          "mal_id":         str,
          "total_episodes": int,
          "tmdb_id":        str,
          "media_url_dub_ep_1": "...",
          "media_url_sub_ep_1": "...",
          ...
        },
        ...
      }
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
        # Reconstruct anime_key from the title stored in the entry
        # We store anime_key explicitly to avoid round-trip loss
        key = entry.get("_anime_key") or _title_to_key(entry.get("title", ""))
        if key:
            anime_map[key] = entry

    return anime_map


def _title_to_key(title):
    """Convert a human title back to a slug key (best-effort reverse)."""
    return title.lower().replace(" ", "-")


def anime_map_to_list(anime_map):
    """
    Convert the in-memory dict → sorted list for JSON output.
    Strips internal _anime_key field, renumbers serial_no, computes total_episodes.
    """
    # Sort by the existing serial_no so order is stable across runs
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
        # Collect all episode media keys in sorted episode order
        ep_keys = _sorted_ep_keys(entry)
        for k in ep_keys:
            out[k] = entry[k]
        result.append(out)

    return result


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
    then dub before sub (matching the target format's alternating dub/sub pattern).
    """
    keys = [k for k in entry if re.match(r'media_url_(?:dub|sub)_ep_\d+$', k)]

    def sort_key(k):
        m = re.match(r'media_url_(dub|sub)_ep_(\d+)$', k)
        ep_num   = int(m.group(2))
        type_ord = 0 if m.group(1) == "dub" else 1
        return (ep_num, type_ord)

    return sorted(keys, key=sort_key)


def save_anime_map(path, anime_map):
    """
    Serialise anime_map → JSON list.
    If the result exceeds MAX_BYTES, rotate the file then write only NEW entries
    (those flagged with _new=True) into the fresh file.
    """
    output_list = anime_map_to_list(anime_map)

    # Attach _anime_key so we can reload correctly next run; strip _new flag
    full_list = []
    for entry, raw in zip(output_list, sorted(anime_map.values(),
                                               key=lambda e: e.get("serial_no", 9999999))):
        enriched = dict(entry)
        enriched["_anime_key"] = raw.get("_anime_key", _title_to_key(entry["title"]))
        full_list.append(enriched)

    content = json.dumps(full_list, indent=2, ensure_ascii=False)

    if len(content.encode("utf-8")) > MAX_BYTES:
        rotate(path)
        # Keep only entries added/updated this run in the new active file
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


# ── Build the set of already-processed main URLs from the JSON map ────────────

def processed_urls_from_map(anime_map):
    """
    Reconstruct the set of individual watch-page URLs that are already stored
    in the JSON.  We do this by looking at every media_url_*_ep_* key and
    noting which (anime_key, episode, type) combinations already exist.
    We can't recreate the original URL exactly, but we can build a canonical
    form that matches how we would insert a URL.
    Returned as a set of (anime_key, episode, type) tuples for O(1) lookup.
    """
    done = set()
    for key, entry in anime_map.items():
        for k in entry:
            m = re.match(r'media_url_(dub|sub)_ep_(\d+)$', k)
            if m and entry[k]:   # only count it if a stream URL was actually stored
                done.add((key, int(m.group(2)), m.group(1)))
    return done


# ── Main ──────────────────────────────────────────────────────────────────────

print(f"Reading URL list from : {URL_LIST_FILE}")
with open(URL_LIST_FILE, "r", encoding="utf-8") as f:
    all_urls = [l.strip() for l in f if l.strip().startswith("http")]
print(f"Total URLs in file    : {len(all_urls)}")

# Load state
processed_urls  = load_text_set(PROCESSED_FILE)
no_media_set    = load_text_set(NO_MEDIA_FILE)
media_found_set = load_text_set(MEDIA_FOUND_FILE)
anime_map       = load_anime_map(OUTPUT_FILE)
json_done       = processed_urls_from_map(anime_map)  # set of (key, ep, type) tuples

# Determine pending URLs:
# A URL is pending if it was never logged to PROCESSED_FILE, NO_MEDIA_FILE,
# MEDIA_FOUND_FILE, AND it has not already been stored in the JSON map.
def is_done(url):
    if url in processed_urls or url in no_media_set or url in media_found_set:
        return True
    parsed = parse_watch_url(url)
    if parsed and (parsed["anime_key"], parsed["episode"], parsed["media_type"]) in json_done:
        return True
    return False

pending = [u for u in all_urls if not is_done(u)]

print(f"Already in text logs  : {len(processed_urls | no_media_set | media_found_set)}")
print(f"Anime entries in JSON : {len(anime_map)}")
print(f"Pending               : {len(pending)}")
print(f"Limit this run        : {LIMIT}")
print("-" * 60)

to_scrape = pending[:LIMIT]
if not to_scrape:
    print("Nothing to scrape — all URLs already done.")
    sys.exit(0)

# ── Scrape loop ───────────────────────────────────────────────────────────────
success_count  = 0
no_media_count = 0
error_count    = 0

# Track serial_no counter (continue from existing max)
next_serial = max((e.get("serial_no", 0) for e in anime_map.values()), default=0) + 1

for idx, url in enumerate(to_scrape, 1):
    print(f"[{idx}/{len(to_scrape)}] {url}")

    # Parse URL to know what we're fetching
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
    media_type = parsed["media_type"]   # "dub" or "sub"

    print(f"  title={title!r}  ep={episode}  type={media_type}")

    # ── Fetch page via FlareSolverr ───────────────────────────────────────────
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

    # ── Extract iframes ───────────────────────────────────────────────────────
    # Look for iframe src attributes — these are the actual stream embed URLs
    iframe_srcs = re.findall(
        r'<iframe[^>]+src=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE
    )

    # Some pages may embed the stream URL in a data-src attribute instead
    if not iframe_srcs:
        iframe_srcs = re.findall(
            r'<iframe[^>]+data-src=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE
        )

    if not iframe_srcs:
        print(f"  NO MEDIA (CF={cf_status}) → {NO_MEDIA_FILE}")
        append_line(NO_MEDIA_FILE, url)
        append_line(PROCESSED_FILE, url)
        no_media_count += 1
        continue

    # ── Use first valid stream URL ────────────────────────────────────────────
    # Filter out common non-stream iframes (ads, social widgets, etc.)
    stream_url = None
    for src in iframe_srcs:
        # Accept only URLs that look like video embeds
        # Reject javascript:, about:blank, relative paths starting with /
        # that point to the same domain navigation, google ads, etc.
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

    # ── Update in-memory anime_map ────────────────────────────────────────────
    if anime_key not in anime_map:
        # First time we see this anime — create entry with a new serial_no
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

    media_key = f"media_url_{media_type}_ep_{episode}"
    anime_map[anime_key][media_key] = stream_url

    append_line(MEDIA_FOUND_FILE, url)
    append_line(PROCESSED_FILE, url)
    success_count += 1

    # Persist after every successful URL so a crash doesn't lose progress
    save_anime_map(OUTPUT_FILE, anime_map)

    time.sleep(1)

# ── Final JSON save (cleans up _new flags) ────────────────────────────────────
for entry in anime_map.values():
    entry.pop("_new", None)
save_anime_map(OUTPUT_FILE, anime_map)

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"Run complete.")
print(f"  Scraped this run   : {len(to_scrape)}")
print(f"  Media found        : {success_count}")
print(f"  No media           : {no_media_count}")
print(f"  Errors             : {error_count}")
print(f"  Total anime in JSON: {len(anime_map)}")

if error_count > 0 and error_count == len(to_scrape):
    print("ERROR: Every URL errored out.")
    sys.exit(1)
