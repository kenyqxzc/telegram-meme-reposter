import os
import sys
import re
import time
import json
import uuid
import socket
import platform
import hashlib
import html
import urllib.request
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

# Ensure immediate logging in Render / Docker
sys.stdout.reconfigure(line_buffering=True)

INSTANCE_ID = uuid.uuid4().hex[:8].upper()
START_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
ADMIN_CHAT_ID = 1248790119  # @Truekent chat ID

last_sent_errors = {}
last_update_id = 0

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


def send_admin_message(text: str):
    """Sends a direct message to @Truekent."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": ADMIN_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception:
        pass


def notify_error(brief_error: str):
    now = time.time()
    last_time = last_sent_errors.get(brief_error, 0)
    if now - last_time < 900:
        return
    last_sent_errors[brief_error] = now
    text = f"⚠️ [Ошибка бота {INSTANCE_ID}]:\n{brief_error}"
    print(f"[ALERT] {text}", flush=True)
    send_admin_message(text)


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
            if not data.get("ok", False):
                notify_error(f"Не удалось отправить фото в {TARGET_CHANNEL}: {data.get('description')}")
                return False
            return True
    except Exception as e:
        print(f"    [!] Telegram send error: {e}", flush=True)
        return False


def scrape_channels():
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

        # Baseline: strictly start from the latest post on page. NEVER look back into history!
        if channel not in last_post_ids:
            last_post_ids[channel] = max_page_id
            save_history()
            print(f"[INIT] @{channel} baseline set to #{max_page_id} (ignoring history)", flush=True)
            continue

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
                except Exception:
                    pass

        if max_page_id > last_seen:
            last_post_ids[channel] = max_page_id
            save_history()


def handle_admin_commands():
    """Listens for /where, /status, /stop commands from admin."""
    global last_update_id
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=2"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for update in data.get("result", []):
            last_update_id = update["update_id"]
            msg = update.get("message", {})
            sender = msg.get("from", {})
            if sender.get("id") == ADMIN_CHAT_ID:
                cmd = msg.get("text", "").strip().lower()
                hostname = socket.gethostname()
                os_info = f"{platform.system()} {platform.release()}"
                
                if cmd in ["/where", "/status", "где ты?", "статус"]:
                    info = (
                        f"🤖 Копия бота: {INSTANCE_ID}\n"
                        f"🖥️ Хост/Сервер: {hostname}\n"
                        f"⚙️ Система: {os_info}\n"
                        f"⏱️ Запущен: {START_TIME}\n"
                        f"📦 В очереди: {len(queue_items)} мемов"
                    )
                    send_admin_message(info)
                elif cmd in ["/stop", "/kill"]:
                    send_admin_message(f"🛑 Копия бота {INSTANCE_ID} на сервере {hostname} остановлена!")
                    print(f"[SHUTDOWN] Terminated by admin command from {hostname}", flush=True)
                    os._exit(0)
    except Exception:
        pass


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"Telegram Bot {INSTANCE_ID} is alive on {socket.gethostname()}!\n".encode("utf-8"))

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def keep_alive_worker():
    url = os.environ.get("RENDER_EXTERNAL_URL", "https://telegram-meme-reposter.onrender.com")
    time.sleep(30)
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                pass
        except Exception:
            pass
        time.sleep(600)


def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    threading.Thread(target=keep_alive_worker, daemon=True).start()

    print(f"=== Scheduled Reposter [{INSTANCE_ID}] for {TARGET_CHANNEL} Started ===", flush=True)
    print(f"Host: {socket.gethostname()} ({platform.system()})", flush=True)
    print(f"Schedule: :05, :25, :45. In queue: {len(queue_items)}", flush=True)

    # Greet admin on startup
    send_admin_message(f"🚀 Запущена копия бота [{INSTANCE_ID}] на сервере {socket.gethostname()} ({platform.system()})")

    last_slot = ""
    last_scrape = 0
    last_cmd_check = 0

    while True:
        now = datetime.now()
        minute = now.minute
        slot_id = now.strftime("%Y-%m-%d %H:") + f"{minute:02d}"

        # Check admin commands every 5s
        if time.time() - last_cmd_check > 5:
            last_cmd_check = time.time()
            handle_admin_commands()

        # Scheduled post trigger
        if minute in SCHEDULE_MINUTES:
            if last_slot != slot_id:
                last_slot = slot_id
                print(f"\n[{now.strftime('%H:%M:%S')}] Slot :{minute:02d} triggered in {INSTANCE_ID}!", flush=True)
                if queue_items:
                    item = queue_items.pop(0)
                    save_queue()

                    print(f"    [POSTING] Sending meme to {TARGET_CHANNEL}...", flush=True)
                    ok = send_telegram_photo(item["caption"], item["file_path"])
                    if ok:
                        print(f"    [SUCCESS] Published! Remaining: {len(queue_items)}", flush=True)
                    else:
                        print(f"    [FAILED] Could not send meme.", flush=True)
                    
                    # Always clean up file - NEVER re-insert to avoid duplicates!
                    if os.path.exists(item["file_path"]):
                        try:
                            os.remove(item["file_path"])
                        except Exception:
                            pass
                else:
                    print(f"    [QUEUE EMPTY] Waiting for fresh memes...", flush=True)

        # Scrape channels every 45s
        if time.time() - last_scrape > 45:
            last_scrape = time.time()
            scrape_channels()

        time.sleep(5)


if __name__ == "__main__":
    main()
