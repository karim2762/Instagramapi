import json
import os
import random
import re
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

# ===== CONFIG =====
DELAY_MIN = 0.5   # seconds (kept short because Vercel has time limits)
DELAY_MAX = 1.0
TIMEOUT = 15      # seconds per request
API_KEY = os.environ.get("API_KEY", "")  # optional: set in Vercel settings

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

VALID_PATTERNS = [
    r"^https?://(www\.)?instagram\.com/[^/]+/?$",                   # profile
    r"^https?://(www\.)?instagram\.com/p/[^/]+/?$",                 # post
    r"^https?://(www\.)?instagram\.com/reel/[^/]+/?$",              # reel
    r"^https?://(www\.)?instagram\.com/stories/[^/]+/[^/]+/?$",     # story
]


def delay():
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def extract_js_var(name, source):
    m = re.search(re.escape(name) + r'\s*=\s*"([^"]+)"', source)
    return m.group(1) if m else None


def parse_media(html):
    soup = BeautifulSoup(html, "html.parser")
    result = {"results_images": 0, "results_videos": 0, "images": [], "videos": []}

    for ul in soup.find_all("ul", class_="download-box"):
        for li in ul.find_all("li", recursive=False):
            # Thumbnail
            thumb = None
            thumb_node = li.select_one('div[class*="download-items__thumb"] img')
            if thumb_node:
                src = thumb_node.get("src", "")
                data_src = thumb_node.get("data-src", "")
                if not src or src == "/imgs/loader.gif":
                    thumb = data_src or src
                else:
                    thumb = src

            # Icon (image or video)
            is_image = False
            is_video = False
            icon = li.select_one('div[class*="download-items__thumb"] i')
            if icon:
                icon_class = " ".join(icon.get("class", []))
                is_image = "icon-dlimage" in icon_class or icon.has_attr("icon-dlimage")
                is_video = "icon-dlvideo" in icon_class or icon.has_attr("icon-dlvideo")

            # Download buttons
            btns = li.select('div[class*="download-items__btn"] a')
            video_href = None
            for a in btns:
                if a.has_attr("video"):
                    video_href = a.get("href")
                    is_video = True
                    break
                if "download video" in a.get_text(strip=True).lower():
                    video_href = a.get("href")
                    is_video = True
                    break
            if is_video and not video_href and btns:
                video_href = btns[0].get("href")

            # Resolutions
            resolutions = []
            for opt in li.select('div[class*="photo-option"] select option'):
                resolutions.append({opt.get_text(strip=True): opt.get("value")})

            if is_image:
                result["results_images"] += 1
                result["images"].append({
                    "thumb_url": thumb,
                    "resolutions_count": len(resolutions),
                    "resolution": resolutions,
                })
            elif is_video:
                result["results_videos"] += 1
                result["videos"].append({
                    "thumb_url": thumb,
                    "video_src": video_href,
                    "resolutions_count": len(resolutions),
                    "resolution": resolutions,
                })
    return result


def fetch_media(target_url):
    s = requests.Session()
    base_headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

    # STEP 1: get tokens from saveinsta.to
    r = s.get(
        "https://saveinsta.to/en/highlights",
        headers={**base_headers,
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "Referer": "https://www.google.com/"},
        timeout=TIMEOUT,
    )
    m = re.search(r'<script[^>]*>var\s+k_url_search="[^"]+"(.*?)</script>', r.text, re.S)
    if not m:
        return 502, {"error": "JS token block not found (source site may be blocking this server)"}
    block = m.group(1)
    k_exp = extract_js_var("k_exp", block)
    k_token = extract_js_var("k_token", block)

    delay()

    # STEP 2: get CF token
    ajax_headers = {
        **base_headers,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://saveinsta.to",
        "X-Requested-With": "XMLHttpRequest",
    }
    r = s.post(
        "https://saveinsta.to/api/userverify",
        data={"url": target_url},
        headers={**ajax_headers, "Referer": "https://saveinsta.to/en/video"},
        timeout=TIMEOUT,
    )
    try:
        cftoken = r.json().get("token")
    except ValueError:
        cftoken = None
    if not cftoken:
        return 502, {"error": "CF token not returned"}

    delay()

    # STEP 3: final request
    r = s.post(
        "https://saveinsta.to/api/ajaxSearch",
        data={
            "k_exp": k_exp,
            "k_token": k_token,
            "q": target_url,
            "t": "media",
            "lang": "en",
            "v": "v2",
            "cftoken": cftoken,
        },
        headers={**ajax_headers, "Referer": "https://saveinsta.to/en/highlights"},
        timeout=TIMEOUT,
    )
    try:
        data = r.json()
    except ValueError:
        return 502, {"error": "Invalid response from source site"}

    if data.get("status") == "ok" and data.get("data"):
        return 200, {"success": True, "media": parse_media(data["data"])}
    return 502, {"error": "Invalid response", "raw": data}


class handler(BaseHTTPRequestHandler):
    def _send(self, status, payload):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)

        # Optional API key check
        if API_KEY and qs.get("key", [""])[0] != API_KEY:
            return self._send(401, {"error": "Unauthorized"})

        url = (qs.get("url", [""])[0]).strip()
        if not url:
            return self._send(400, {"error": "Missing 'url' parameter"})
        if not re.match(r"^https?://(www\.)?instagram\.com/.*", url, re.I):
            return self._send(400, {"error": "URL must be a valid Instagram link"})
        if not any(re.match(p, url, re.I) for p in VALID_PATTERNS):
            return self._send(400, {"error": "Only profile, post, reel, or story URLs are supported."})

        try:
            status, payload = fetch_media(url)
        except requests.RequestException as e:
            status, payload = 502, {"error": f"Request failed: {e}"}
        except Exception as e:
            status, payload = 500, {"error": f"Server error: {e}"}
        self._send(status, payload)
