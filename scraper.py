import requests
import re
import json
import os
import sys

FLARESOLVERR = "http://localhost:8191/v1"
URL = os.environ.get("TARGET_URL", "https://anihq.cc/watch/naruto-shippuuden-episode-500-english-dubbed/")

print(f"Target URL: {URL}")
print(f"FlareSolverr: {FLARESOLVERR}")
print("-" * 60)

try:
    resp = requests.post(FLARESOLVERR, json={
        "cmd": "request.get",
        "url": URL,
        "maxTimeout": 60000,
    }, timeout=90)
    resp.raise_for_status()
except requests.exceptions.RequestException as e:
    print(f"ERROR: Failed to reach FlareSolverr — {e}")
    sys.exit(1)

data = resp.json()

status = data.get("status", "unknown")
cf_status = data.get("solution", {}).get("status", "unknown")
html = data.get("solution", {}).get("response", "")

print(f"FlareSolverr status : {status}")
print(f"CF bypass status    : {cf_status}")

if status != "ok":
    print("ERROR: FlareSolverr did not return ok status.")
    print(json.dumps(data, indent=2))
    sys.exit(1)

# --- Extract iframes ---
iframes = re.findall(r'<iframe[^>]+>', html, re.IGNORECASE)
print(f"\nIframes found: {len(iframes)}")
for tag in iframes:
    print(" ", tag)

# --- Extract stream URLs ---
streams = re.findall(
    r'https?://[^\s\'"<>]+(?:\.m3u8|\.mp4|embed|player|stream)[^\s\'"<>]*',
    html, re.IGNORECASE
)
print(f"\nStream URLs found: {len(streams)}")
for s in streams:
    print(" ", s)

# --- Save results as JSON artifact ---
results = {
    "target_url": URL,
    "flaresolverr_status": status,
    "cf_status": cf_status,
    "iframes": iframes,
    "stream_urls": streams,
}

with open("results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\nResults saved to results.json")

if not streams and not iframes:
    print("\nWARNING: Nothing found — the page may require JS rendering beyond FlareSolverr's capability.")
    sys.exit(1)
