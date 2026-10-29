import os
import sys
import re
import time
import json
import hashlib
import html
import urllib.request
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

# Ensure line buffering for logs in Docker / Render
sys.stdout.reconfigure(line_buffering=True)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "history.json")
QUEUE_FILE = os.path.join(os.path.dirname(__file__), "queue.json")
QUEUE_DIR = os.path.join(os.path.dirname(__file__), "images_queue")

os.makedirs(QUEUE_DIR, exist_ok=True)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

BOT_TOKEN = config["bot_token"]
TARGET_CHANNEL = config["target_channel"]
SOURCE_CHANNELS = config["source_channels"]
SCHEDULE_MINUTES = config.get("schedule_minutes", [5, 25, 45])
AD_KEYWORDS = [kw.lower() for kw in config.get("ad_keywords", [])]

# History
h_data = {}
if os.path.exists(HISTORY_FILE):
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            h_data = json.load(f)
    except Exception:
        h_data = {}

last_post_ids = h_data.get("last_post_ids", {})
history_hashes = set(h_data.get("image_hashes", []))

# Queue
queue_items = []
if os.path.exists(QUEUE_FILE):
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            queue_items = json.load(f)
    except Exception:
        queue_items = []


def save_history():
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "last_post_ids": last_post_ids,
                "image_hashes": list(history_hashes)[-5000:],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def save_queue():
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue_items, f, ensure_ascii=False, indent=2)


def is_ad(text: str) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(kw in lower for kw in AD_KEYWORDS)


def send_telegram_photo(caption: str, file_path: str) -> bool:
    if not os.path.exists(file_path):
        return False
    with open(file_path, "rb") as f:
        image_bytes = f.read()

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    boundary = "----Boundary" + hashlib.md5(str(time.time()).encode()).hexdigest()

    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TARGET_CHANNEL}\r\n".encode("utf-8"))
    if caption:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption[:1000]}\r\n".encode("utf-8"))
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"photo.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode("utf-8"))
    parts.append(image_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        url,
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("ok", False)
    except Exception as e:
        print(f"    [!] Telegram error: {e}", flush=True)
        return False


def scrape_channels():
    now_utc = datetime.now(timezone.utc)

    for channel in SOURCE_CHANNELS:
        url = f"https://t.me/s/{channel}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            continue

        posts = re.findall(
            r'<div class="tgme_widget_message\b[^"]*"[^>]*data-post="([^"]+)"([\s\S]*?)(?=<div class="tgme_widget_message\b|$)',
            content
        )
        if not posts:
            continue

        page_ids = []
        for post_id, _ in posts:
            m = re.search(r'/(\d+)$', post_id)
            if m:
                page_ids.append(int(m.group(1)))
        
        if not page_ids:
            continue
        max_page_id = max(page_ids)

        # Baseline initialization: if channel seen for first time, look back only up to 2 hours
        if channel not in last_post_ids:
            # Find posts older than 2 hours as baseline
            baseline_id = max_page_id
            for post_id, block in reversed(posts):
                time_match = re.search(r'<time datetime="([^"]+)"', block)
                if time_match:
                    try:
                        p_time = datetime.fromisoformat(time_match.group(1).replace("Z", "+00:00"))
                        age_seconds = (now_utc - p_time).total_seconds()
                        if age_seconds < 7200: # within last 2 hours
                            m = re.search(r'/(\d+)$', post_id)
                            if m:
                                baseline_id = int(m.group(1)) - 1
                    except Exception:
                        pass
            last_post_ids[channel] = baseline_id
            save_history()
            print(f"[INIT] @{channel} baseline set to #{baseline_id} (monitoring new posts)", flush=True)

        last_seen = last_post_ids[channel]

        for post_id, block in posts:
            m = re.search(r'/(\d+)$', post_id)
            if not m:
                continue
            num_id = int(m.group(1))
            
            # Skip old history
            if num_id <= last_seen:
                continue

            photos = re.findall(r"background-image:url\('([^']+)'\)", block)
            if not photos:
                continue

            text_match = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>', block)
            text = ""
            if text_match:
                raw_text = text_match.group(1)
                raw_text = re.sub(r'<br\s*/?>', '\n', raw_text)
                text = re.sub(r'<[^>]+>', '', raw_text)
                text = html.unescape(text).strip()

            if is_ad(text):
                print(f"    [!] Skipped Ad: {post_id}", flush=True)
                continue

            for p_url in photos:
                try:
                    img_req = urllib.request.Request(p_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(img_req, timeout=15) as img_resp:
                        img_bytes = img_resp.read()
                    if len(img_bytes) < 2000:
                        continue

                    img_hash = hashlib.md5(img_bytes).hexdigest()
                    if img_hash in history_hashes:
                        continue

                    file_path = os.path.join(QUEUE_DIR, f"{img_hash}.jpg")
                    with open(file_path, "wb") as f_out:
                        f_out.write(img_bytes)

                    queue_items.append({
                        "file_path": file_path,
                        "caption": text,
                        "post_id": post_id,
                        "hash": img_hash
                    })
                    history_hashes.add(img_hash)
                    save_queue()
                    save_history()
                    print(f"    [+ NEW MEME] Captured #{num_id} from @{channel}! In queue: {len(queue_items)}", flush=True)
                except Exception as ex:
                    print(f"    [!] Download error: {ex}", flush=True)

        if max_page_id > last_seen:
            last_post_ids[channel] = max_page_id
            save_history()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Telegram Bot is running healthy!\n")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def keep_alive_worker():
    """Pings self URL every 10 minutes to prevent Render Free tier from spinning down."""
    # Render provides RENDER_EXTERNAL_URL automatically, fallback to known service name
    url = os.environ.get("RENDER_EXTERNAL_URL", "https://telegram-meme-reposter.onrender.com")
    time.sleep(30) # wait for server to start
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                pass
        except Exception:
            pass
        time.sleep(600) # every 10 minutes


def main():
    # 1. Start HTTP server for Render health check
    threading.Thread(target=run_health_server, daemon=True).start()

    # 2. Start Keep-Alive self-pinger to prevent sleeping
    threading.Thread(target=keep_alive_worker, daemon=True).start()

    print(f"=== Scheduled Reposter (NEW MEMES ONLY) for {TARGET_CHANNEL} Started ===", flush=True)
    print(f"Schedule: every hour at :05, :25, :45. In queue: {len(queue_items)}", flush=True)
    
    last_slot = ""
    last_scrape = 0

    while True:
        now = datetime.now()
        minute = now.minute
        slot_id = now.strftime("%Y-%m-%d %H:") + f"{minute:02d}"

        # Scheduled post trigger
        if minute in SCHEDULE_MINUTES:
            if last_slot != slot_id:
                last_slot = slot_id
                print(f"\n[{now.strftime('%H:%M:%S')}] Slot :{minute:02d} triggered!", flush=True)
                if queue_items:
                    item = queue_items.pop(0)
                    save_queue()
                    print(f"    [POSTING] Sending to {TARGET_CHANNEL}...", flush=True)
                    ok = send_telegram_photo(item["caption"], item["file_path"])
                    if ok:
                        print(f"    [SUCCESS] Published! Remaining: {len(queue_items)}", flush=True)
                        if os.path.exists(item["file_path"]):
                            os.remove(item["file_path"])
                    else:
                        print("    [!] Failed to send. Restoring to queue.", flush=True)
                        queue_items.insert(0, item)
                        save_queue()
                else:
                    print(f"    [QUEUE EMPTY] Waiting for fresh memes...", flush=True)

        # Scrape channels every 45s
        if time.time() - last_scrape > 45:
            last_scrape = time.time()
            scrape_channels()

        time.sleep(5)


if __name__ == "__main__":
    main()
