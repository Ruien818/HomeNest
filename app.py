import os
import re
import json
import shlex
import shutil
import sqlite3
import datetime
import platform
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from datetime import datetime as dt_parser
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import html
import random
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as dt_parser, timezone
from email.utils import parsedate_to_datetime
from flask import request, jsonify, render_template, session
import ssl
import time

app = Flask(__name__)
app.secret_key = "homenest-family-secret-key-change-me"

NEWS_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
_NEWS_CACHE = {}
_NEWS_CACHE_TTL = 300  # 5 minutes

_STOCK_CACHE = {}
_STOCK_CACHE_TTL = 60

CITY_COORDS = {
    "candiac": (45.38, -73.51),
    "montreal": (45.50, -73.56),
    "laval": (45.57, -73.75),
    "toronto": (43.65, -79.38),
    "washington": (38.90, -77.03),
    "chicago": (41.87, -87.62),
    "london": (51.50, -0.12),
    "tokyo": (35.67, 139.65),
    "paris": (48.85, 2.35),
}

DIRECT_FEEDS = {
    "tech": [
        ("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/uk/technology/rss"),
        ("CBC Tech", "https://www.cbc.ca/webfeed/rss/rss-technology"),
    ],
    "stocks": [
        ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
        ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("CNBC Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069"),
    ],
    "economy": [
        ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/uk/business/rss"),
        ("CBC Business", "https://www.cbc.ca/webfeed/rss/rss-business"),
    ],
    "best": [
        ("The Guardian Culture", "https://www.theguardian.com/uk/culture/rss"),
        ("NPR Culture & Life", "https://feeds.npr.org/1008/rss.xml"),
    ],
    "world": [
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/world/rss"),
        ("CBC World", "https://www.cbc.ca/webfeed/rss/rss-world"),
    ],
    "local_qc": [
        ("CBC Montreal", "https://www.cbc.ca/webfeed/rss/rss-canada-montreal"),
        ("CBC Canada", "https://www.cbc.ca/webfeed/rss/rss-canada"),
    ],
}

DEFAULT_TICKERS = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "NASDAQ"),
    ("^DJI", "DOW"),
    ("^GSPTSE", "TSX"),
    ("AAPL", "AAPL"),
    ("NVDA", "NVDA"),
    ("MSFT", "MSFT"),
    ("GC=F", "GOLD"),
    ("CL=F", "OIL"),
]

POPULAR_STOCKS = [
    {"symbol": "AAPL", "name": "Apple Inc."},
    {"symbol": "MSFT", "name": "Microsoft Corporation"},
    {"symbol": "NVDA", "name": "NVIDIA Corporation"},
    {"symbol": "GOOGL", "name": "Alphabet Inc."},
    {"symbol": "AMZN", "name": "Amazon.com Inc."},
    {"symbol": "META", "name": "Meta Platforms Inc."},
    {"symbol": "TSLA", "name": "Tesla Inc."},
    {"symbol": "SHOP.TO", "name": "Shopify Inc. (TSX)"},
    {"symbol": "RY.TO", "name": "Royal Bank of Canada (TSX)"},
    {"symbol": "TD.TO", "name": "Toronto-Dominion Bank (TSX)"},
    {"symbol": "ENB.TO", "name": "Enbridge Inc. (TSX)"},
]


def _urlopen_safe(url, timeout=7):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": NEWS_USER_AGENT,
            "Accept": "application/rss+xml, application/xml, application/json, text/xml;q=0.9, */*;q=0.8",
        },
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except Exception as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc) or isinstance(getattr(exc, "reason", None), ssl.SSLError):
            ctx = ssl._create_unverified_context()
            return urllib.request.urlopen(req, timeout=timeout, context=ctx)
        raise


def _clean_text(raw):
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_timestamp(raw_date):
    if not raw_date:
        return dt_parser.now(timezone.utc)
    s = str(raw_date).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = dt_parser.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return dt_parser.now(timezone.utc)


def _format_time_ago(dt):
    diff = max(0, int((dt_parser.now(timezone.utc) - dt).total_seconds()))
    if diff < 60:
        return "Just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def _extract_rss_image(item, raw_desc):
    for child in item:
        tag = child.tag.lower()
        if tag.endswith("content") or tag.endswith("thumbnail") or tag.endswith("enclosure"):
            url = child.attrib.get("url", "")
            media_type = child.attrib.get("type", "")
            if url and (not media_type or media_type.startswith("image") or tag.endswith("thumbnail")):
                return url
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw_desc or "", re.IGNORECASE)
    return match.group(1) if match else ""


def _build_article(title, summary, source, category, pub_raw, url, image=""):
    clean_title = _clean_text(title)
    clean_summary = _clean_text(summary)
    if not clean_title or not url:
        return None
    if len(clean_summary) > 340:
        clean_summary = clean_summary[:337].rsplit(" ", 1)[0] + "..."
    dt = _parse_timestamp(pub_raw)
    words = len((clean_title + " " + clean_summary).split())
    read_mins = max(2, min(8, round(words / 45) + 2))
    return {
        "title": clean_title,
        "summary": clean_summary or "Open the original dispatch to read the full report.",
        "source": _clean_text(source) or "Wire Desk",
        "category": category,
        "time_ago": _format_time_ago(dt),
        "read_time": f"{read_mins} min read",
        "url": url.strip(),
        "image": image or "",
        "_ts": dt.timestamp(),
    }


def _fetch_single_rss(source_name, feed_url, category):
    articles = []
    with _urlopen_safe(feed_url, timeout=7) as resp:
        xml_bytes = resp.read()
    root = ET.fromstring(xml_bytes)

    for item in root.findall(".//item")[:12]:
        raw_desc = item.findtext("description") or ""
        img = _extract_rss_image(item, raw_desc)
        art = _build_article(
            item.findtext("title"),
            raw_desc,
            source_name,
            category,
            item.findtext("pubDate"),
            item.findtext("link"),
            img,
        )
        if art:
            articles.append(art)
    return articles


def _fetch_keyless_json_extras(category, query):
    articles = []
    try:
        if query:
            url = "https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=10&query=" + urllib.parse.quote(query)
            with _urlopen_safe(url, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for hit in data.get("hits", []):
                story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                art = _build_article(
                    hit.get("title"),
                    f"Community discussion ({hit.get('num_comments', 0)} comments, {hit.get('points', 0)} points).",
                    "Hacker News",
                    category,
                    hit.get("created_at"),
                    story_url,
                )
                if art:
                    articles.append(art)
        elif category in ("Science", "Technology"):
            url = "https://api.spaceflightnewsapi.net/v4/articles/?limit=8"
            with _urlopen_safe(url, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for item in data.get("results", []):
                art = _build_article(
                    item.get("title"),
                    item.get("summary"),
                    item.get("news_site") or "Spaceflight News",
                    category,
                    item.get("published_at"),
                    item.get("url"),
                    item.get("image_url"),
                )
                if art:
                    articles.append(art)
    except Exception:
        pass
    return articles


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if os.path.basename(BASE_DIR).lower() == "homenest":
    HOMENEST_ROOT = BASE_DIR
else:
    HOMENEST_ROOT = os.path.join(BASE_DIR, "HomeNest")

STORAGE_DIR = os.path.join(HOMENEST_ROOT, "files")
DB_PATH = os.path.join(BASE_DIR, "homenest.db")
LM_STUDIO_BASE = "http://localhost:1234"
LM_STUDIO_URL = f"{LM_STUDIO_BASE}/v1/chat/completions"
LM_STUDIO_MODELS_URL = f"{LM_STUDIO_BASE}/v1/models"

os.makedirs(STORAGE_DIR, exist_ok=True)

def ensure_calendar_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            event_date TEXT NOT NULL,
            start_time TEXT DEFAULT '',
            end_time TEXT DEFAULT '',
            category TEXT DEFAULT 'family',
            visibility TEXT DEFAULT 'shared',
            shared_with TEXT DEFAULT '',
            repeat_rule TEXT DEFAULT 'none',
            repeat_until TEXT DEFAULT '',
            location TEXT DEFAULT '',
            is_completed INTEGER DEFAULT 0,
            completed_dates TEXT DEFAULT '',
            priority TEXT DEFAULT 'medium',
            subtasks TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    existing_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(calendar_events)").fetchall()
    }
    for col, col_def in (
        ("shared_with", "TEXT DEFAULT ''"),
        ("repeat_rule", "TEXT DEFAULT 'none'"),
        ("repeat_until", "TEXT DEFAULT ''"),
        ("location", "TEXT DEFAULT ''"),
        ("is_completed", "INTEGER DEFAULT 0"),
        ("completed_dates", "TEXT DEFAULT ''"),
        ("priority", "TEXT DEFAULT 'medium'"),
        ("subtasks", "TEXT DEFAULT '[]'"),
    ):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE calendar_events ADD COLUMN {col} {col_def}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            ai_safety_enabled INTEGER DEFAULT 1,
            updated_by TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO calendar_settings (id, ai_safety_enabled) VALUES (1, 1)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS safety_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_app TEXT DEFAULT 'Calendar',
            user_id INTEGER,
            username TEXT,
            content TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/drive")
def drive_page():
    return render_template("drive.html")


@app.route("/dictionary")
def dictionary_page():
    return render_template("dictionary.html")

@app.route("/translator")
def translator_page():
    return render_template("translator.html")

@app.route("/news")
def news_page():
    return render_template("news.html")

@app.route("/calendar")
def calendar_page():
    return render_template("calendar.html")

@app.route("/reminders")
def reminder_page():
    return render_template("reminders.html")

@app.route("/weather")
def weather_page():
    return render_template("weather.html")


# --- DATABASE SETUP & AUTO-MIGRATION ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'kid',
            sensitive_scan INTEGER NOT NULL DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            parent_id INTEGER,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#fbbf24',
            is_trashed INTEGER NOT NULL DEFAULT 0,
            trashed_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            folder_id INTEGER,
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            scan_status TEXT NOT NULL DEFAULT 'Clean',
            scan_details TEXT NOT NULL DEFAULT 'Passed all active safety checks.',
            is_trashed INTEGER NOT NULL DEFAULT 0,
            trashed_at TEXT,
            uploaded_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'info',
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS desktop_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_type TEXT NOT NULL DEFAULT 'file',
            item_id INTEGER NOT NULL,
            pos_x INTEGER NOT NULL DEFAULT 28,
            pos_y INTEGER NOT NULL DEFAULT 28
        )
    """)

    file_cols = [row["name"] for row in c.execute("PRAGMA table_info(files)").fetchall()]
    if "folder_id" not in file_cols:
        c.execute("ALTER TABLE files ADD COLUMN folder_id INTEGER")
    if "scan_details" not in file_cols:
        c.execute("ALTER TABLE files ADD COLUMN scan_details TEXT NOT NULL DEFAULT 'Passed all active safety checks.'")
    if "is_trashed" not in file_cols:
        c.execute("ALTER TABLE files ADD COLUMN is_trashed INTEGER NOT NULL DEFAULT 0")
    if "trashed_at" not in file_cols:
        c.execute("ALTER TABLE files ADD COLUMN trashed_at TEXT")

    folder_cols = [row["name"] for row in c.execute("PRAGMA table_info(folders)").fetchall()]
    if "is_trashed" not in folder_cols:
        c.execute("ALTER TABLE folders ADD COLUMN is_trashed INTEGER NOT NULL DEFAULT 0")
    if "trashed_at" not in folder_cols:
        c.execute("ALTER TABLE folders ADD COLUMN trashed_at TEXT")

    desk_cols = [row["name"] for row in c.execute("PRAGMA table_info(desktop_items)").fetchall()]
    if "item_type" not in desk_cols and "file_id" in desk_cols:
        c.execute("""
            CREATE TABLE desktop_items_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_type TEXT NOT NULL DEFAULT 'file',
                item_id INTEGER NOT NULL,
                pos_x INTEGER NOT NULL DEFAULT 28,
                pos_y INTEGER NOT NULL DEFAULT 28
            )
        """)
        c.execute("""
            INSERT INTO desktop_items_new (id, user_id, item_type, item_id, pos_x, pos_y)
            SELECT id, user_id, 'file', file_id, pos_x, pos_y FROM desktop_items
        """)
        c.execute("DROP TABLE desktop_items")
        c.execute("ALTER TABLE desktop_items_new RENAME TO desktop_items")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            event_date TEXT NOT NULL,
            start_time TEXT DEFAULT '',
            end_time TEXT DEFAULT '',
            category TEXT DEFAULT 'family',
            visibility TEXT DEFAULT 'shared',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.commit()
    conn.close()


    init_db()


# --- TEXT EXTRACTION HELPER ---
def extract_readable_text(filepath, filename, limit=12000):
    lower = filename.lower()
    if lower.endswith(".docx"):
        try:
            with zipfile.ZipFile(filepath) as z:
                xml_content = z.read("word/document.xml")
            tree = ET.fromstring(xml_content)
            paragraphs = [elem.text for elem in tree.iter() if elem.text]
            return " ".join(paragraphs)[:limit]
        except Exception:
            return ""
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except Exception:
        return ""


# --- MULTI-ENGINE CONTENT SCANNER ---
def push_notification(user_id, title, message, level="info"):
    conn = get_db()
    conn.execute(
        "INSERT INTO notifications (user_id, title, message, level, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, title, message, level, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    conn.commit()
    conn.close()


def run_sensitive_scan(filename, text_content):
    reasons = []

    rule_groups = [
        ("Threat / Violence", r"\b(kill|murder|stab|shoot|attack|hurt you|harm you|beat you|die|threat|weapon|gun|bomb|violence|gore)\b"),
        ("Self-Harm", r"\b(suicide|self[- ]?harm|kill myself|end my life)\b"),
        ("Explicit / Adult", r"\b(nsfw|porn|explicit|nude|xxx)\b"),
        ("Credentials / Secrets", r"\b(password|secret_key|api_key|private_key)\b"),
        ("PII (SSN / Card)", r"\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]*?){13,16}\b")
    ]
    combined_text = f"{filename}\n{text_content[:5000]}"
    for label, pattern in rule_groups:
        match = re.search(pattern, combined_text, re.IGNORECASE)
        if match:
            reasons.append(f"Python Engine [{label}]: matched '{match.group(0)}'")

    if filename.lower().endswith((".exe", ".bat", ".sh", ".vbs", ".ps1", ".cmd")):
        reasons.append("Heuristic Engine: Executable script detected")

    if text_content.strip():
        try:
            payload = {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are the HomeNest family safety scanner. Check the text for threats, violence, "
                            "bullying, self-harm, personal data leaks, or inappropriate content. "
                            "If ANY risk exists, reply ONLY with 'FLAGGED: <short reason>'. Otherwise reply 'SAFE'."
                        )
                    },
                    {"role": "user", "content": text_content[:2500]}
                ],
                "temperature": 0.0,
                "max_tokens": 60
            }
            resp = requests.post(LM_STUDIO_URL, json=payload, timeout=8)
            if resp.status_code == 200:
                ai_reply = resp.json()["choices"][0]["message"]["content"].strip()
                if "FLAGGED" in ai_reply.upper():
                    reasons.append(f"LM Studio AI: {ai_reply}")
        except Exception:
            pass

    return reasons


def evaluate_file_safety(conn, user, save_path, safe_name):
    scan_status = "Skipped"
    scan_details = "Scanner disabled for this account."

    if user["role"] == "kid" or user["sensitive_scan"] == 1:
        text_preview = extract_readable_text(save_path, safe_name, limit=5000)
        flags = run_sensitive_scan(safe_name, text_preview)
        if flags:
            scan_status = "Flagged"
            scan_details = "; ".join(flags)
            alert_msg = f"File '{safe_name}' triggered alerts: {scan_details}"
            push_notification(user["id"], "HomeNest Safety Alert", alert_msg, "alert")
            parents = conn.execute("SELECT id FROM users WHERE role = 'parent' AND id != ?", (user["id"],)).fetchall()
            for parent in parents:
                push_notification(parent["id"], f"Family Alert ({user['username']})", alert_msg, "alert")
        else:
            scan_status = "Clean"
            scan_details = "Scanned by Python Engine & LM Studio — no threats found."

    return scan_status, scan_details


def save_and_register_file(conn, user, folder_id, raw_filename, content_bytes):
    safe_name = secure_filename(raw_filename) or f"file_{datetime.datetime.now().strftime('%H%M%S')}.txt"
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    stored_name = f"{user['id']}_{timestamp}_{safe_name}"
    save_path = os.path.join(STORAGE_DIR, stored_name)

    with open(save_path, "wb") as f:
        f.write(content_bytes)

    size_bytes = os.path.getsize(save_path)
    scan_status, scan_details = evaluate_file_safety(conn, user, save_path, safe_name)

    cur = conn.execute(
        "INSERT INTO files (owner_id, folder_id, filename, stored_name, size_bytes, scan_status, scan_details, is_trashed, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (user["id"], folder_id, safe_name, stored_name, size_bytes, scan_status, scan_details, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    return cur.lastrowid, safe_name, scan_status


# --- AUTH DECORATOR ---
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated




@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = data.get("role", "kid")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    sensitive_scan = 1 if role == "kid" else int(data.get("sensitive_scan", 1))

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, sensitive_scan) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), role, sensitive_scan)
        )
        conn.commit()
        user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Username already exists"}), 409
    conn.close()

    session["user_id"] = user_id
    push_notification(user_id, "Welcome to HomeNest", f"Your HomeNest space is ready ({role.upper()}).", "info")
    return jsonify({"status": "ok"})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "")

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid credentials"}), 401

    session["user_id"] = user["id"]
    return jsonify({"status": "ok"})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/api/me")
@login_required
def me():
    conn = get_db()
    user = conn.execute("SELECT id, username, role, sensitive_scan FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return jsonify(dict(user))


@app.route("/api/settings/scan", methods=["POST"])
@login_required
def toggle_scan():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    if user["role"] == "kid":
        conn.close()
        return jsonify({"error": "Sensitive Alerts cannot be disabled for Kid accounts."}), 403

    new_val = 1 if request.json.get("enabled") else 0
    conn.execute("UPDATE users SET sensitive_scan = ? WHERE id = ?", (new_val, user["id"]))
    conn.commit()
    conn.close()

    push_notification(
        user["id"],
        "HomeNest Safety Updated",
        f"Sensitive Content Scanner turned {'ON' if new_val else 'OFF'}.",
        "info"
    )
    return jsonify({"sensitive_scan": new_val})


# --- DRIVE: FOLDERS & FILES ---
@app.route("/api/drive", methods=["GET"])
@login_required
def get_drive_contents():
    folder_id = request.args.get("folder_id", type=int)
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    breadcrumbs = []
    curr_id = folder_id
    while curr_id:
        row = conn.execute("SELECT id, name, parent_id FROM folders WHERE id = ? AND is_trashed = 0", (curr_id,)).fetchone()
        if not row:
            break
        breadcrumbs.insert(0, {"id": row["id"], "name": row["name"]})
        curr_id = row["parent_id"]

    if user["role"] == "parent":
        if folder_id is None:
            folders = conn.execute(
                "SELECT folders.*, users.username AS owner FROM folders JOIN users ON folders.owner_id = users.id WHERE parent_id IS NULL AND folders.is_trashed = 0 ORDER BY folders.name ASC"
            ).fetchall()
            files = conn.execute(
                "SELECT files.*, users.username AS owner FROM files JOIN users ON files.owner_id = users.id WHERE folder_id IS NULL AND files.is_trashed = 0 ORDER BY files.id DESC"
            ).fetchall()
        else:
            folders = conn.execute(
                "SELECT folders.*, users.username AS owner FROM folders JOIN users ON folders.owner_id = users.id WHERE parent_id = ? AND folders.is_trashed = 0 ORDER BY folders.name ASC",
                (folder_id,)
            ).fetchall()
            files = conn.execute(
                "SELECT files.*, users.username AS owner FROM files JOIN users ON files.owner_id = users.id WHERE folder_id = ? AND files.is_trashed = 0 ORDER BY files.id DESC",
                (folder_id,)
            ).fetchall()
    else:
        if folder_id is None:
            folders = conn.execute(
                "SELECT folders.*, users.username AS owner FROM folders JOIN users ON folders.owner_id = users.id WHERE owner_id = ? AND parent_id IS NULL AND folders.is_trashed = 0 ORDER BY folders.name ASC",
                (user["id"],)
            ).fetchall()
            files = conn.execute(
                "SELECT files.*, users.username AS owner FROM files JOIN users ON files.owner_id = users.id WHERE owner_id = ? AND folder_id IS NULL AND files.is_trashed = 0 ORDER BY files.id DESC",
                (user["id"],)
            ).fetchall()
        else:
            folders = conn.execute(
                "SELECT folders.*, users.username AS owner FROM folders JOIN users ON folders.owner_id = users.id WHERE owner_id = ? AND parent_id = ? AND folders.is_trashed = 0 ORDER BY folders.name ASC",
                (user["id"], folder_id)
            ).fetchall()
            files = conn.execute(
                "SELECT files.*, users.username AS owner FROM files JOIN users ON files.owner_id = users.id WHERE owner_id = ? AND folder_id = ? AND files.is_trashed = 0 ORDER BY files.id DESC",
                (user["id"], folder_id)
            ).fetchall()

    conn.close()
    return jsonify({
        "breadcrumbs": breadcrumbs,
        "folders": [dict(f) for f in folders],
        "files": [dict(f) for f in files]
    })


def add_desktop_shortcut_internal(conn, user_id, item_type, item_id):
    existing = conn.execute(
        "SELECT id FROM desktop_items WHERE user_id = ? AND item_type = ? AND item_id = ?",
        (user_id, item_type, item_id)
    ).fetchone()
    if existing:
        return
    count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM desktop_items WHERE user_id = ?",
        (user_id,)
    ).fetchone()["cnt"]
    pos_x = 28 + (count // 5) * 112
    pos_y = 28 + (count % 5) * 106
    conn.execute(
        "INSERT INTO desktop_items (user_id, item_type, item_id, pos_x, pos_y) VALUES (?, ?, ?, ?, ?)",
        (user_id, item_type, item_id, pos_x, pos_y)
    )


@app.route("/api/folders", methods=["POST"])
@login_required
def create_folder():
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    color = data.get("color", "#fbbf24").strip()
    parent_id = data.get("parent_id")
    pin_desktop = bool(data.get("pin_desktop", False))

    if not name:
        return jsonify({"error": "Folder name is required"}), 400

    conn = get_db()
    cur = conn.execute(
        "INSERT INTO folders (owner_id, parent_id, name, color, is_trashed, created_at) VALUES (?, ?, ?, ?, 0, ?)",
        (session["user_id"], parent_id, name, color, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    folder_id = cur.lastrowid
    if pin_desktop:
        add_desktop_shortcut_internal(conn, session["user_id"], "folder", folder_id)
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "id": folder_id})


@app.route("/api/folders/<int:folder_id>", methods=["PATCH", "DELETE"])
@login_required
def manage_folder(folder_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    folder = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()

    if not folder or (folder["owner_id"] != user["id"] and user["role"] != "parent"):
        conn.close()
        return jsonify({"error": "Forbidden"}), 403

    if request.method == "PATCH":
        data = request.get_json(silent=True) or {}
        new_name = data.get("name", folder["name"]).strip() or folder["name"]
        new_color = data.get("color", folder["color"]).strip() or folder["color"]
        conn.execute("UPDATE folders SET name = ?, color = ? WHERE id = ?", (new_name, new_color, folder_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "renamed"})

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    trash_folder_recursive(conn, folder_id, now)
    conn.commit()
    conn.close()
    return jsonify({"status": "trashed"})


@app.route("/api/upload", methods=["POST"])
@login_required
def upload_file():
    uploaded_files = request.files.getlist("file")
    folder_id_raw = request.form.get("folder_id", "")
    folder_id = int(folder_id_raw) if folder_id_raw.isdigit() else None
    pin_desktop = request.form.get("pin_desktop") == "1"

    if not uploaded_files or uploaded_files[0].filename == "":
        return jsonify({"error": "No selected file"}), 400

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    results = []

    for file in uploaded_files:
        if not file.filename:
            continue
        new_file_id, safe_name, scan_status = save_and_register_file(
            conn, user, folder_id, file.filename, file.read()
        )
        if pin_desktop:
            add_desktop_shortcut_internal(conn, user["id"], "file", new_file_id)
        results.append({"id": new_file_id, "filename": safe_name, "scan_status": scan_status})

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "uploaded": results})


@app.route("/api/preview/<int:file_id>")
@login_required
def preview_file(file_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    file_row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    conn.close()

    if not file_row:
        return "Not found", 404
    if file_row["owner_id"] != user["id"] and user["role"] != "parent":
        return "Forbidden", 403

    return send_from_directory(STORAGE_DIR, file_row["stored_name"], as_attachment=False)


@app.route("/api/download/<int:file_id>")
@login_required
def download_file(file_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    file_row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    conn.close()

    if not file_row:
        return "Not found", 404
    if file_row["owner_id"] != user["id"] and user["role"] != "parent":
        return "Forbidden", 403

    return send_from_directory(STORAGE_DIR, file_row["stored_name"], as_attachment=True, download_name=file_row["filename"])


@app.route("/api/files/<int:file_id>", methods=["PATCH", "DELETE"])
@login_required
def manage_file(file_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    file_row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    if not file_row or (file_row["owner_id"] != user["id"] and user["role"] != "parent"):
        conn.close()
        return jsonify({"error": "Forbidden"}), 403

    if request.method == "PATCH":
        data = request.get_json(silent=True) or {}
        if "folder_id" in data:
            target_folder = data["folder_id"]
            conn.execute("UPDATE files SET folder_id = ? WHERE id = ?", (target_folder, file_id))
            conn.commit()
            conn.close()
            return jsonify({"status": "moved"})

        new_name = secure_filename(data.get("filename", "").strip())
        if not new_name:
            conn.close()
            return jsonify({"error": "Valid filename required"}), 400
        conn.execute("UPDATE files SET filename = ? WHERE id = ?", (new_name, file_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "renamed"})

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute("UPDATE files SET is_trashed = 1, trashed_at = ? WHERE id = ?", (now, file_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "trashed"})


# --- DESKTOP ITEMS ---
@app.route("/api/desktop", methods=["GET", "POST"])
@login_required
def manage_desktop():
    conn = get_db()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        item_type = data.get("item_type", "file")
        item_id = data.get("item_id")
        if item_type not in ("folder", "file") or not item_id:
            conn.close()
            return jsonify({"error": "Valid item_type and item_id required"}), 400

        add_desktop_shortcut_internal(conn, session["user_id"], item_type, int(item_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "pinned"})

    rows = conn.execute(
        "SELECT * FROM desktop_items WHERE user_id = ? ORDER BY id ASC",
        (session["user_id"],)
    ).fetchall()

    items = []
    for row in rows:
        if row["item_type"] == "folder":
            f = conn.execute(
                "SELECT folders.*, users.username AS owner FROM folders JOIN users ON folders.owner_id = users.id WHERE folders.id = ? AND folders.is_trashed = 0",
                (row["item_id"],)
            ).fetchone()
            if f:
                items.append({
                    "shortcut_id": row["id"],
                    "item_type": "folder",
                    "item_id": f["id"],
                    "name": f["name"],
                    "color": f["color"],
                    "owner": f["owner"],
                    "created_at": f["created_at"],
                    "pos_x": row["pos_x"],
                    "pos_y": row["pos_y"]
                })
        else:
            f = conn.execute(
                "SELECT files.*, users.username AS owner FROM files JOIN users ON files.owner_id = users.id WHERE files.id = ? AND files.is_trashed = 0",
                (row["item_id"],)
            ).fetchone()
            if f:
                items.append({
                    "shortcut_id": row["id"],
                    "item_type": "file",
                    "item_id": f["id"],
                    "name": f["filename"],
                    "size_bytes": f["size_bytes"],
                    "scan_status": f["scan_status"],
                    "scan_details": f["scan_details"],
                    "owner": f["owner"],
                    "uploaded_at": f["uploaded_at"],
                    "pos_x": row["pos_x"],
                    "pos_y": row["pos_y"]
                })

    conn.close()
    return jsonify(items)


@app.route("/api/desktop/<int:shortcut_id>", methods=["PATCH", "DELETE"])
@login_required
def update_desktop_shortcut(shortcut_id):
    conn = get_db()
    if request.method == "PATCH":
        data = request.get_json(silent=True) or {}
        pos_x = int(data.get("pos_x", 28))
        pos_y = int(data.get("pos_y", 28))
        conn.execute(
            "UPDATE desktop_items SET pos_x = ?, pos_y = ? WHERE id = ? AND user_id = ?",
            (pos_x, pos_y, shortcut_id, session["user_id"])
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "moved"})

    conn.execute(
        "DELETE FROM desktop_items WHERE id = ? AND user_id = ?",
        (shortcut_id, session["user_id"])
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "removed"})


# --- TRASH BIN ROUTES ---
def trash_folder_recursive(conn, folder_id, now_str):
    conn.execute("UPDATE folders SET is_trashed = 1, trashed_at = ? WHERE id = ?", (now_str, folder_id))
    conn.execute("UPDATE files SET is_trashed = 1, trashed_at = ? WHERE folder_id = ?", (now_str, folder_id))
    children = conn.execute("SELECT id FROM folders WHERE parent_id = ?", (folder_id,)).fetchall()
    for ch in children:
        trash_folder_recursive(conn, ch["id"], now_str)


def restore_folder_recursive(conn, folder_id):
    conn.execute("UPDATE folders SET is_trashed = 0, trashed_at = NULL WHERE id = ?", (folder_id,))
    conn.execute("UPDATE files SET is_trashed = 0, trashed_at = NULL WHERE folder_id = ?", (folder_id,))
    children = conn.execute("SELECT id FROM folders WHERE parent_id = ?", (folder_id,)).fetchall()
    for ch in children:
        restore_folder_recursive(conn, ch["id"])


def purge_folder_recursive(conn, folder_id):
    child_files = conn.execute("SELECT * FROM files WHERE folder_id = ?", (folder_id,)).fetchall()
    for cf in child_files:
        fp = os.path.join(STORAGE_DIR, cf["stored_name"])
        if os.path.exists(fp):
            os.remove(fp)
        conn.execute("DELETE FROM desktop_items WHERE item_type = 'file' AND item_id = ?", (cf["id"],))
    conn.execute("DELETE FROM files WHERE folder_id = ?", (folder_id,))

    children = conn.execute("SELECT id FROM folders WHERE parent_id = ?", (folder_id,)).fetchall()
    for ch in children:
        purge_folder_recursive(conn, ch["id"])

    conn.execute("DELETE FROM desktop_items WHERE item_type = 'folder' AND item_id = ?", (folder_id,))
    conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))


@app.route("/api/trash", methods=["GET"])
@login_required
def get_trash():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    if user["role"] == "parent":
        folders = conn.execute(
            "SELECT folders.*, users.username AS owner FROM folders JOIN users ON folders.owner_id = users.id WHERE folders.is_trashed = 1 ORDER BY folders.trashed_at DESC"
        ).fetchall()
        files = conn.execute(
            "SELECT files.*, users.username AS owner FROM files JOIN users ON files.owner_id = users.id WHERE files.is_trashed = 1 ORDER BY files.trashed_at DESC"
        ).fetchall()
    else:
        folders = conn.execute(
            "SELECT folders.*, users.username AS owner FROM folders JOIN users ON folders.owner_id = users.id WHERE owner_id = ? AND folders.is_trashed = 1 ORDER BY folders.trashed_at DESC",
            (user["id"],)
        ).fetchall()
        files = conn.execute(
            "SELECT files.*, users.username AS owner FROM files JOIN users ON files.owner_id = users.id WHERE owner_id = ? AND files.is_trashed = 1 ORDER BY files.trashed_at DESC",
            (user["id"],)
        ).fetchall()

    conn.close()
    return jsonify({
        "folders": [dict(f) for f in folders],
        "files": [dict(f) for f in files]
    })


@app.route("/api/trash/move", methods=["POST"])
@login_required
def move_to_trash():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    for item in items:
        itype = item.get("type")
        iid = int(item.get("id", 0))
        if itype == "folder":
            row = conn.execute("SELECT * FROM folders WHERE id = ?", (iid,)).fetchone()
            if row and (row["owner_id"] == user["id"] or user["role"] == "parent"):
                trash_folder_recursive(conn, iid, now)
        elif itype == "file":
            row = conn.execute("SELECT * FROM files WHERE id = ?", (iid,)).fetchone()
            if row and (row["owner_id"] == user["id"] or user["role"] == "parent"):
                conn.execute("UPDATE files SET is_trashed = 1, trashed_at = ? WHERE id = ?", (now, iid))

    conn.commit()
    conn.close()
    return jsonify({"status": "trashed"})


@app.route("/api/trash/restore", methods=["POST"])
@login_required
def restore_from_trash():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    for item in items:
        itype = item.get("type")
        iid = int(item.get("id", 0))
        if itype == "folder":
            row = conn.execute("SELECT * FROM folders WHERE id = ?", (iid,)).fetchone()
            if row and (row["owner_id"] == user["id"] or user["role"] == "parent"):
                if row["parent_id"]:
                    parent = conn.execute("SELECT is_trashed FROM folders WHERE id = ?", (row["parent_id"],)).fetchone()
                    if not parent or parent["is_trashed"] == 1:
                        conn.execute("UPDATE folders SET parent_id = NULL WHERE id = ?", (iid,))
                restore_folder_recursive(conn, iid)
        elif itype == "file":
            row = conn.execute("SELECT * FROM files WHERE id = ?", (iid,)).fetchone()
            if row and (row["owner_id"] == user["id"] or user["role"] == "parent"):
                if row["folder_id"]:
                    parent = conn.execute("SELECT is_trashed FROM folders WHERE id = ?", (row["folder_id"],)).fetchone()
                    if not parent or parent["is_trashed"] == 1:
                        conn.execute("UPDATE files SET folder_id = NULL WHERE id = ?", (iid,))
                conn.execute("UPDATE files SET is_trashed = 0, trashed_at = NULL WHERE id = ?", (iid,))

    conn.commit()
    conn.close()
    return jsonify({"status": "restored"})


@app.route("/api/trash/delete", methods=["POST"])
@login_required
def delete_permanently():
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    for item in items:
        itype = item.get("type")
        iid = int(item.get("id", 0))
        if itype == "folder":
            row = conn.execute("SELECT * FROM folders WHERE id = ?", (iid,)).fetchone()
            if row and (row["owner_id"] == user["id"] or user["role"] == "parent"):
                purge_folder_recursive(conn, iid)
        elif itype == "file":
            row = conn.execute("SELECT * FROM files WHERE id = ?", (iid,)).fetchone()
            if row and (row["owner_id"] == user["id"] or user["role"] == "parent"):
                fp = os.path.join(STORAGE_DIR, row["stored_name"])
                if os.path.exists(fp):
                    os.remove(fp)
                conn.execute("DELETE FROM desktop_items WHERE item_type = 'file' AND item_id = ?", (iid,))
                conn.execute("DELETE FROM files WHERE id = ?", (iid,))

    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


@app.route("/api/trash/empty", methods=["DELETE"])
@login_required
def empty_trash():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    if user["role"] == "parent":
        trashed_folders = conn.execute("SELECT id FROM folders WHERE is_trashed = 1").fetchall()
        trashed_files = conn.execute("SELECT * FROM files WHERE is_trashed = 1").fetchall()
    else:
        trashed_folders = conn.execute("SELECT id FROM folders WHERE owner_id = ? AND is_trashed = 1", (user["id"],)).fetchall()
        trashed_files = conn.execute("SELECT * FROM files WHERE owner_id = ? AND is_trashed = 1", (user["id"],)).fetchall()

    for f in trashed_folders:
        purge_folder_recursive(conn, f["id"])

    for tf in trashed_files:
        fp = os.path.join(STORAGE_DIR, tf["stored_name"])
        if os.path.exists(fp):
            os.remove(fp)
        conn.execute("DELETE FROM desktop_items WHERE item_type = 'file' AND item_id = ?", (tf["id"],))
        conn.execute("DELETE FROM files WHERE id = ?", (tf["id"],))

    conn.commit()
    conn.close()
    return jsonify({"status": "emptied"})


# --- SHARED NESTDRIVE VFS HELPERS (TERMINAL + PERSONAL AI) ---
def get_visible_folders(conn, user, parent_id):
    if user["role"] == "parent":
        if parent_id is None:
            return conn.execute("SELECT * FROM folders WHERE parent_id IS NULL AND is_trashed = 0 ORDER BY name ASC").fetchall()
        return conn.execute("SELECT * FROM folders WHERE parent_id = ? AND is_trashed = 0 ORDER BY name ASC", (parent_id,)).fetchall()
    if parent_id is None:
        return conn.execute("SELECT * FROM folders WHERE owner_id = ? AND parent_id IS NULL AND is_trashed = 0 ORDER BY name ASC", (user["id"],)).fetchall()
    return conn.execute("SELECT * FROM folders WHERE owner_id = ? AND parent_id = ? AND is_trashed = 0 ORDER BY name ASC", (user["id"], parent_id)).fetchall()


def get_visible_files(conn, user, folder_id):
    if user["role"] == "parent":
        if folder_id is None:
            return conn.execute("SELECT * FROM files WHERE folder_id IS NULL AND is_trashed = 0 ORDER BY filename ASC").fetchall()
        return conn.execute("SELECT * FROM files WHERE folder_id = ? AND is_trashed = 0 ORDER BY filename ASC", (folder_id,)).fetchall()
    if folder_id is None:
        return conn.execute("SELECT * FROM files WHERE owner_id = ? AND folder_id IS NULL AND is_trashed = 0 ORDER BY filename ASC", (user["id"],)).fetchall()
    return conn.execute("SELECT * FROM files WHERE owner_id = ? AND folder_id = ? AND is_trashed = 0 ORDER BY filename ASC", (user["id"], folder_id)).fetchall()


def build_cwd_path(conn, folder_id):
    parts = []
    curr = folder_id
    while curr:
        row = conn.execute("SELECT id, name, parent_id FROM folders WHERE id = ? AND is_trashed = 0", (curr,)).fetchone()
        if not row:
            break
        parts.insert(0, row["name"])
        curr = row["parent_id"]
    return "/" + "/".join(parts) if parts else "/"


def resolve_drive_target(conn, user, cwd_id, raw_path):
    if not raw_path or raw_path == ".":
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (cwd_id,)).fetchone() if cwd_id else None
        return {"kind": "folder", "id": cwd_id, "row": row}

    if raw_path in ("/", "~"):
        return {"kind": "folder", "id": None, "row": None}

    cleaned = raw_path.strip()
    if cleaned.startswith("~/"):
        curr = None
        cleaned = cleaned[2:]
    elif cleaned.startswith("/"):
        curr = None
        cleaned = cleaned[1:]
    else:
        curr = cwd_id

    segments = [s for s in cleaned.split("/") if s and s != "."]
    if not segments:
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (curr,)).fetchone() if curr else None
        return {"kind": "folder", "id": curr, "row": row}

    for idx, seg in enumerate(segments):
        is_last = idx == len(segments) - 1
        if seg == "..":
            if curr is not None:
                parent_row = conn.execute("SELECT parent_id FROM folders WHERE id = ?", (curr,)).fetchone()
                curr = parent_row["parent_id"] if parent_row else None
            if is_last:
                row = conn.execute("SELECT * FROM folders WHERE id = ?", (curr,)).fetchone() if curr else None
                return {"kind": "folder", "id": curr, "row": row}
            continue

        folders = get_visible_folders(conn, user, curr)
        matched_folder = next((f for f in folders if f["name"] == seg), None)
        if not matched_folder:
            matched_folder = next((f for f in folders if f["name"].lower() == seg.lower()), None)

        if matched_folder:
            if is_last:
                return {"kind": "folder", "id": matched_folder["id"], "row": matched_folder}
            curr = matched_folder["id"]
            continue

        if is_last:
            files = get_visible_files(conn, user, curr)
            matched_file = next((f for f in files if f["filename"] == seg), None)
            if not matched_file:
                matched_file = next((f for f in files if f["filename"].lower() == seg.lower()), None)
            if matched_file:
                return {"kind": "file", "id": matched_file["id"], "row": matched_file, "parent_id": curr}
            return {"kind": "missing", "parent_id": curr, "name": seg}

        return {"kind": "invalid", "name": seg}

    row = conn.execute("SELECT * FROM folders WHERE id = ?", (curr,)).fetchone() if curr else None
    return {"kind": "folder", "id": curr, "row": row}


def ensure_folder_path(conn, user, raw_path, color="#fbbf24"):
    cleaned = raw_path.strip().strip("/")
    if not cleaned:
        return None
    curr = None
    for seg in [s for s in cleaned.split("/") if s and s != "."]:
        folders = get_visible_folders(conn, user, curr)
        match = next((f for f in folders if f["name"].lower() == seg.lower()), None)
        if match:
            curr = match["id"]
        else:
            cur = conn.execute(
                "INSERT INTO folders (owner_id, parent_id, name, color, is_trashed, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (user["id"], curr, seg, color, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
            curr = cur.lastrowid
    return curr


def build_tree_lines(conn, user, folder_id=None, prefix=""):
    lines = []
    folders = get_visible_folders(conn, user, folder_id)
    files = get_visible_files(conn, user, folder_id)
    entries = [("folder", f) for f in folders] + [("file", f) for f in files]

    for idx, (etype, item) in enumerate(entries):
        is_last = idx == len(entries) - 1
        connector = "└── " if is_last else "├── "
        extension = "    " if is_last else "│   "
        if etype == "folder":
            lines.append(f"{prefix}{connector}[DIR] {item['name']}/")
            lines.extend(build_tree_lines(conn, user, item["id"], prefix + extension))
        else:
            kb = item["size_bytes"] / 1024
            lines.append(f"{prefix}{connector}{item['filename']} ({kb:.1f} KB, {item['scan_status']})")
    return lines


# --- PERSONAL AI (LM STUDIO AGENTIC FILE ASSISTANT) ---
@app.route("/api/ai/status", methods=["GET"])
@login_required
def ai_status():
    try:
        resp = requests.get(LM_STUDIO_MODELS_URL, timeout=2.5)
        if resp.status_code == 200:
            models = resp.json().get("data", [])
            model_id = models[0]["id"] if models else "Loaded Model"
            return jsonify({"online": True, "model": model_id})
    except Exception:
        pass
    return jsonify({"online": False, "model": None})


def execute_ai_tool(conn, user, tool_name, args, allow_edits):
    write_tools = {
        "create_folder", "write_file", "append_file", "replace_in_file",
        "rename_item", "move_item", "trash_item", "pin_desktop"
    }
    if tool_name in write_tools and not allow_edits:
        return "Blocked: User has disabled 'Allow File Changes' mode.", None

    if tool_name == "list_files":
        path = (args.get("path") or "/").strip()
        target = resolve_drive_target(conn, user, None, path)
        if target["kind"] != "folder":
            return f"Folder '{path}' not found.", None
        folders = get_visible_folders(conn, user, target["id"])
        files = get_visible_files(conn, user, target["id"])
        items = [f"[DIR] {fo['name']}/" for fo in folders] + [
            f"[FILE] {fi['filename']} ({fi['size_bytes']} bytes, {fi['scan_status']})" for fi in files
        ]
        summary = "\n".join(items) if items else "(empty folder)"
        return summary, {"tool": "list_files", "label": f"Listed {path}", "mutated": False}

    if tool_name == "read_file":
        path = (args.get("path") or "").strip()
        target = resolve_drive_target(conn, user, None, path)
        if target["kind"] != "file":
            # Fallback: search by filename anywhere in NestDrive
            rows = conn.execute(
                "SELECT * FROM files WHERE filename LIKE ? AND is_trashed = 0",
                (f"%{os.path.basename(path)}%",)
            ).fetchall()
            visible = [r for r in rows if r["owner_id"] == user["id"] or r["role"] == "parent"]
            if not visible:
                return f"File '{path}' not found.", None
            f_row = visible[0]
        else:
            f_row = target["row"]

        disk_path = os.path.join(STORAGE_DIR, f_row["stored_name"])
        content = extract_readable_text(disk_path, f_row["filename"], limit=10000)
        return (
            f"File: {f_row['filename']} ({f_row['size_bytes']} bytes)\nContent:\n{content or '(empty file)'}",
            {"tool": "read_file", "label": f"Read {f_row['filename']}", "mutated": False}
        )

    if tool_name == "search_files":
        query = (args.get("query") or "").strip().lower()
        if user["role"] == "parent":
            all_fo = conn.execute("SELECT * FROM folders WHERE is_trashed = 0").fetchall()
            all_fi = conn.execute("SELECT * FROM files WHERE is_trashed = 0").fetchall()
        else:
            all_fo = conn.execute("SELECT * FROM folders WHERE owner_id = ? AND is_trashed = 0", (user["id"],)).fetchall()
            all_fi = conn.execute("SELECT * FROM files WHERE owner_id = ? AND is_trashed = 0", (user["id"],)).fetchall()

        hits = []
        for fo in all_fo:
            if query in fo["name"].lower():
                hits.append(f"[DIR] {build_cwd_path(conn, fo['id'])}/")
        for fi in all_fi:
            parent_p = build_cwd_path(conn, fi["folder_id"])
            full_p = f"{'' if parent_p == '/' else parent_p}/{fi['filename']}"
            txt = extract_readable_text(os.path.join(STORAGE_DIR, fi["stored_name"]), fi["filename"], limit=4000)
            if query in fi["filename"].lower() or query in txt.lower():
                hits.append(f"[FILE] {full_p}")
        return (
            "\n".join(hits) if hits else f"No matches found for '{query}'.",
            {"tool": "search_files", "label": f"Searched '{query}'", "mutated": False}
        )

    if tool_name == "create_folder":
        path = (args.get("path") or args.get("name") or "").strip()
        color = (args.get("color") or "#38bdf8").strip()
        if not path:
            return "Folder path is required.", None
        fid = ensure_folder_path(conn, user, path, color=color)
        conn.commit()
        return (
            f"Created folder '{path}' (id={fid}).",
            {"tool": "create_folder", "label": f"Created folder {path}", "mutated": True}
        )

    if tool_name in ("write_file", "append_file"):
        path = (args.get("path") or args.get("filename") or "").strip()
        content = str(args.get("content") or "")
        if not path:
            return "File path is required.", None

        target = resolve_drive_target(conn, user, None, path)
        if target["kind"] == "file":
            f_row = target["row"]
            full_disk = os.path.join(STORAGE_DIR, f_row["stored_name"])
            if tool_name == "append_file":
                existing = extract_readable_text(full_disk, f_row["filename"], limit=50000)
                sep = "\n" if existing and not existing.endswith("\n") else ""
                final_text = existing + sep + content
            else:
                final_text = content

            raw_bytes = final_text.encode("utf-8")
            with open(full_disk, "wb") as wf:
                wf.write(raw_bytes)
            scan_status, scan_details = evaluate_file_safety(conn, user, full_disk, f_row["filename"])
            conn.execute(
                "UPDATE files SET size_bytes = ?, scan_status = ?, scan_details = ? WHERE id = ?",
                (len(raw_bytes), scan_status, scan_details, f_row["id"])
            )
            conn.commit()
            verb = "Appended to" if tool_name == "append_file" else "Updated"
            return (
                f"{verb} '{f_row['filename']}' ({len(raw_bytes)} bytes, Safety: {scan_status}).",
                {"tool": tool_name, "label": f"{verb} {f_row['filename']}", "mutated": True}
            )

        # Create parent folders automatically if path contains subdirectories
        clean_rel = path.lstrip("/")
        dir_part = os.path.dirname(clean_rel)
        file_part = os.path.basename(clean_rel)
        parent_id = ensure_folder_path(conn, user, dir_part) if dir_part else None
        _, saved_name, scan_status = save_and_register_file(conn, user, parent_id, file_part, content.encode("utf-8"))
        conn.commit()
        return (
            f"Created '{saved_name}' in '{dir_part or '/'}' (Safety: {scan_status}).",
            {"tool": tool_name, "label": f"Created {saved_name}", "mutated": True}
        )

    if tool_name == "replace_in_file":
        path = (args.get("path") or "").strip()
        old_text = str(args.get("old_text") or "")
        new_text = str(args.get("new_text") or "")
        target = resolve_drive_target(conn, user, None, path)
        if target["kind"] != "file":
            return f"File '{path}' not found.", None
        f_row = target["row"]
        full_disk = os.path.join(STORAGE_DIR, f_row["stored_name"])
        current_text = extract_readable_text(full_disk, f_row["filename"], limit=50000)
        if old_text not in current_text:
            return f"Could not find target text inside '{f_row['filename']}'.", None
        updated_text = current_text.replace(old_text, new_text, 1)
        raw_bytes = updated_text.encode("utf-8")
        with open(full_disk, "wb") as wf:
            wf.write(raw_bytes)
        scan_status, scan_details = evaluate_file_safety(conn, user, full_disk, f_row["filename"])
        conn.execute(
            "UPDATE files SET size_bytes = ?, scan_status = ?, scan_details = ? WHERE id = ?",
            (len(raw_bytes), scan_status, scan_details, f_row["id"])
        )
        conn.commit()
        return (
            f"Edited '{f_row['filename']}' successfully.",
            {"tool": "replace_in_file", "label": f"Edited {f_row['filename']}", "mutated": True}
        )

    if tool_name == "rename_item":
        path = (args.get("path") or "").strip()
        new_name = (args.get("new_name") or "").strip()
        target = resolve_drive_target(conn, user, None, path)
        if target["kind"] == "file" and new_name:
            safe_new = secure_filename(new_name) or new_name
            conn.execute("UPDATE files SET filename = ? WHERE id = ?", (safe_new, target["id"]))
            conn.commit()
            return (
                f"Renamed file '{target['row']['filename']}' to '{safe_new}'.",
                {"tool": "rename_item", "label": f"Renamed -> {safe_new}", "mutated": True}
            )
        if target["kind"] == "folder" and target["id"] is not None and new_name:
            conn.execute("UPDATE folders SET name = ? WHERE id = ?", (new_name, target["id"]))
            conn.commit()
            return (
                f"Renamed folder '{target['row']['name']}' to '{new_name}'.",
                {"tool": "rename_item", "label": f"Renamed -> {new_name}/", "mutated": True}
            )
        return f"Could not rename '{path}'.", None

    if tool_name == "move_item":
        src_path = (args.get("source") or "").strip()
        dst_folder_path = (args.get("destination_folder") or "/").strip()
        src = resolve_drive_target(conn, user, None, src_path)
        if src["kind"] not in ("file", "folder") or src["id"] is None:
            return f"Source '{src_path}' not found.", None

        if dst_folder_path in ("/", "~", ""):
            dst_id = None
        else:
            dst = resolve_drive_target(conn, user, None, dst_folder_path)
            dst_id = dst["id"] if dst["kind"] == "folder" else ensure_folder_path(conn, user, dst_folder_path)

        if src["kind"] == "file":
            conn.execute("UPDATE files SET folder_id = ? WHERE id = ?", (dst_id, src["id"]))
            conn.commit()
            return (
                f"Moved '{src['row']['filename']}' to '{dst_folder_path or '/'}'.",
                {"tool": "move_item", "label": f"Moved {src['row']['filename']}", "mutated": True}
            )
        if src["kind"] == "folder" and dst_id != src["id"]:
            conn.execute("UPDATE folders SET parent_id = ? WHERE id = ?", (dst_id, src["id"]))
            conn.commit()
            return (
                f"Moved folder '{src['row']['name']}' to '{dst_folder_path or '/'}'.",
                {"tool": "move_item", "label": f"Moved {src['row']['name']}/", "mutated": True}
            )
        return "Invalid move target.", None

    if tool_name == "trash_item":
        path = (args.get("path") or "").strip()
        target = resolve_drive_target(conn, user, None, path)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        if target["kind"] == "file":
            conn.execute("UPDATE files SET is_trashed = 1, trashed_at = ? WHERE id = ?", (now, target["id"]))
            conn.commit()
            return (
                f"Moved file '{target['row']['filename']}' to Trash Bin.",
                {"tool": "trash_item", "label": f"Trashed {target['row']['filename']}", "mutated": True}
            )
        if target["kind"] == "folder" and target["id"] is not None:
            trash_folder_recursive(conn, target["id"], now)
            conn.commit()
            return (
                f"Moved folder '{target['row']['name']}' to Trash Bin.",
                {"tool": "trash_item", "label": f"Trashed {target['row']['name']}/", "mutated": True}
            )
        return f"Item '{path}' not found.", None

    if tool_name == "pin_desktop":
        path = (args.get("path") or "").strip()
        target = resolve_drive_target(conn, user, None, path)
        if target["kind"] in ("file", "folder") and target["id"] is not None:
            add_desktop_shortcut_internal(conn, user["id"], target["kind"], target["id"])
            conn.commit()
            return (
                f"Pinned '{path}' to Desktop.",
                {"tool": "pin_desktop", "label": f"Pinned {path} to Desktop", "mutated": True}
            )
        return f"Item '{path}' not found.", None

    return f"Unknown tool '{tool_name}'.", None


def extract_tool_call_from_text(text):
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Check fenced JSON blocks first
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL):
        try:
            obj = json.loads(match.group(1))
            if isinstance(obj, dict) and "tool" in obj:
                return obj["tool"], obj.get("args", {})
        except Exception:
            pass

    # Check raw inline JSON object containing "tool"
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidate = cleaned[brace_start:brace_end + 1]
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "tool" in obj:
                return obj["tool"], obj.get("args", {})
        except Exception:
            pass

    return None, None


@app.route("/api/ai/chat", methods=["POST"])
@login_required
def ai_chat():
    data = request.get_json(silent=True) or {}
    chat_history = data.get("messages", [])
    allow_edits = bool(data.get("allow_edits", True))

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    tree_lines = build_tree_lines(conn, user, None)
    tree_snapshot = "\n".join(tree_lines) if tree_lines else "(NestDrive is currently empty)"

    tools_doc = (
        "Available File Tools:\n"
        '- {"tool": "list_files", "args": {"path": "/"}}\n'
        '- {"tool": "read_file", "args": {"path": "notes.txt"}}\n'
        '- {"tool": "search_files", "args": {"query": "keyword"}}\n'
    )
    if allow_edits:
        tools_doc += (
            '- {"tool": "create_folder", "args": {"path": "FolderName", "color": "#38bdf8"}}\n'
            '- {"tool": "write_file", "args": {"path": "FolderName/file.txt", "content": "full text"}}\n'
            '- {"tool": "append_file", "args": {"path": "file.txt", "content": "text to add"}}\n'
            '- {"tool": "replace_in_file", "args": {"path": "file.txt", "old_text": "before", "new_text": "after"}}\n'
            '- {"tool": "rename_item", "args": {"path": "old.txt", "new_name": "new.txt"}}\n'
            '- {"tool": "move_item", "args": {"source": "file.txt", "destination_folder": "FolderName"}}\n'
            '- {"tool": "trash_item", "args": {"path": "unwanted.txt"}}\n'
            '- {"tool": "pin_desktop", "args": {"path": "FolderName"}}\n'
        )
    else:
        tools_doc += "(Note: File editing is currently toggled OFF; only read/search tools are allowed.)\n"

    system_prompt = (
        f"You are HomeNest Personal AI, an intelligent local file assistant for {user['username']} ({user['role']}).\n"
        "You have direct access to the user's NestDrive cloud filesystem.\n\n"
        f"Current NestDrive Tree:\n{tree_snapshot}\n\n"
        f"{tools_doc}\n"
        "RULES:\n"
        "1. When you need to read a file or make any change (create, edit, rename, move, trash, pin), reply ONLY with a single JSON object:\n"
        '   {"tool": "<tool_name>", "args": {<arguments>}}\n'
        "2. Do not wrap the JSON tool call in extra conversation; wait for the TOOL_RESULT message before answering the user.\n"
        "3. Once all needed tool calls are finished, reply clearly and concisely to the user explaining what you found or changed."
    )

    llm_messages = [{"role": "system", "content": system_prompt}]
    for msg in chat_history[-12:]:
        role = msg.get("role")
        content = str(msg.get("content", ""))
        if role in ("user", "assistant") and content:
            llm_messages.append({"role": role, "content": content})

    executed_actions = []
    any_mutated = False
    final_reply = ""
    active_model = None

    for _step in range(6):
        try:
            resp = requests.post(
                LM_STUDIO_URL,
                json={"messages": llm_messages, "temperature": 0.2, "max_tokens": 900},
                timeout=35
            )
            if resp.status_code != 200:
                conn.close()
                return jsonify({"error": f"LM Studio returned HTTP {resp.status_code}. Make sure a model is loaded."}), 502

            payload = resp.json()
            active_model = payload.get("model")
            choice_msg = payload["choices"][0]["message"]
            raw_content = choice_msg.get("content") or ""
        except requests.exceptions.ConnectionError:
            conn.close()
            return jsonify({"error": "Cannot connect to LM Studio at http://localhost:1234. Start the Local Server in LM Studio first."}), 503
        except Exception as exc:
            conn.close()
            return jsonify({"error": f"LM Studio error: {exc}"}), 500

        tool_name, tool_args = extract_tool_call_from_text(raw_content)
        if not tool_name:
            final_reply = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
            break

        observation, action_badge = execute_ai_tool(conn, user, tool_name, tool_args or {}, allow_edits)
        if action_badge:
            executed_actions.append(action_badge)
            if action_badge.get("mutated"):
                any_mutated = True

        llm_messages.append({"role": "assistant", "content": raw_content})
        llm_messages.append({
            "role": "user",
            "content": f"TOOL_RESULT ({tool_name}):\n{observation}\n\nNow either execute the next needed tool JSON or provide your final answer to the user."
        })
    else:
        final_reply = "Completed the file operations listed above."

    conn.close()
    return jsonify({
        "reply": final_reply or "Done.",
        "actions": executed_actions,
        "refresh": any_mutated,
        "model": active_model
    })


# --- WORKING TERMINAL ENGINE (NESTDRIVE VFS + REAL INTERNET/SYSTEM SHELL) ---
def run_host_command(raw_cmd):
    blocked_patterns = [
        r"\brm\s+-rf\s+/",
        r"\bmkfs\b",
        r"\bdd\s+if=",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bformat\s+[a-zA-Z]:",
        r"\bdiskpart\b",
        r":\(\)\s*\{"
    ]
    for pat in blocked_patterns:
        if re.search(pat, raw_cmd, re.IGNORECASE):
            return "Blocked: Destructive system-level command is not permitted."

    is_windows = platform.system().lower().startswith("win")
    cmd_to_run = raw_cmd.strip()

    try:
        tokens = shlex.split(cmd_to_run, posix=not is_windows)
    except ValueError:
        tokens = cmd_to_run.split()

    if not tokens:
        return ""

    base = tokens[0].lower()

    if base == "ping":
        if is_windows and "-n" not in [t.lower() for t in tokens]:
            cmd_to_run = f"ping -n 4 {' '.join(tokens[1:])}"
        elif not is_windows and "-c" not in [t.lower() for t in tokens]:
            cmd_to_run = f"ping -c 4 {' '.join(tokens[1:])}"

    if base == "traceroute" and is_windows and not shutil.which("traceroute"):
        cmd_to_run = f"tracert {' '.join(tokens[1:])}"
    elif base == "tracert" and not is_windows and not shutil.which("tracert"):
        cmd_to_run = f"traceroute {' '.join(tokens[1:])}"

    try:
        proc = subprocess.run(
            cmd_to_run,
            shell=True,
            cwd=STORAGE_DIR,
            capture_output=True,
            text=True,
            timeout=15
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return out.strip() if out.strip() else f"(Command exited with code {proc.returncode})"
    except subprocess.TimeoutExpired:
        return "Command timed out after 15 seconds."
    except Exception as exc:
        return f"Execution error: {exc}"


@app.route("/api/terminal", methods=["POST"])
@login_required
def execute_terminal():
    data = request.get_json(silent=True) or {}
    raw_cmd = (data.get("command") or "").strip()
    cwd_id = data.get("cwd_id")
    if cwd_id is not None:
        cwd_id = int(cwd_id)

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()

    if cwd_id is not None:
        exists = conn.execute("SELECT id FROM folders WHERE id = ? AND is_trashed = 0", (cwd_id,)).fetchone()
        if not exists:
            cwd_id = None

    cwd_path = build_cwd_path(conn, cwd_id)

    if not raw_cmd:
        conn.close()
        return jsonify({"output": "", "cwd_id": cwd_id, "cwd_path": cwd_path})

    redir_match = re.match(r'^echo\s+(.*?)\s*(>>|>)\s*([^\s]+)$', raw_cmd)
    if redir_match:
        text_body = redir_match.group(1).strip().strip('"').strip("'")
        mode = redir_match.group(2)
        target_path = redir_match.group(3)
        target = resolve_drive_target(conn, user, cwd_id, target_path)

        if target["kind"] == "file":
            f_row = target["row"]
            full_disk = os.path.join(STORAGE_DIR, f_row["stored_name"])
            existing_bytes = b""
            if mode == ">>" and os.path.exists(full_disk):
                with open(full_disk, "rb") as rf:
                    existing_bytes = rf.read()
                if existing_bytes and not existing_bytes.endswith(b"\n"):
                    existing_bytes += b"\n"
            new_bytes = existing_bytes + (text_body + "\n").encode("utf-8")
            with open(full_disk, "wb") as wf:
                wf.write(new_bytes)
            conn.execute("UPDATE files SET size_bytes = ? WHERE id = ?", (len(new_bytes), f_row["id"]))
            conn.commit()
            conn.close()
            return jsonify({
                "output": f"Wrote {len(new_bytes)} bytes to {f_row['filename']}",
                "cwd_id": cwd_id,
                "cwd_path": cwd_path,
                "refresh": True
            })
        elif target["kind"] == "missing":
            _, saved_name, scan_status = save_and_register_file(
                conn, user, target["parent_id"], target["name"], (text_body + "\n").encode("utf-8")
            )
            conn.commit()
            conn.close()
            return jsonify({
                "output": f"Created {saved_name} (Safety: {scan_status})",
                "cwd_id": cwd_id,
                "cwd_path": cwd_path,
                "refresh": True
            })

    try:
        tokens = shlex.split(raw_cmd)
    except ValueError as err:
        conn.close()
        return jsonify({"output": f"Syntax error: {err}", "cwd_id": cwd_id, "cwd_path": cwd_path})

    cmd = tokens[0].lower()
    args = tokens[1:]

    if cmd == "help":
        help_text = (
            "HomeNest Terminal — NestDrive Files + Real Internet & System Shell\n"
            "──────────────────────────────────────────────────────────────────\n"
            "File & Folder Commands (Synced live with NestDrive & Desktop):\n"
            "  ls [-la] [path]         List folders & files in NestDrive\n"
            "  cd <folder|..|/>        Change current NestDrive directory\n"
            "  pwd                     Show current NestDrive directory path\n"
            "  tree                    Display full folder & file tree\n"
            "  mkdir <name>            Create a new folder in NestDrive\n"
            "  touch <file>            Create a new empty file in NestDrive\n"
            "  echo \"text\" > <file>    Write or append (>>) text to a file\n"
            "  cat <file>              Read file contents (supports .txt, .py, .docx, etc.)\n"
            "  mv <src> <dst>          Rename or move a file/folder\n"
            "  rm <name>               Move a file or folder to Trash Bin\n"
            "  find <query>            Search files & folders by name\n"
            "  grep <text>             Search inside file contents\n"
            "  open <file>             Open file in HomeNest Full-Screen Preview\n"
            "  desktop <name>          Pin file or folder to Desktop\n"
            "  clear                   Clear terminal screen\n\n"
            "Internet & Network Commands (Real Host Execution):\n"
            "  curl <url>              Fetch web pages, APIs, or headers (curl -I <url>)\n"
            "  curl <url> -o <file>    Download a file straight into NestDrive\n"
            "  wget <url>              Download URL directly into current folder\n"
            "  ping <host>             Test live internet latency (e.g. ping 1.1.1.1)\n"
            "  nslookup / dig <domain> Query live DNS records\n"
            "  whois <domain>          Look up domain registration info\n"
            "  traceroute <host>       Trace network route to host\n"
            "  ifconfig / ipconfig     Inspect network interfaces\n"
            "  python3 -c \"...\"        Run Python one-liners or scripts"
        )
        conn.close()
        return jsonify({"output": help_text, "cwd_id": cwd_id, "cwd_path": cwd_path})

    if cmd in ("clear", "cls"):
        conn.close()
        return jsonify({"output": "", "cwd_id": cwd_id, "cwd_path": cwd_path, "clear": True})

    if cmd == "pwd":
        conn.close()
        return jsonify({"output": f"/HomeNest/files{'' if cwd_path == '/' else cwd_path}", "cwd_id": cwd_id, "cwd_path": cwd_path})

    if cmd == "cd":
        dest = args[0] if args else "/"
        target = resolve_drive_target(conn, user, cwd_id, dest)
        if target["kind"] != "folder":
            conn.close()
            return jsonify({"output": f"cd: no such directory: {dest}", "cwd_id": cwd_id, "cwd_path": cwd_path})
        new_cwd = target["id"]
        new_path = build_cwd_path(conn, new_cwd)
        conn.close()
        return jsonify({"output": "", "cwd_id": new_cwd, "cwd_path": new_path})

    if cmd in ("ls", "dir"):
        flags = [a for a in args if a.startswith("-")]
        paths = [a for a in args if not a.startswith("-")]
        detailed = any("l" in fl for fl in flags)
        target_folder_id = cwd_id

        if paths:
            target = resolve_drive_target(conn, user, cwd_id, paths[0])
            if target["kind"] == "file":
                f = target["row"]
                out = f"{f['uploaded_at']}  {(f['size_bytes']/1024):6.1f} KB  [{f['scan_status']}]  {f['filename']}"
                conn.close()
                return jsonify({"output": out, "cwd_id": cwd_id, "cwd_path": cwd_path})
            if target["kind"] != "folder":
                conn.close()
                return jsonify({"output": f"ls: cannot access '{paths[0]}': No such file or directory", "cwd_id": cwd_id, "cwd_path": cwd_path})
            target_folder_id = target["id"]

        folders = get_visible_folders(conn, user, target_folder_id)
        files = get_visible_files(conn, user, target_folder_id)
        lines = []

        if detailed:
            for fo in folders:
                lines.append(f"drwxr-xr-x  {fo['owner']:<10}       -  {fo['created_at']}  {fo['name']}/")
            for fi in files:
                kb = f"{fi['size_bytes']/1024:.1f} KB"
                lines.append(f"-rw-r--r--  {fi['owner']:<10} {kb:>8}  {fi['uploaded_at']}  {fi['filename']}  [{fi['scan_status']}]")
        else:
            names = [f"{fo['name']}/" for fo in folders] + [fi["filename"] for fi in files]
            if names:
                lines.append("  ".join(names))

        conn.close()
        return jsonify({
            "output": "\n".join(lines) if lines else "(empty directory)",
            "cwd_id": cwd_id,
            "cwd_path": cwd_path
        })

    if cmd == "tree":
        tree_lines = [f"/HomeNest/files{'' if cwd_path == '/' else cwd_path}"]
        tree_lines.extend(build_tree_lines(conn, user, cwd_id))
        conn.close()
        return jsonify({"output": "\n".join(tree_lines), "cwd_id": cwd_id, "cwd_path": cwd_path})

    if cmd == "mkdir":
        if not args:
            conn.close()
            return jsonify({"output": "usage: mkdir <folder_name>", "cwd_id": cwd_id, "cwd_path": cwd_path})
        created = []
        for raw_name in args:
            if raw_name.startswith("-"):
                continue
            target = resolve_drive_target(conn, user, cwd_id, raw_name)
            if target["kind"] in ("folder", "file"):
                created.append(f"mkdir: '{raw_name}' already exists")
                continue
            if target["kind"] != "missing":
                created.append(f"mkdir: invalid path '{raw_name}'")
                continue
            conn.execute(
                "INSERT INTO folders (owner_id, parent_id, name, color, is_trashed, created_at) VALUES (?, ?, ?, '#fbbf24', 0, ?)",
                (user["id"], target["parent_id"], target["name"], datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
            created.append(f"Created folder: {target['name']}/")
        conn.commit()
        conn.close()
        return jsonify({"output": "\n".join(created), "cwd_id": cwd_id, "cwd_path": cwd_path, "refresh": True})

    if cmd == "touch":
        if not args:
            conn.close()
            return jsonify({"output": "usage: touch <filename>", "cwd_id": cwd_id, "cwd_path": cwd_path})
        msgs = []
        for raw_name in args:
            target = resolve_drive_target(conn, user, cwd_id, raw_name)
            if target["kind"] == "file":
                msgs.append(f"Updated timestamp: {target['row']['filename']}")
            elif target["kind"] == "missing":
                _, sname, status = save_and_register_file(conn, user, target["parent_id"], target["name"], b"")
                msgs.append(f"Created file: {sname} [{status}]")
            else:
                msgs.append(f"touch: cannot create '{raw_name}'")
        conn.commit()
        conn.close()
        return jsonify({"output": "\n".join(msgs), "cwd_id": cwd_id, "cwd_path": cwd_path, "refresh": True})

    if cmd in ("cat", "head", "tail"):
        if not args:
            conn.close()
            return jsonify({"output": f"usage: {cmd} <filename>", "cwd_id": cwd_id, "cwd_path": cwd_path})
        target = resolve_drive_target(conn, user, cwd_id, args[0])
        if target["kind"] != "file":
            conn.close()
            return jsonify({"output": f"{cmd}: {args[0]}: No such file", "cwd_id": cwd_id, "cwd_path": cwd_path})
        f_row = target["row"]
        disk_path = os.path.join(STORAGE_DIR, f_row["stored_name"])
        content = extract_readable_text(disk_path, f_row["filename"])
        if not content and os.path.exists(disk_path):
            content = f"(Binary file: {f_row['filename']}, {f_row['size_bytes']} bytes)"
        if cmd == "head":
            content = "\n".join(content.splitlines()[:15])
        elif cmd == "tail":
            content = "\n".join(content.splitlines()[-15:])
        conn.close()
        return jsonify({"output": content or "(empty file)", "cwd_id": cwd_id, "cwd_path": cwd_path})

    if cmd == "open":
        if not args:
            conn.close()
            return jsonify({"output": "usage: open <filename>", "cwd_id": cwd_id, "cwd_path": cwd_path})
        target = resolve_drive_target(conn, user, cwd_id, args[0])
        if target["kind"] != "file":
            conn.close()
            return jsonify({"output": f"open: '{args[0]}' is not a file", "cwd_id": cwd_id, "cwd_path": cwd_path})
        conn.close()
        return jsonify({
            "output": f"Opening {target['row']['filename']} in previewer...",
            "cwd_id": cwd_id,
            "cwd_path": cwd_path,
            "open_file_id": target["id"]
        })

    if cmd == "desktop":
        if not args:
            conn.close()
            return jsonify({"output": "usage: desktop <file_or_folder>", "cwd_id": cwd_id, "cwd_path": cwd_path})
        target = resolve_drive_target(conn, user, cwd_id, args[0])
        if target["kind"] not in ("file", "folder") or target["id"] is None:
            conn.close()
            return jsonify({"output": f"desktop: '{args[0]}' not found", "cwd_id": cwd_id, "cwd_path": cwd_path})
        add_desktop_shortcut_internal(conn, user["id"], target["kind"], target["id"])
        conn.commit()
        conn.close()
        return jsonify({
            "output": f"Pinned '{args[0]}' to Desktop.",
            "cwd_id": cwd_id,
            "cwd_path": cwd_path,
            "refresh": True
        })

    if cmd == "rm":
        targets = [a for a in args if not a.startswith("-")]
        if not targets:
            conn.close()
            return jsonify({"output": "usage: rm <file_or_folder>", "cwd_id": cwd_id, "cwd_path": cwd_path})
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        msgs = []
        for t_str in targets:
            target = resolve_drive_target(conn, user, cwd_id, t_str)
            if target["kind"] == "folder" and target["id"] is not None:
                trash_folder_recursive(conn, target["id"], now)
                msgs.append(f"Moved folder '{target['row']['name']}' to Trash Bin.")
            elif target["kind"] == "file":
                conn.execute("UPDATE files SET is_trashed = 1, trashed_at = ? WHERE id = ?", (now, target["id"]))
                msgs.append(f"Moved file '{target['row']['filename']}' to Trash Bin.")
            else:
                msgs.append(f"rm: cannot remove '{t_str}': No such file or directory")
        conn.commit()
        conn.close()
        return jsonify({"output": "\n".join(msgs), "cwd_id": cwd_id, "cwd_path": cwd_path, "refresh": True})

    if cmd == "mv":
        if len(args) < 2:
            conn.close()
            return jsonify({"output": "usage: mv <source> <destination>", "cwd_id": cwd_id, "cwd_path": cwd_path})
        src = resolve_drive_target(conn, user, cwd_id, args[0])
        dst = resolve_drive_target(conn, user, cwd_id, args[1])

        if src["kind"] not in ("file", "folder") or src["id"] is None:
            conn.close()
            return jsonify({"output": f"mv: cannot stat '{args[0]}': No such file or directory", "cwd_id": cwd_id, "cwd_path": cwd_path})

        if dst["kind"] == "folder":
            if src["kind"] == "file":
                conn.execute("UPDATE files SET folder_id = ? WHERE id = ?", (dst["id"], src["id"]))
                conn.commit()
                conn.close()
                return jsonify({"output": f"Moved '{src['row']['filename']}' -> '{args[1]}'", "cwd_id": cwd_id, "cwd_path": cwd_path, "refresh": True})
            if src["kind"] == "folder" and dst["id"] != src["id"]:
                conn.execute("UPDATE folders SET parent_id = ? WHERE id = ?", (dst["id"], src["id"]))
                conn.commit()
                conn.close()
                return jsonify({"output": f"Moved folder '{src['row']['name']}' -> '{args[1]}'", "cwd_id": cwd_id, "cwd_path": cwd_path, "refresh": True})

        if dst["kind"] == "missing":
            if src["kind"] == "file":
                new_fn = secure_filename(dst["name"]) or dst["name"]
                conn.execute("UPDATE files SET filename = ?, folder_id = ? WHERE id = ?", (new_fn, dst["parent_id"], src["id"]))
                conn.commit()
                conn.close()
                return jsonify({"output": f"Renamed '{src['row']['filename']}' -> '{new_fn}'", "cwd_id": cwd_id, "cwd_path": cwd_path, "refresh": True})
            if src["kind"] == "folder":
                conn.execute("UPDATE folders SET name = ?, parent_id = ? WHERE id = ?", (dst["name"], dst["parent_id"], src["id"]))
                conn.commit()
                conn.close()
                return jsonify({"output": f"Renamed folder '{src['row']['name']}' -> '{dst['name']}'", "cwd_id": cwd_id, "cwd_path": cwd_path, "refresh": True})

        conn.close()
        return jsonify({"output": f"mv: cannot move '{args[0]}' to '{args[1]}'", "cwd_id": cwd_id, "cwd_path": cwd_path})

    if cmd == "find":
        query = args[0].lower() if args else ""
        if user["role"] == "parent":
            all_fo = conn.execute("SELECT * FROM folders WHERE is_trashed = 0").fetchall()
            all_fi = conn.execute("SELECT * FROM files WHERE is_trashed = 0").fetchall()
        else:
            all_fo = conn.execute("SELECT * FROM folders WHERE owner_id = ? AND is_trashed = 0", (user["id"],)).fetchall()
            all_fi = conn.execute("SELECT * FROM files WHERE owner_id = ? AND is_trashed = 0", (user["id"],)).fetchall()
        matches = []
        for fo in all_fo:
            if query in fo["name"].lower():
                matches.append(f"[DIR]  {build_cwd_path(conn, fo['id'])}/")
        for fi in all_fi:
            if query in fi["filename"].lower():
                parent_p = build_cwd_path(conn, fi["folder_id"])
                sep = "" if parent_p == "/" else "/"
                matches.append(f"[FILE] {parent_p}{sep}{fi['filename']}")
        conn.close()
        return jsonify({"output": "\n".join(matches) if matches else "No matches found.", "cwd_id": cwd_id, "cwd_path": cwd_path})

    if cmd == "grep":
        if not args:
            conn.close()
            return jsonify({"output": "usage: grep <search_text>", "cwd_id": cwd_id, "cwd_path": cwd_path})
        needle = args[0].lower()
        files = get_visible_files(conn, user, cwd_id)
        hits = []
        for fi in files:
            txt = extract_readable_text(os.path.join(STORAGE_DIR, fi["stored_name"]), fi["filename"])
            for lineno, line in enumerate(txt.splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{fi['filename']}:{lineno}: {line.strip()[:120]}")
        conn.close()
        return jsonify({"output": "\n".join(hits) if hits else "No matching lines in current directory.", "cwd_id": cwd_id, "cwd_path": cwd_path})

    if cmd == "wget" or (cmd == "curl" and any(a in ("-o", "-O", "--output", "--remote-name") for a in args)):
        url = next((a for a in args if a.startswith("http://") or a.startswith("https://")), None)
        if not url:
            url = next((a for a in args if not a.startswith("-") and "." in a), None)
            if url:
                url = "https://" + url
        if not url:
            conn.close()
            return jsonify({"output": f"{cmd}: missing URL", "cwd_id": cwd_id, "cwd_path": cwd_path})

        out_name = None
        if "-o" in args:
            idx = args.index("-o")
            if idx + 1 < len(args):
                out_name = args[idx + 1]
        elif "--output" in args:
            idx = args.index("--output")
            if idx + 1 < len(args):
                out_name = args[idx + 1]
        if not out_name:
            parsed = urlparse(url)
            out_name = os.path.basename(parsed.path) or f"{parsed.netloc or 'download'}.html"

        try:
            resp = requests.get(url, timeout=12, headers={"User-Agent": "HomeNest-Terminal/1.0"})
            resp.raise_for_status()
            _, saved_name, scan_status = save_and_register_file(conn, user, cwd_id, out_name, resp.content)
            conn.commit()
            conn.close()
            return jsonify({
                "output": f"Downloaded {len(resp.content):,} bytes from {url}\nSaved to NestDrive as '{saved_name}' (Safety: {scan_status})",
                "cwd_id": cwd_id,
                "cwd_path": cwd_path,
                "refresh": True
            })
        except Exception as exc:
            conn.close()
            return jsonify({"output": f"Download failed: {exc}", "cwd_id": cwd_id, "cwd_path": cwd_path})

    conn.close()
    host_output = run_host_command(raw_cmd)
    return jsonify({
        "output": host_output,
        "cwd_id": cwd_id,
        "cwd_path": cwd_path
    })


# --- NOTIFICATIONS ---
@app.route("/api/notifications", methods=["GET", "DELETE"])
@login_required
def handle_notifications():
    conn = get_db()
    if request.method == "DELETE":
        conn.execute("DELETE FROM notifications WHERE user_id = ?", (session["user_id"],))
        conn.commit()
        conn.close()
        return jsonify({"status": "cleared"})

    notes = conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 30",
        (session["user_id"],)
    ).fetchall()
    conn.close()
    return jsonify([dict(n) for n in notes])


@app.route("/api/notifications/read", methods=["POST"])
@login_required
def mark_notifications_read():
    conn = get_db()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ?", (session["user_id"],))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


def _init_tool_history_table():
    conn = sqlite3.connect("homenest.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_tool_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_key TEXT NOT NULL,
            tool TEXT NOT NULL,
            item_json TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


_init_tool_history_table()


def _current_account_key():
    uid = session.get("user_id")
    uname = session.get("username")
    if uid is not None:
        return f"id:{uid}"
    if uname:
        return f"user:{uname}"
    return None


@app.route("/api/history/<tool>", methods=["GET", "POST", "DELETE"])
def api_tool_history(tool):
    if tool not in ("dictionary", "translator"):
        return jsonify({"error": "Invalid tool"}), 400

    account_key = _current_account_key()
    if not account_key:
        return jsonify({"error": "Unauthorized"}), 401

    conn = sqlite3.connect("homenest.db")
    cur = conn.cursor()

    if request.method == "GET":
        cur.execute(
            "SELECT item_json FROM user_tool_history WHERE account_key = ? AND tool = ? ORDER BY id DESC LIMIT 12",
            (account_key, tool),
        )
        rows = [json.loads(r[0]) for r in cur.fetchall()]
        conn.close()
        return jsonify({"items": rows})

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        item = payload.get("item")
        if not item:
            conn.close()
            return jsonify({"error": "Missing item"}), 400

        serialized = json.dumps(item, ensure_ascii=False)
        cur.execute(
            "DELETE FROM user_tool_history WHERE account_key = ? AND tool = ? AND item_json = ?",
            (account_key, tool, serialized),
        )
        cur.execute(
            "INSERT INTO user_tool_history (account_key, tool, item_json) VALUES (?, ?, ?)",
            (account_key, tool, serialized),
        )
        cur.execute(
            """
            DELETE FROM user_tool_history
            WHERE account_key = ? AND tool = ? AND id NOT IN (
                SELECT id FROM user_tool_history
                WHERE account_key = ? AND tool = ?
                ORDER BY id DESC LIMIT 12
            )
            """,
            (account_key, tool, account_key, tool),
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "saved"})

    # DELETE
    cur.execute(
        "DELETE FROM user_tool_history WHERE account_key = ? AND tool = ?",
        (account_key, tool),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "cleared"})
    

def _http_get_text(url, timeout=8, extra_headers=None):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json, application/xml, text/xml, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except Exception as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc) or isinstance(getattr(exc, "reason", None), ssl.SSLError):
            ctx = ssl._create_unverified_context()
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
        else:
            raise
    with resp:
        final_url = resp.geturl()
        raw = resp.read().decode("utf-8", errors="replace")
        return raw, final_url


def _strip_tags(raw_html):
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw_html or ""))
    return re.sub(r"\s+", " ", text).strip()


def _resolve_city_coords(city_name):
    lower = (city_name or "").lower()
    for key, coords in CITY_COORDS.items():
        if key in lower:
            return coords
    try:
        q = urllib.parse.quote(city_name.split(",")[0].strip())
        raw, _ = _http_get_text(
            f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count=1&language=en&format=json",
            timeout=5,
        )
        data = json.loads(raw)
        if data.get("results"):
            r = data["results"][0]
            return float(r["latitude"]), float(r["longitude"])
    except Exception:
        pass
    return 45.38, -73.51  # Default: Candiac, QC


def _parse_rss_feed(source_name, rss_url, loc_label, base_lat, base_lon, limit=6):
    items_out = []
    xml_text, _ = _http_get_text(rss_url, timeout=8)
    root = ET.fromstring(xml_text)
    for item in root.findall("./channel/item")[:limit]:
        raw_title = _strip_tags(item.findtext("title") or "")
        if not raw_title:
            continue
        source_el = item.find("source")
        src = _strip_tags(source_el.text) if (source_el is not None and source_el.text) else source_name
        if not src and " - " in raw_title:
            raw_title, src = raw_title.rsplit(" - ", 1)
        src = src or "Wire Desk"

        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        try:
            dt = parsedate_to_datetime(pub_date)
            ts = dt.timestamp()
            date_str = dt.strftime("%b %d, %H:%M")
        except Exception:
            ts = 0.0
            date_str = pub_date[:16] or "Today"

        desc_html = item.findtext("description") or ""
        snippet = _strip_tags(desc_html)
        if snippet.lower().startswith(raw_title.lower()[:25]):
            snippet = ""

        items_out.append({
            "title": raw_title,
            "source": src,
            "date": date_str,
            "link": link,
            "snippet": snippet or f"Coverage from {src} on {raw_title}.",
            "content_encoded": desc_html,
            "location_name": loc_label,
            "lat": round(base_lat + random.uniform(-0.14, 0.14), 4),
            "lon": round(base_lon + random.uniform(-0.18, 0.18), 4),
            "_ts": ts,
        })
    return items_out


def _generate_news_briefing(articles, category, city):
    headlines = "\n".join(f"- {a['title']} ({a['source']})" for a in articles[:6])
    prompt = (
        f"Summarize these {category} news headlines (focus: {city}) in 2 crisp, journalistic sentences "
        f"for a daily newspaper briefing:\n{headlines}"
    )
    payload = json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 180,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            LM_STUDIO_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            summary = data["choices"][0]["message"]["content"].strip()
            if summary:
                return summary, "LM Studio"
    except Exception:
        pass

    top_titles = [a["title"].split(" - ")[0] for a in articles[:3]]
    fallback = "Today's edition tracks key developments including " + "; ".join(top_titles) + "."
    return fallback, "Newsroom Digest"


@app.route("/api/news")
def api_news():
    category = request.args.get("category", "tech").lower()
    city = request.args.get("city", "Candiac, QC").strip() or "Candiac, QC"
    stock_q = request.args.get("q", "").strip()
    lat_arg = request.args.get("lat", type=float)
    lon_arg = request.args.get("lon", type=float)

    if lat_arg is not None and lon_arg is not None:
        base_lat, base_lon = lat_arg, lon_arg
    else:
        base_lat, base_lon = _resolve_city_coords(city)

    if category in ("stocks", "stocks_news"):
        feeds = list(DIRECT_FEEDS["stocks"])
        loc_label = f"{stock_q} Desk" if stock_q else "Equity Desk"
        if stock_q:
            q_enc = urllib.parse.quote(f"{stock_q} stock")
            feeds.insert(0, (f"{stock_q} Wire", f"https://news.google.com/rss/search?q={q_enc}&hl=en-CA&gl=CA&ceid=CA:en"))
    elif category == "economy":
        feeds = DIRECT_FEEDS["economy"]
        loc_label = "Global Markets"
    elif category == "best":
        feeds = DIRECT_FEEDS["best"]
        loc_label = "Curated Picks"
    elif category == "world":
        feeds = DIRECT_FEEDS["world"]
        loc_label = "World Desk"
    elif category == "city":
        loc_label = city
        q = urllib.parse.quote(city)
        feeds = [("Local Wire", f"https://news.google.com/rss/search?q={q}&hl=en-CA&gl=CA&ceid=CA:en")]
        if any(k in city.lower() for k in ("candiac", "montreal", "laval", "qc", "quebec")):
            feeds = DIRECT_FEEDS["local_qc"] + feeds
    else:
        feeds = DIRECT_FEEDS["tech"]
        loc_label = "Tech Desk"

    raw_articles = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_parse_rss_feed, src, url, loc_label, base_lat, base_lon, 6)
            for src, url in feeds
        ]
        for fut in as_completed(futures):
            try:
                raw_articles.extend(fut.result())
            except Exception:
                continue

    raw_articles.sort(key=lambda a: a.get("_ts", 0), reverse=True)
    seen = set()
    articles = []
    for art in raw_articles:
        key = re.sub(r"[^a-z0-9]+", "", art["title"].lower())[:50]
        if key and key not in seen:
            seen.add(key)
            art.pop("_ts", None)
            articles.append(art)

    if not articles:
        return jsonify({"success": False, "error": "Unable to reach news feeds.", "articles": []})

    ai_summary, ai_source = _generate_news_briefing(articles, stock_q or category, city)
    return jsonify({
        "success": True,
        "articles": articles[:15],
        "ai_summary": ai_summary,
        "ai_source": ai_source,
    })


def _normalize_interval(range_str, interval_str):
    if range_str in ("1mo", "3mo") and interval_str == "1mo":
        return "1d"
    if range_str == "1mo" and interval_str == "1wk":
        return "1d"
    return interval_str


def _fetch_yahoo_chart(symbol, range_str="1y", interval_str="1d"):
    interval_str = _normalize_interval(range_str, interval_str)
    cache_key = (symbol.upper(), range_str, interval_str)
    now = time.time()
    cached = _STOCK_CACHE.get(cache_key)
    if cached and (now - cached["ts"] < _STOCK_CACHE_TTL):
        return cached["meta"], cached["history"]

    sym = urllib.parse.quote(symbol)
    yahoo_headers = {"Referer": "https://finance.yahoo.com/"}
    last_err = None

    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{sym}?range={range_str}&interval={interval_str}&includePrePost=false&events=div%2Csplits"
        try:
            raw, _ = _http_get_text(url, timeout=8, extra_headers=yahoo_headers)
            data = json.loads(raw)
            chart_obj = data.get("chart") or {}
            if chart_obj.get("error"):
                last_err = chart_obj["error"]
                continue
            results = chart_obj.get("result") or []
            if not results:
                continue

            result = results[0]
            meta = result.get("meta", {})
            timestamps = result.get("timestamp") or []
            quote = (result.get("indicators", {}).get("quote") or [{}])[0]
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            volumes = quote.get("volume") or []

            history = []
            for i, ts in enumerate(timestamps):
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                history.append({
                    "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "open": opens[i] if (i < len(opens) and opens[i] is not None) else c,
                    "high": highs[i] if (i < len(highs) and highs[i] is not None) else c,
                    "low": lows[i] if (i < len(lows) and lows[i] is not None) else c,
                    "close": c,
                    "volume": volumes[i] if i < len(volumes) else None,
                })

            if history:
                _STOCK_CACHE[cache_key] = {"ts": now, "meta": meta, "history": history}
                return meta, history
        except Exception as exc:
            last_err = exc
            continue

    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=range_str, interval=interval_str)
        if df is not None and not df.empty:
            history = []
            for idx, row in df.iterrows():
                history.append({
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]) if "Volume" in row else None,
                })
            meta = {
                "symbol": symbol.upper(),
                "shortName": symbol.upper(),
                "regularMarketPrice": history[-1]["close"],
                "chartPreviousClose": history[-2]["close"] if len(history) > 1 else history[0]["close"],
            }
            _STOCK_CACHE[cache_key] = {"ts": now, "meta": meta, "history": history}
            return meta, history
    except Exception:
        pass

    raise RuntimeError(f"Unable to fetch chart for {symbol}: {last_err}")


@app.route("/api/stocks/ticker")
def api_stocks_ticker():
    def _load_one(item):
        symbol, label = item
        meta, history = _fetch_yahoo_chart(symbol, range_str="5d", interval_str="1d")
        price = float(meta.get("regularMarketPrice") or (history[-1]["close"] if history else 0))
        prev = float(meta.get("chartPreviousClose") or (history[-2]["close"] if len(history) > 1 else price))
        pct = ((price - prev) / prev * 100.0) if prev else 0.0
        return {
            "name": label,
            "price": f"{price:,.2f}",
            "percent": f"{abs(pct):.2f}%",
            "is_positive": pct >= 0,
        }

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_load_one, item): idx for idx, item in enumerate(DEFAULT_TICKERS)}
        ordered = {}
        for fut in as_completed(futures):
            try:
                ordered[futures[fut]] = fut.result()
            except Exception:
                continue
        stocks = [ordered[i] for i in sorted(ordered)]

    return jsonify({"success": True, "stocks": stocks})


@app.route("/api/stocks/search")
def api_stocks_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})

    results = []
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(q)}&quotesCount=8&newsCount=0"
        raw, _ = _http_get_text(url, timeout=6, extra_headers={"Referer": "https://finance.yahoo.com/"})
        data = json.loads(raw)
        for item in data.get("quotes", []):
            sym = item.get("symbol")
            name = item.get("shortname") or item.get("longname") or sym
            if sym and item.get("quoteType") in ("EQUITY", "ETF", "INDEX", "FUTURE", "CRYPTOCURRENCY"):
                results.append({"symbol": sym, "name": name})
    except Exception:
        ql = q.lower()
        results = [s for s in POPULAR_STOCKS if ql in s["symbol"].lower() or ql in s["name"].lower()]

    return jsonify({"results": results[:8]})


@app.route("/api/stocks/history")
def api_stocks_history():
    symbol = (request.args.get("symbol") or "AAPL").strip().upper()
    period = request.args.get("period", "1y")
    interval = request.args.get("interval", "1d")

    try:
        meta, history = _fetch_yahoo_chart(symbol, range_str=period, interval_str=interval)
        if not history:
            return jsonify({"history": []})

        current_price = float(meta.get("regularMarketPrice") or history[-1]["close"])
        first_close = float(history[0]["close"])
        change = current_price - first_close
        change_percent = (change / first_close * 100.0) if first_close else 0.0

        return jsonify({
            "symbol": meta.get("symbol") or symbol,
            "name": meta.get("longName") or meta.get("shortName") or symbol,
            "current_price": current_price,
            "change": change,
            "change_percent": change_percent,
            "market_cap": meta.get("marketCap"),
            "history": history,
        })
    except Exception as exc:
        return jsonify({"history": [], "error": str(exc)})


@app.route("/api/auth/status")
def api_auth_status():
    user_id = session.get("user_id")
    if user_id:
        conn = get_db()
        user = conn.execute(
            "SELECT username, role FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        conn.close()
        if user:
            return jsonify({
                "logged_in": True,
                "username": user["username"],
                "role": user["role"],
            })

    username = session.get("username")
    if username:
        return jsonify({
            "logged_in": True,
            "username": username,
            "role": session.get("role", "member"),
        })

    return jsonify({"logged_in": False})


@app.route("/api/news/read", methods=["POST"])
def api_news_read():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    title = (payload.get("title") or "").strip()
    source = (payload.get("source") or "News Desk").strip()
    snippet = (payload.get("snippet") or "").strip()
    content_encoded = payload.get("content_encoded") or ""

    paragraphs = []
    real_url = url

    def add_text(text, heading=False):
        normalized = re.sub(r"\s+", " ", html.unescape(text)).strip()
        if len(normalized) < 18:
            return
        lowered = normalized.lower()
        if any(skip in lowered for skip in (
            "cookie", "javascript", "sign in", "subscribe",
            "accept all", "privacy policy", "terms of service",
            "all rights reserved", "newsletter", "continue reading...",
        )):
            return
        key = normalized[:160].lower()
        if any(p["text"][:160].lower() == key for p in paragraphs):
            return
        paragraphs.append({
            "type": "heading" if heading else "paragraph",
            "text": normalized,
        })

    if url:
        try:
            page_html, real_url = _http_get_text(url, timeout=12)

            # Publishers often expose the complete story in JSON-LD.
            for ld_match in re.finditer(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                page_html,
                flags=re.I | re.S,
            ):
                try:
                    ld = json.loads(html.unescape(ld_match.group(1)))
                    objects = ld if isinstance(ld, list) else [ld]
                    for obj in objects:
                        if not isinstance(obj, dict):
                            continue
                        article_body = obj.get("articleBody")
                        if isinstance(article_body, str):
                            for block in re.split(r"\n{2,}|(?<=[.!?])\s+(?=[A-Z(\[])", article_body):
                                add_text(block)
                except Exception:
                    continue

            # Prefer semantic article containers, then main/page content.
            if not paragraphs:
                candidates = []
                for container_tag in ("article", "main"):
                    candidates.extend(re.findall(
                        rf"<{container_tag}\b[^>]*>(.*?)</{container_tag}",
                        page_html,
                        flags=re.I | re.S,
                    ))

                html_blocks = candidates or [page_html]
                for block in html_blocks:
                    for match in re.finditer(
                        r"<(h1|h2|h3|p)[^>]*>(.*?)</\1>",
                        block,
                        flags=re.I | re.S,
                    ):
                        tag = match.group(1).lower()
                        add_text(_strip_tags(match.group(2)), heading=(tag in ("h1", "h2", "h3")))
                        if len(paragraphs) >= 100:
                            break
                    if len(paragraphs) >= 100:
                        break
        except Exception:
            pass

    # RSS content often remains readable when a publisher blocks direct scraping.
    if len(paragraphs) < 3 and content_encoded:
        for match in re.finditer(r"<(h1|h2|h3|p)[^>]*>(.*?)</\1>", content_encoded, flags=re.I | re.S):
            tag = match.group(1).lower()
            add_text(_strip_tags(match.group(2)), heading=(tag in ("h1", "h2", "h3")))

    if len(paragraphs) < 3:
        plain_snippet = _strip_tags(snippet)
        if plain_snippet:
            add_text(plain_snippet)

    if len(paragraphs) == 0:
        paragraphs = [
            {"type": "heading", "text": title or f"{source} — Story Brief"},
            {"type": "paragraph", "text": "The publisher did not provide readable article text for this story."},
            {"type": "paragraph", "text": "Open the publisher's web page with 'Open Web Page' to read the complete article."},
        ]

    return jsonify({
        "success": True,
        "real_url": real_url,
        "paragraphs": paragraphs,
    })


def _is_ai_safety_enabled(conn):
    row = conn.execute(
        "SELECT ai_safety_enabled FROM calendar_settings WHERE id = 1"
    ).fetchone()
    return bool(row and row["ai_safety_enabled"])


def _check_event_safety(conn, user_id, username, title, description="", location=""):
    """Runs Python keyword/regex + LM Studio safety scan ONLY when AI Safety Alert is ON."""
    import json
    import re
    import urllib.request

    if not _is_ai_safety_enabled(conn):
        return True, ""

    combined = f"{title} {description} {location}".strip()
    if not combined:
        return True, ""

    # Stage 1: Fast local Python regex rules (profanity, violence, bullying, NSFW, sensitive PII)
    safety_patterns = [
        (
            r"\b(fuck\w*|shit\w*|bitch\w*|asshole\w*|bastard\w*|dick\w*|cock\w*|pussy\w*|cunt\w*|slut\w*|whore\w*|nigger\w*|faggot\w*|retard\w*)\b",
            "Profanity or inappropriate language detected",
        ),
        (
            r"\b(kill\s+myself|suicide|self[- ]?harm|cut\s+myself|bomb\s+threat|shoot\s+up|bring\s+a\s+(gun|knife|weapon))\b",
            "Unsafe or harmful content detected",
        ),
        (
            r"\b(porn\w*|xxx|nude\w*|naked|sex\s+party|strip\s+club)\b",
            "Age-inappropriate content detected",
        ),
        (
            r"\b(password\s*[:=]\s*\S+|sin\s*[:=]\s*\d{3}[- ]?\d{3}[- ]?\d{3}|\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b)",
            "Sensitive personal/security information detected",
        ),
    ]

    flagged_reason = ""
    for pattern, reason in safety_patterns:
        if re.search(pattern, combined, flags=re.I):
            flagged_reason = reason
            break

    # Stage 2: LM Studio AI scan (if LM Studio is running on port 1234)
    if not flagged_reason:
        lm_url = (
            globals().get("LM_STUDIO_URL")
            or globals().get("LM_STUDIO_API_URL")
            or "http://127.0.0.1:1234/v1/chat/completions"
        )
        prompt = (
            "You are a family calendar safety moderator. Check if this calendar event contains "
            "profanity/bad words, bullying, hate speech, violence, self-harm, or adult/NSFW content.\n"
            'Reply ONLY with "SAFE" or "UNSAFE: <short 5-word reason>".\n'
            f"Event text: {combined}"
        )
        payload = json.dumps(
            {
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 40,
            }
        ).encode("utf-8")
        try:
            req = urllib.request.Request(
                lm_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                reply = (
                    json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]
                    .strip()
                )
                if reply.upper().startswith("UNSAFE"):
                    parts = reply.split(":", 1)
                    flagged_reason = (
                        parts[1].strip() if len(parts) > 1 and parts[1].strip() else "Flagged by AI Safety Guard"
                    )
        except Exception:
            pass

    if flagged_reason:
        conn.execute(
            """
            INSERT INTO safety_alerts (source_app, user_id, username, content, reason)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("Calendar", user_id, username, combined[:300], flagged_reason),
        )
        conn.commit()
        return False, flagged_reason

    return True, ""


def _normalize_shared_with(raw_shared, creator_username=""):
    if not raw_shared:
        return ""
    if isinstance(raw_shared, str):
        items = [x.strip() for x in raw_shared.split(",") if x.strip()]
    elif isinstance(raw_shared, list):
        items = [str(x).strip() for x in raw_shared if str(x).strip()]
    else:
        items = []

    seen = set()
    cleaned = []
    for name in items:
        low = name.lower()
        if creator_username and low == creator_username.lower():
            continue
        if low not in seen:
            seen.add(low)
            cleaned.append(name)
    return ",".join(cleaned)



@app.route("/api/calendar/members", methods=["GET"])
def api_calendar_members():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, role FROM users ORDER BY username ASC"
    ).fetchall()
    conn.close()
    return jsonify({"success": True, "members": [dict(r) for r in rows]})


@app.route("/api/calendar/safety-settings", methods=["GET", "POST"])
def api_calendar_safety_settings():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    conn = get_db()
    ensure_calendar_table(conn)
    user = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    is_parent = bool(user and user["role"] in ("parent", "admin"))

    if request.method == "POST":
        if not is_parent:
            conn.close()
            return jsonify({"success": False, "error": "Only parent/admin accounts can toggle AI Safety Alert"}), 403
        data = request.get_json(silent=True) or {}
        enabled = 1 if data.get("ai_safety_enabled") else 0
        conn.execute(
            """
            UPDATE calendar_settings
            SET ai_safety_enabled = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (enabled, user["username"]),
        )
        conn.commit()

    enabled_now = _is_ai_safety_enabled(conn)
    alerts = []
    if is_parent:
        alert_rows = conn.execute(
            """
            SELECT id, source_app, username, content, reason, created_at
            FROM safety_alerts
            WHERE source_app = 'Calendar'
            ORDER BY id DESC LIMIT 10
            """
        ).fetchall()
        alerts = [dict(r) for r in alert_rows]

    conn.close()
    return jsonify(
        {
            "success": True,
            "ai_safety_enabled": enabled_now,
            "can_toggle": is_parent,
            "alerts": alerts,
        }
    )


@app.route("/api/calendar/events", methods=["GET"])
def api_get_calendar_events():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    conn = get_db()
    ensure_calendar_table(conn)
    user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    me = (user["username"] if user else (session.get("username") or "")).strip().lower()

    rows = conn.execute(
        """
        SELECT * FROM calendar_events
        ORDER BY event_date ASC, start_time ASC
        """
    ).fetchall()
    conn.close()

    visible_events = []
    for r in rows:
        ev = dict(r)
        vis = (ev.get("visibility") or "shared").lower()
        if vis == "shared" or ev["user_id"] == user_id:
            visible_events.append(ev)
        elif vis == "custom":
            recipients = [
                x.strip().lower()
                for x in (ev.get("shared_with") or "").split(",")
                if x.strip()
            ]
            if me and me in recipients:
                visible_events.append(ev)

    return jsonify({"success": True, "events": visible_events})


@app.route("/api/calendar/events", methods=["POST"])
def api_create_calendar_event():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    event_date = (data.get("event_date") or "").strip()
    description = (data.get("description") or "").strip()
    location = (data.get("location") or "").strip()

    category = (data.get("category") or "family").strip().lower()
    if category not in ("family", "school", "activity", "reminder"):
        category = "family"

    if not title or (not event_date and category != "reminder"):
        return jsonify({"success": False, "error": "Title and date are required"}), 400

    conn = get_db()
    ensure_calendar_table(conn)

    user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    username = user["username"] if user else (session.get("username") or "Member")

    # AI Safety Alert check (runs only if enabled)
    is_safe, reason = _check_event_safety(conn, user_id, username, title, description, location)
    if not is_safe:
        conn.close()
        return (
            jsonify(
                {
                    "success": False,
                    "safety_blocked": True,
                    "error": f"AI Safety Alert: {reason}. A parent notification has been logged.",
                }
            ),
            403,
        )

    repeat_rule = (data.get("repeat_rule") or "none").strip().lower()
    if repeat_rule not in ("none", "daily", "weekly", "biweekly", "monthly", "yearly"):
        repeat_rule = "none"

    priority = (data.get("priority") or "medium").strip().lower()
    if priority not in ("low", "medium", "high"):
        priority = "medium"

    subtasks_json = _sanitize_subtasks(data.get("subtasks"))

    raw_vis = (data.get("visibility") or "shared").strip().lower()
    visibility = raw_vis if raw_vis in ("shared", "custom", "private") else "shared"
    shared_with = (
        _normalize_shared_with(data.get("shared_with"), username)
        if visibility == "custom"
        else ""
    )
    if visibility == "custom" and not shared_with:
        visibility = "private"

    cur = conn.execute(
        """
        INSERT INTO calendar_events (
            user_id, username, title, description, event_date, start_time, end_time,
            category, visibility, shared_with, repeat_rule, repeat_until, location,
            priority, subtasks
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            title,
            description,
            event_date,
            (data.get("start_time") or "").strip(),
            (data.get("end_time") or "").strip(),
            category,
            visibility,
            shared_with,
            repeat_rule,
            (data.get("repeat_until") or "").strip(),
            location,
            priority,
            subtasks_json,
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    row = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    return jsonify({"success": True, "id": new_id, "event": dict(row) if row else None})


@app.route("/api/calendar/events/<int:event_id>", methods=["PUT", "DELETE"])
def api_modify_calendar_event(event_id):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    conn = get_db()
    ensure_calendar_table(conn)
    user = conn.execute("SELECT username, role FROM users WHERE id = ?", (user_id,)).fetchone()
    ev = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (event_id,)).fetchone()
    if not ev:
        conn.close()
        return jsonify({"success": False, "error": "Event not found"}), 404

    is_parent = user and user["role"] in ("parent", "admin")
    if ev["user_id"] != user_id and not is_parent:
        conn.close()
        return jsonify({"success": False, "error": "Permission denied"}), 403

    if request.method == "DELETE":
        conn.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}
    new_title = (data.get("title") or ev["title"]).strip()
    new_desc = (
        data.get("description") if data.get("description") is not None else ev["description"]
    ).strip()
    new_loc = (
        data.get("location") if data.get("location") is not None else (ev["location"] or "")
    ).strip()
    new_date = (
        data.get("event_date").strip()
        if data.get("event_date") is not None
        else ev["event_date"]
    )

    # AI Safety Alert check on edits (runs only if enabled)
    actor_name = user["username"] if user else ev["username"]
    is_safe, reason = _check_event_safety(conn, user_id, actor_name, new_title, new_desc, new_loc)
    if not is_safe:
        conn.close()
        return (
            jsonify(
                {
                    "success": False,
                    "safety_blocked": True,
                    "error": f"AI Safety Alert: {reason}. A parent notification has been logged.",
                }
            ),
            403,
        )

    category = (data.get("category") or ev["category"]).strip().lower()
    if category not in ("family", "school", "activity", "reminder"):
        category = ev["category"]

    repeat_rule = (data.get("repeat_rule") or ev["repeat_rule"] or "none").strip().lower()
    if repeat_rule not in ("none", "daily", "weekly", "biweekly", "monthly", "yearly"):
        repeat_rule = "none"

    priority = (data.get("priority") or ev["priority"] or "medium").strip().lower()
    if priority not in ("low", "medium", "high"):
        priority = "medium"

    subtasks_json = (
        _sanitize_subtasks(data.get("subtasks"))
        if data.get("subtasks") is not None
        else (ev["subtasks"] or "[]")
    )

    raw_vis = (data.get("visibility") or ev["visibility"] or "shared").strip().lower()
    visibility = raw_vis if raw_vis in ("shared", "custom", "private") else "shared"
    raw_sw = data.get("shared_with") if data.get("shared_with") is not None else (ev["shared_with"] or "")
    shared_with = (
        _normalize_shared_with(raw_sw, ev["username"])
        if visibility == "custom"
        else ""
    )
    if visibility == "custom" and not shared_with:
        visibility = "private"

    conn.execute(
        """
        UPDATE calendar_events
        SET title = ?, description = ?, event_date = ?, start_time = ?, end_time = ?,
            category = ?, visibility = ?, shared_with = ?, repeat_rule = ?, repeat_until = ?,
            location = ?, priority = ?, subtasks = ?
        WHERE id = ?
        """,
        (
            new_title,
            new_desc,
            new_date,
            (data.get("start_time") if data.get("start_time") is not None else ev["start_time"]).strip(),
            (data.get("end_time") if data.get("end_time") is not None else ev["end_time"]).strip(),
            category,
            visibility,
            shared_with,
            repeat_rule,
            (data.get("repeat_until") if data.get("repeat_until") is not None else (ev["repeat_until"] or "")).strip(),
            new_loc,
            priority,
            subtasks_json,
            event_id,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/calendar/ai-parse", methods=["POST"])
def api_calendar_ai_parse():
    import json
    import re
    import urllib.request
    from datetime import datetime, timedelta

    if not session.get("user_id"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    selected_date = (data.get("selected_date") or "").strip()
    if not text:
        return jsonify({"success": False, "error": "Empty input"}), 400

    fallback = _fallback_parse_event(text, selected_date)

    now = datetime.now()
    calendar_ref = ", ".join(
        f"{(now + timedelta(days=i)).strftime('%Y-%m-%d')} ({(now + timedelta(days=i)).strftime('%A')})"
        for i in range(14)
    )

    system_prompt = (
        "Extract calendar event details into ONLY a valid JSON object. Remove day/time words from title.\n"
        f"Upcoming dates: {calendar_ref}.\n"
        'Example input: "Friday soccer match at 18:00"\n'
        'Example output: {"title":"Soccer match","event_date":"'
        + fallback["event_date"]
        + '","start_time":"18:00","end_time":"","category":"activity","repeat_rule":"none","location":"","visibility":"shared","description":""}\n'
        "Categories: family, school, activity, reminder. Repeat rules: none, daily, weekly, biweekly, monthly, yearly."
    )

    lm_url = (
        globals().get("LM_STUDIO_URL")
        or globals().get("LM_STUDIO_API_URL")
        or "http://127.0.0.1:1234/v1/chat/completions"
    )

    payload = json.dumps(
        {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.1,
            "max_tokens": 220,
        }
    ).encode("utf-8")

    try:
        req = urllib.request.Request(
            lm_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]
            match = re.search(r"\{.*\}", raw, re.S)
            if match:
                parsed = json.loads(match.group(0))
                ai_title = (parsed.get("title") or "").strip()
                # If the LLM left the raw weekday in the title, use the cleaned fallback title
                if not ai_title or ai_title.lower() == text.lower():
                    ai_title = fallback["title"]

                ai_cat = parsed.get("category")
                if ai_cat not in ("family", "school", "activity", "reminder") or (
                    ai_cat == "family" and fallback["category"] != "family"
                ):
                    ai_cat = fallback["category"]

                merged = {
                    "title": ai_title,
                    "event_date": parsed.get("event_date")
                    if re.match(r"^\d{4}-\d{2}-\d{2}$", str(parsed.get("event_date") or ""))
                    else fallback["event_date"],
                    "start_time": parsed.get("start_time")
                    if re.match(r"^\d{2}:\d{2}$", str(parsed.get("start_time") or ""))
                    else fallback["start_time"],
                    "end_time": parsed.get("end_time")
                    if re.match(r"^\d{2}:\d{2}$", str(parsed.get("end_time") or ""))
                    else fallback["end_time"],
                    "category": ai_cat,
                    "repeat_rule": parsed.get("repeat_rule")
                    if parsed.get("repeat_rule") in ("none", "daily", "weekly", "biweekly", "monthly", "yearly")
                    else fallback["repeat_rule"],
                    "location": (parsed.get("location") or fallback["location"]).strip(),
                    "visibility": "private" if parsed.get("visibility") == "private" else fallback["visibility"],
                    "description": (parsed.get("description") or "").strip(),
                }
                return jsonify({"success": True, "source": "LM Studio", "parsed": merged})
    except Exception:
        pass

    return jsonify({"success": True, "source": "Smart Parser", "parsed": fallback})

@app.route("/api/reminders", methods=["GET"])
def api_get_reminders():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    conn = get_db()
    ensure_calendar_table(conn)
    user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    me = (user["username"] if user else (session.get("username") or "")).strip().lower()

    rows = conn.execute(
        """
        SELECT * FROM calendar_events
        WHERE category = 'reminder'
        ORDER BY is_completed ASC,
                 CASE WHEN event_date = '' THEN 1 ELSE 0 END ASC,
                 event_date ASC,
                 start_time ASC,
                 id DESC
        """
    ).fetchall()
    conn.close()

    visible = []
    for r in rows:
        ev = dict(r)
        vis = (ev.get("visibility") or "shared").lower()
        if vis == "shared" or ev["user_id"] == user_id:
            visible.append(ev)
        elif vis == "custom":
            recipients = [
                x.strip().lower()
                for x in (ev.get("shared_with") or "").split(",")
                if x.strip()
            ]
            if me and me in recipients:
                visible.append(ev)

    return jsonify({"success": True, "reminders": visible})


@app.route("/api/reminders/<int:event_id>/toggle", methods=["POST"])
def api_toggle_reminder(event_id):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    occurrence_date = (data.get("occurrence_date") or "").strip()

    conn = get_db()
    ensure_calendar_table(conn)
    ev = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (event_id,)).fetchone()
    if not ev:
        conn.close()
        return jsonify({"success": False, "error": "Reminder not found"}), 404

    rule = (ev["repeat_rule"] or "none").lower()
    if rule != "none" and occurrence_date:
        done_set = {
            d.strip()
            for d in (ev["completed_dates"] or "").split(",")
            if d.strip()
        }
        if occurrence_date in done_set:
            done_set.remove(occurrence_date)
            checked = False
        else:
            done_set.add(occurrence_date)
            checked = True
        new_dates = ",".join(sorted(done_set))
        conn.execute(
            "UPDATE calendar_events SET completed_dates = ? WHERE id = ?",
            (new_dates, event_id),
        )
    else:
        checked = not bool(ev["is_completed"])
        conn.execute(
            "UPDATE calendar_events SET is_completed = ? WHERE id = ?",
            (1 if checked else 0, event_id),
        )

    conn.commit()
    row = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    return jsonify({"success": True, "checked": checked, "event": dict(row)})

def _fallback_parse_event(text, selected_date=""):
    import re
    from datetime import datetime, timedelta

    now = datetime.now()
    clean = (text or "").strip()
    event_date = (selected_date or "").strip() or now.strftime("%Y-%m-%d")
    start_time = ""
    end_time = ""
    repeat_rule = "none"
    category = "family"
    visibility = "shared"
    shared_with = ""
    priority = "medium"
    location = ""

    # 1. Detect custom person-to-person sharing ("share with Mom") or private visibility
    share_match = re.search(
        r"\b(?:share\s+with|only\s+with|shared\s+with)\s+([a-zA-Z0-9_-]{2,24})\b",
        clean,
        flags=re.I,
    )
    if share_match:
        visibility = "custom"
        shared_with = share_match.group(1).strip()
        clean = clean[: share_match.start()] + " " + clean[share_match.end() :]
    elif re.search(r"\b(private|only me|personal)\b", clean, flags=re.I):
        visibility = "private"
        clean = re.sub(r"\b(private|only me|personal)\b", " ", clean, flags=re.I)

    # 2. Detect priority
    if re.search(r"\b(high\s+priority|urgent|asap|very\s+important)\b", clean, flags=re.I):
        priority = "high"
        clean = re.sub(r"\b(high\s+priority|urgent|asap|very\s+important)\b", " ", clean, flags=re.I)
    elif re.search(r"\b(low\s+priority|whenever|no\s+rush)\b", clean, flags=re.I):
        priority = "low"
        clean = re.sub(r"\b(low\s+priority|whenever|no\s+rush)\b", " ", clean, flags=re.I)

    # 3. Detect repeat rule
    if re.search(r"\b(every\s+day|daily)\b", clean, flags=re.I):
        repeat_rule = "daily"
        clean = re.sub(r"\b(every\s+day|daily)\b", " ", clean, flags=re.I)
    elif re.search(r"\b(every\s+2\s+weeks|biweekly|every\s+other\s+week)\b", clean, flags=re.I):
        repeat_rule = "biweekly"
        clean = re.sub(r"\b(every\s+2\s+weeks|biweekly|every\s+other\s+week)\b", " ", clean, flags=re.I)
    elif re.search(r"\b(every\s+week|weekly|every\s+(?:mon|tue|wed|thu|fri|sat|sun)\w*)\b", clean, flags=re.I):
        repeat_rule = "weekly"
        clean = re.sub(r"\b(every\s+week|weekly)\b", " ", clean, flags=re.I)
    elif re.search(r"\b(every\s+month|monthly)\b", clean, flags=re.I):
        repeat_rule = "monthly"
        clean = re.sub(r"\b(every\s+month|monthly)\b", " ", clean, flags=re.I)
    elif re.search(r"\b(every\s+year|yearly|annually|birthday)\b", clean, flags=re.I):
        repeat_rule = "yearly"
        clean = re.sub(r"\b(every\s+year|yearly|annually)\b", " ", clean, flags=re.I)

    # 4. Detect category from keywords
    lower_now = clean.lower()
    if re.search(r"\b(exam|test|quiz|homework|school|class|study|math|french|english|science|project|assignment)\b", lower_now):
        category = "school"
    elif re.search(r"\b(guitar|practice|soccer|match|hockey|swim|music|lesson|game|workout|gym|tournament|piano)\b", lower_now):
        category = "activity"
    elif re.search(r"\b(remind|reminder|pay|bill|call|check|bring|pack|buy|pick\s+up)\b", lower_now):
        category = "reminder"

    # 5. Detect explicit ISO date (YYYY-MM-DD) or relative dates
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", clean)
    if iso_match:
        event_date = iso_match.group(1)
        clean = clean[: iso_match.start()] + " " + clean[iso_match.end() :]
    elif re.search(r"\btomorrow\b", clean, flags=re.I):
        event_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        clean = re.sub(r"\btomorrow\b", " ", clean, flags=re.I)
    elif re.search(r"\btoday\b", clean, flags=re.I):
        event_date = now.strftime("%Y-%m-%d")
        clean = re.sub(r"\btoday\b", " ", clean, flags=re.I)
    else:
        in_days = re.search(r"\bin\s+(\d+)\s+days?\b", clean, flags=re.I)
        if in_days:
            event_date = (now + timedelta(days=int(in_days.group(1)))).strftime("%Y-%m-%d")
            clean = clean[: in_days.start()] + " " + clean[in_days.end() :]
        else:
            weekdays = {
                "monday": 0, "mon": 0,
                "tuesday": 1, "tue": 1, "tues": 1,
                "wednesday": 2, "wed": 2,
                "thursday": 3, "thu": 3, "thurs": 3,
                "friday": 4, "fri": 4,
                "saturday": 5, "sat": 5,
                "sunday": 6, "sun": 6,
            }
            wd_match = re.search(
                r"\b(?:next|this|on|every)?\s*(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thu|friday|fri|saturday|sat|sunday|sun)\b",
                clean,
                flags=re.I,
            )
            if wd_match:
                target_wd = weekdays[wd_match.group(1).lower()]
                days_ahead = (target_wd - now.weekday()) % 7
                if days_ahead == 0 and "today" not in clean.lower():
                    days_ahead = 7
                event_date = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
                clean = clean[: wd_match.start()] + " " + clean[wd_match.end() :]

    # 6. Detect explicit Month + Day (e.g. "Dec 9" or "December 9th")
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    md_match = re.search(
        r"\b(?:on\s+)?(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
        clean,
        flags=re.I,
    )
    if md_match:
        m_num = months[md_match.group(1).lower()]
        d_num = int(md_match.group(2))
        yr = now.year if (m_num, d_num) >= (now.month, now.day) else now.year + 1
        try:
            event_date = datetime(yr, m_num, d_num).strftime("%Y-%m-%d")
            clean = clean[: md_match.start()] + " " + clean[md_match.end() :]
        except ValueError:
            pass

    # 7. Detect time or time range (e.g. "17:00", "5pm", "5:30 pm to 6:30 pm")
    def _to_24h(h_str, m_str, ampm):
        h = int(h_str)
        m = int(m_str or 0)
        if ampm:
            ap = ampm.lower()
            if ap == "pm" and h < 12:
                h += 12
            elif ap == "am" and h == 12:
                h = 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
        return ""

    range_match = re.search(
        r"\b(?:from\s+|at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|to|until)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
        clean,
        flags=re.I,
    )
    if range_match and (range_match.group(2) or range_match.group(3) or range_match.group(5) or range_match.group(6)):
        ap1 = range_match.group(3) or range_match.group(6)
        ap2 = range_match.group(6) or range_match.group(3)
        start_time = _to_24h(range_match.group(1), range_match.group(2), ap1)
        end_time = _to_24h(range_match.group(4), range_match.group(5), ap2)
        clean = clean[: range_match.start()] + " " + clean[range_match.end() :]
    else:
        time_match = re.search(
            r"\b(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?\b|\b(?:at\s+)?(\d{1,2})\s*(am|pm)\b",
            clean,
            flags=re.I,
        )
        if time_match:
            if time_match.group(1):
                start_time = _to_24h(time_match.group(1), time_match.group(2), time_match.group(3))
            else:
                start_time = _to_24h(time_match.group(4), "0", time_match.group(5))
            clean = clean[: time_match.start()] + " " + clean[time_match.end() :]

    # 8. Detect location ("at <Capitalized Place>" at end of phrase)
    loc_match = re.search(r"\bat\s+([A-Z][\w\s'-]{2,32})$", clean.strip())
    if loc_match:
        location = loc_match.group(1).strip()
        clean = clean.strip()[: loc_match.start()]

    # 9. Clean leftover connector words and format title
    clean = re.sub(r"\s+", " ", clean).strip(" ,.-")
    clean = re.sub(r"^(?:on|at|for)\s+|\s+(?:on|at|for)$", "", clean, flags=re.I).strip(" ,.-")
    title = (clean[0].upper() + clean[1:]) if clean else text.strip()

    return {
        "title": title,
        "event_date": event_date,
        "start_time": start_time,
        "end_time": end_time,
        "category": category,
        "repeat_rule": repeat_rule,
        "location": location,
        "visibility": visibility,
        "shared_with": shared_with,
        "priority": priority,
        "description": "",
    }

def _sanitize_subtasks(raw_subtasks):
    import json
    if isinstance(raw_subtasks, str):
        try:
            raw_subtasks = json.loads(raw_subtasks)
        except Exception:
            return "[]"
    if not isinstance(raw_subtasks, list):
        return "[]"
    cleaned = []
    for item in raw_subtasks[:30]:
        if isinstance(item, dict):
            txt = str(item.get("text") or "").strip()
            if txt:
                cleaned.append({"text": txt[:160], "done": bool(item.get("done"))})
    return json.dumps(cleaned)

@app.route("/api/reminders/<int:event_id>/snooze", methods=["POST"])
def api_snooze_reminder(event_id):
    from datetime import datetime, timedelta

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "tomorrow").strip().lower()

    conn = get_db()
    ensure_calendar_table(conn)
    ev = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (event_id,)).fetchone()
    if not ev:
        conn.close()
        return jsonify({"success": False, "error": "Reminder not found"}), 404

    now = datetime.now()
    base_date_str = ev["event_date"] or now.strftime("%Y-%m-%d")
    try:
        base_dt = datetime.strptime(base_date_str, "%Y-%m-%d")
    except ValueError:
        base_dt = now

    new_date = base_date_str
    new_time = ev["start_time"] or ""

    if mode == "15m":
        target_dt = now + timedelta(minutes=15)
        new_date = target_dt.strftime("%Y-%m-%d")
        new_time = target_dt.strftime("%H:%M")
    elif mode == "next_week":
        anchor = max(base_dt, now.replace(hour=0, minute=0, second=0, microsecond=0))
        new_date = (anchor + timedelta(days=7)).strftime("%Y-%m-%d")
    else:  # "tomorrow"
        new_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    conn.execute(
        "UPDATE calendar_events SET event_date = ?, start_time = ?, is_completed = 0 WHERE id = ?",
        (new_date, new_time, event_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    return jsonify({"success": True, "event": dict(row)})


@app.route("/api/reminders/clear-completed", methods=["POST"])
def api_clear_completed_reminders():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    conn = get_db()
    ensure_calendar_table(conn)
    user = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    is_parent = bool(user and user["role"] in ("parent", "admin"))

    if is_parent:
        cur = conn.execute(
            "DELETE FROM calendar_events WHERE category = 'reminder' AND is_completed = 1 AND (repeat_rule = 'none' OR repeat_rule = '')"
        )
    else:
        cur = conn.execute(
            "DELETE FROM calendar_events WHERE category = 'reminder' AND is_completed = 1 AND user_id = ? AND (repeat_rule = 'none' OR repeat_rule = '')"
            ,
            (user_id,),
        )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return jsonify({"success": True, "deleted": deleted})


def _http_get_json(url, timeout=8):
    import json
    import ssl
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "HomeNest-Weather/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as first_err:
        if url.startswith("https://"):
            try:
                ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as second_err:
                print(f"[HomeNest Weather] Failed {url} -> {first_err} | retry -> {second_err}")
                raise
        print(f"[HomeNest Weather] Failed {url} -> {first_err}")
        raise


_MAC_LOC_CACHE = {"ts": 0.0, "lat": None, "lon": None}


def _ensure_weather_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_home (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            label TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            updated_by TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def _reverse_geocode_city(lat, lon):
    import urllib.parse

    # 1. OpenStreetMap Nominatim (preserves exact towns/suburbs like Candiac)
    try:
        nom_url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
            {
                "format": "jsonv2",
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "zoom": 14,
                "addressdetails": 1,
                "accept-language": "en",
            }
        )
        nom = _http_get_json(nom_url, timeout=5)
        addr = nom.get("address") or {}
        city = (
            addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("municipality")
            or addr.get("suburb")
            or ""
        )
        region = addr.get("state") or addr.get("province") or ""
        country = addr.get("country") or ""
        if city:
            label = ", ".join(p for p in (city, region, country) if p)
            return city, region, country, label
    except Exception:
        pass

    # 2. BigDataCloud fallback
    try:
        rev_url = "https://api.bigdatacloud.net/data/reverse-geocode-client?" + urllib.parse.urlencode(
            {"latitude": round(lat, 4), "longitude": round(lon, 4), "localityLanguage": "en"}
        )
        rev = _http_get_json(rev_url, timeout=5)
        city = rev.get("locality") or rev.get("city") or rev.get("principalSubdivision") or "Current Location"
        region = rev.get("principalSubdivision") or ""
        country = rev.get("countryName") or ""
        label = ", ".join(p for p in (city, region, country) if p)
        return city, region, country, label
    except Exception:
        return "Current Location", "", "", f"{lat:.3f}, {lon:.3f}"


def _detect_macos_corelocation():
    import subprocess
    import sys
    import time

    if sys.platform != "darwin":
        return None

    now = time.time()
    if _MAC_LOC_CACHE["lat"] is not None and (now - _MAC_LOC_CACHE["ts"]) < 1800:
        return _MAC_LOC_CACHE["lat"], _MAC_LOC_CACHE["lon"]

    swift_code = """
import CoreLocation
import Foundation

class LocDelegate: NSObject, CLLocationManagerDelegate {
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        if let loc = locations.last, loc.horizontalAccuracy >= 0 {
            print("\\(loc.coordinate.latitude),\\(loc.coordinate.longitude)")
            fflush(stdout)
            exit(0)
        }
    }
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        exit(1)
    }
}

let mgr = CLLocationManager()
let del = LocDelegate()
mgr.delegate = del
mgr.desiredAccuracy = kCLLocationAccuracyBest
mgr.startUpdatingLocation()
RunLoop.main.run(until: Date(timeIntervalSinceNow: 3.5))
exit(1)
"""
    try:
        res = subprocess.run(
            ["swift", "-e", swift_code],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and "," in res.stdout:
            lat_s, lon_s = res.stdout.strip().splitlines()[-1].split(",", 1)
            lat_v, lon_v = round(float(lat_s), 4), round(float(lon_s), 4)
            _MAC_LOC_CACHE.update({"ts": now, "lat": lat_v, "lon": lon_v})
            return lat_v, lon_v
    except Exception:
        pass
    return None


def _is_lan_client(remote_addr):
    addr = (remote_addr or "").strip()
    return (
        addr.startswith("192.168.")
        or addr.startswith("10.")
        or addr.startswith("127.")
        or addr == "::1"
        or any(addr.startswith(f"172.{i}.") for i in range(16, 32))
    )


@app.route("/api/weather/home", methods=["POST"])
def api_weather_set_home():
    if not session.get("user_id"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    lat = data.get("lat")
    lon = data.get("lon")
    if not label or lat is None or lon is None:
        return jsonify({"success": False, "error": "Missing location details"}), 400

    conn = get_db()
    _ensure_weather_table(conn)
    conn.execute(
        """
        INSERT INTO weather_home (id, label, lat, lon, updated_by, updated_at)
        VALUES (1, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            label = excluded.label,
            lat = excluded.lat,
            lon = excluded.lon,
            updated_by = excluded.updated_by,
            updated_at = CURRENT_TIMESTAMP
        """,
        (label, round(float(lat), 4), round(float(lon), 4), session.get("username") or ""),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "label": label})


@app.route("/api/weather/detect-location", methods=["GET"])
def api_weather_detect_location():
    if not session.get("user_id"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    force_live = request.args.get("force") == "1"

    # 1. Exact browser GPS / Wi-Fi coordinates (when available)
    if lat is not None and lon is not None:
        city, region, country, label = _reverse_geocode_city(lat, lon)
        return jsonify(
            {
                "success": True,
                "source": "Browser GPS",
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "city": city,
                "region": region,
                "country": country,
                "label": label,
            }
        )

    # 2. Saved Home Base in SQLite (prevents ISP Montreal gateway override on home Wi-Fi)
    if _is_lan_client(request.remote_addr):
        conn = get_db()
        _ensure_weather_table(conn)
        home = conn.execute("SELECT label, lat, lon FROM weather_home WHERE id = 1").fetchone()
        conn.close()
        if home:
            return jsonify(
                {
                    "success": True,
                    "source": "Home Wi-Fi Base",
                    "lat": home["lat"],
                    "lon": home["lon"],
                    "city": home["label"].split(",")[0].strip(),
                    "region": "",
                    "country": "",
                    "label": home["label"],
                }
            )

    # 3. macOS Wi-Fi CoreLocation on the Mac mini host (~15m accuracy)
    if force_live or _is_lan_client(request.remote_addr):
        mac_coords = _detect_macos_corelocation()
        if mac_coords:
            m_lat, m_lon = mac_coords
            city, region, country, label = _reverse_geocode_city(m_lat, m_lon)
            return jsonify(
                {
                    "success": True,
                    "source": "Mac Wi-Fi Positioning",
                    "lat": m_lat,
                    "lon": m_lon,
                    "city": city,
                    "region": region,
                    "country": country,
                    "label": label,
                }
            )

    # 4. IP-based geolocation fallback
    for provider_url, parser in (
        (
            "https://ipapi.co/json/",
            lambda d: (
                float(d["latitude"]),
                float(d["longitude"]),
                d.get("city") or "",
                d.get("region") or "",
                d.get("country_name") or "",
            ),
        ),
        (
            "http://ip-api.com/json/?fields=status,country,regionName,city,lat,lon",
            lambda d: (
                float(d["lat"]),
                float(d["lon"]),
                d.get("city") or "",
                d.get("regionName") or "",
                d.get("country") or "",
            )
            if d.get("status") == "success"
            else (_ for _ in ()).throw(ValueError("ip-api failed")),
        ),
    ):
        try:
            raw = _http_get_json(provider_url, timeout=4)
            d_lat, d_lon, city, region, country = parser(raw)
            label = ", ".join(p for p in (city, region, country) if p)
            return jsonify(
                {
                    "success": True,
                    "source": "IP Location",
                    "lat": round(d_lat, 4),
                    "lon": round(d_lon, 4),
                    "city": city or "Detected Area",
                    "region": region,
                    "country": country,
                    "label": label or "Detected Area",
                }
            )
        except Exception:
            continue

    return jsonify(
        {
            "success": True,
            "source": "Default Location",
            "lat": 45.3834,
            "lon": -73.5166,
            "city": "Candiac",
            "region": "Quebec",
            "country": "Canada",
            "label": "Candiac, Quebec, Canada",
        }
    )



@app.route("/api/weather/search", methods=["GET"])
def api_weather_search():
    import urllib.parse

    if not session.get("user_id"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"success": True, "results": []})

    try:
        url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": q, "count": 7, "language": "en", "format": "json"}
        )
        data = _http_get_json(url, timeout=5)
        results = []
        for item in data.get("results") or []:
            parts = [item.get("name"), item.get("admin1"), item.get("country")]
            results.append(
                {
                    "name": item.get("name") or "",
                    "region": item.get("admin1") or "",
                    "country": item.get("country") or "",
                    "label": ", ".join(p for p in parts if p),
                    "lat": round(float(item["latitude"]), 4),
                    "lon": round(float(item["longitude"]), 4),
                    "timezone": item.get("timezone") or "auto",
                }
            )
        return jsonify({"success": True, "results": results})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 502


@app.route("/api/weather/forecast", methods=["GET"])
def api_weather_forecast():
    import urllib.parse

    if not session.get("user_id"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    label = (request.args.get("label") or "").strip()
    if lat is None or lon is None:
        return jsonify({"success": False, "error": "Missing coordinates"}), 400

    forecast_params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join(
            [
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "is_day",
                "precipitation",
                "weather_code",
                "cloud_cover",
                "pressure_msl",
                "wind_speed_10m",
                "wind_direction_10m",
                "wind_gusts_10m",
            ]
        ),
        "hourly": ",".join(
            [
                "temperature_2m",
                "precipitation_probability",
                "weather_code",
                "is_day",
                "wind_speed_10m",
            ]
        ),
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "sunrise",
                "sunset",
                "uv_index_max",
                "wind_speed_10m_max",
            ]
        ),
        "timezone": "auto",
        "forecast_days": 7,
    }

    try:
        fc_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(forecast_params)
        fc = _http_get_json(fc_url, timeout=7)
    except Exception as exc:
        return jsonify({"success": False, "error": f"Weather fetch failed: {exc}"}), 502

    aqi_data = {}
    try:
        aqi_url = "https://air-quality-api.open-meteo.com/v1/air-quality?" + urllib.parse.urlencode(
            {
                "latitude": lat,
                "longitude": lon,
                "current": "us_aqi,pm2_5,pm10,uv_index",
                "timezone": "auto",
            }
        )
        aqi_raw = _http_get_json(aqi_url, timeout=4)
        aqi_data = aqi_raw.get("current") or {}
    except Exception:
        aqi_data = {}

    return jsonify(
        {
            "success": True,
            "location_name": label,
            "timezone": fc.get("timezone") or "Local",
            "elevation": fc.get("elevation"),
            "current": fc.get("current") or {},
            "hourly": fc.get("hourly") or {},
            "daily": fc.get("daily") or {},
            "air_quality": aqi_data,
        }
    )


@app.route("/api/weather/ai-brief", methods=["POST"])
def api_weather_ai_brief():
    import json
    import urllib.request

    if not session.get("user_id"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    loc = (data.get("location") or "your area").strip()
    temp = data.get("temp")
    feels = data.get("feels_like")
    cond = (data.get("condition") or "").strip()
    wind = data.get("wind")
    precip_prob = data.get("precip_prob")
    high = data.get("high")
    low = data.get("low")

    # Deterministic fallback advice if LM Studio is offline
    advice_parts = []
    if temp is not None:
        if temp <= -5:
            advice_parts.append("Bundle up in a heavy winter coat, gloves, and warm layers.")
        elif temp <= 5:
            advice_parts.append("Wear a warm jacket or fleece—it's chilly outside.")
        elif temp <= 16:
            advice_parts.append("A light jacket or hoodie is ideal for today's mild temperatures.")
        elif temp <= 26:
            advice_parts.append("Comfortable weather for a t-shirt and outdoor activities.")
        else:
            advice_parts.append("Stay hydrated, wear light breathable clothes, and use sunscreen.")
    if (precip_prob or 0) >= 40 or any(w in cond.lower() for w in ("rain", "drizzle", "shower", "snow", "storm")):
        advice_parts.append("Bring an umbrella or waterproof shell for expected precipitation.")
    if (wind or 0) >= 30:
        advice_parts.append(f"Expect brisk gusts around {round(wind)} km/h.")
    fallback_brief = " ".join(advice_parts) or f"Currently {cond.lower()} in {loc} with a high of {high}°C and low of {low}°C."

    prompt = (
        f"Give a helpful, friendly 2-sentence family weather and outfit briefing for {loc}. "
        f"Current: {temp}°C (feels like {feels}°C), {cond}, wind {wind} km/h, "
        f"today's high {high}°C / low {low}°C, rain/snow chance {precip_prob}%. "
        "Write 2 complete sentences with no markdown and stay under 50 words."
    )

    try:
        payload = json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "You are HomeNest's concise family weather assistant."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 260,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:1234/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            msg = res_json["choices"][0]["message"]["content"].strip()
            if msg:
                return jsonify({"success": True, "brief": msg, "source": "LM Studio"})
    except Exception:
        pass

    return jsonify({"success": True, "brief": fallback_brief, "source": "Smart Forecast"})


def _fallback_weather_chat(question, loc, structured, unit_sym):
    q = (question or "").lower()
    cur = structured.get("current") or {}
    hourly = structured.get("hourly_summary") or {}
    daily = structured.get("daily") or []

    temp = cur.get("temp")
    feels = cur.get("feels_like")
    cond = cur.get("condition", "variable conditions")
    wind = cur.get("wind")
    gusts = cur.get("gusts")
    humidity = cur.get("humidity")
    aqi = cur.get("aqi")
    uv = cur.get("uv_max")
    hi0 = daily[0]["high"] if daily else None
    lo0 = daily[0]["low"] if daily else None
    pop0 = daily[0]["precip_prob"] if daily else 0

    if any(k in q for k in ("wear", "outfit", "cloth", "jacket", "coat", "dress")):
        parts = []
        if temp is not None:
            temp_c = (temp - 32) * 5 / 9 if unit_sym == "°F" else temp
            if temp_c <= -5:
                parts.append(f"At {temp}{unit_sym} (feels like {feels}{unit_sym}), wear a heavy winter coat, insulated gloves, and warm layers.")
            elif temp_c <= 5:
                parts.append(f"It's chilly at {temp}{unit_sym} (feels like {feels}{unit_sym})—go with a warm jacket, sweater, or fleece.")
            elif temp_c <= 16:
                parts.append(f"Mild at {temp}{unit_sym}; a light jacket or hoodie is ideal, especially with a low of {lo0}{unit_sym}.")
            else:
                parts.append(f"Comfortable at {temp}{unit_sym}; light breathable layers work well.")
        if (pop0 or 0) >= 35 or any(w in cond.lower() for w in ("rain", "drizzle", "snow", "shower")):
            parts.append(f"Bring an umbrella or waterproof layer ({pop0}% precipitation chance).")
        return " ".join(parts)

    if any(k in q for k in ("rain", "snow", "precip", "umbrella", "storm", "wet")):
        peak_pop = hourly.get("max_pop_24h", pop0)
        peak_time = hourly.get("peak_pop_time", "later today")
        wet_days = [f"{d['day']} ({d['precip_prob']}%)" for d in daily if (d.get("precip_prob") or 0) >= 35]
        msg = f"In {loc}, the next 24 hours peak at a {peak_pop}% chance of precipitation around {peak_time}."
        if wet_days:
            msg += f" Later in the week, watch for precipitation on {', '.join(wet_days[:3])}."
        else:
            msg += " No major rain or snow systems stand out in the 7-day outlook."
        return msg

    if any(k in q for k in ("wind", "gust", "air", "aqi", "uv", "sun", "humidity")):
        return (
            f"In {loc}, winds are {wind} km/h (gusts up to {gusts} km/h) with {humidity}% humidity. "
            f"US Air Quality Index is {aqi if aqi is not None else 'N/A'}, max UV index is {uv}, "
            f"sunrise is at {cur.get('sunrise', '--:--')}, and sunset is at {cur.get('sunset', '--:--')}."
        )

    if any(k in q for k in ("week", "7-day", "7 day", "tomorrow", "weekend", "outlook")):
        if len(daily) >= 2:
            tmr = daily[1]
            week_his = [d["high"] for d in daily if d.get("high") is not None]
            week_los = [d["low"] for d in daily if d.get("low") is not None]
            return (
                f"Tomorrow ({tmr['day']}) in {loc} looks {tmr['condition'].lower()} with a high of {tmr['high']}{unit_sym}, "
                f"low of {tmr['low']}{unit_sym}, and {tmr['precip_prob']}% precipitation chance. "
                f"Across the next 7 days, highs range from {min(week_los)}{unit_sym} up to {max(week_his)}{unit_sym}."
            )

    # Default full summary
    tmr_note = ""
    if len(daily) >= 2:
        tmr = daily[1]
        tmr_note = f" Tomorrow ({tmr['day']}) will be {tmr['condition'].lower()} ({tmr['high']}{unit_sym} / {tmr['low']}{unit_sym})."
    return (
        f"Right now in {loc}, it's {temp}{unit_sym} (feels like {feels}{unit_sym}) with {cond.lower()} and {wind} km/h winds. "
        f"Today's high is {hi0}{unit_sym}, tonight's low is {lo0}{unit_sym}, and precipitation chance is {pop0}%.{tmr_note}"
    )


@app.route("/api/weather/ai-chat", methods=["POST"])
def api_weather_ai_chat():
    import json
    import urllib.request

    if not session.get("user_id"):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    loc = (data.get("location") or "Current Location").strip()
    question = (data.get("question") or "Summarize the weather forecast.").strip()
    weather_context = (data.get("weather_context") or "").strip()
    structured = data.get("structured") or {}
    unit_sym = data.get("unit") or "°C"
    history = data.get("history") or []

    system_prompt = (
        f"You are HomeNest Weather AI, a concise, helpful family weather assistant for {loc}. "
        f"Answer questions using ONLY the live forecast data below (units are in {unit_sym}). "
        "Keep answers clear, practical, and under 80 words with no markdown tables.\n\n"
        f"LIVE FORECAST DATA:\n{weather_context}"
    )

    messages = [{"role": "system", "content": system_prompt}]
    for turn in history[-6:]:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    try:
        payload = json.dumps(
            {
                "messages": messages,
                "temperature": 0.35,
                "max_tokens": 350,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:1234/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            reply = res_json["choices"][0]["message"]["content"].strip()
            if reply:
                return jsonify({"success": True, "reply": reply, "source": "LM Studio"})
    except Exception:
        pass

    fallback_reply = _fallback_weather_chat(question, loc, structured, unit_sym)
    return jsonify({"success": True, "reply": fallback_reply, "source": "Smart Forecast"})


if __name__ == "__main__":
    print(f"HomeNest storage mounted at: {STORAGE_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=True)