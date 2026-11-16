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
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "homenest-family-secret-key-change-me"

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


# --- AUTH ROUTES ---
@app.route("/")
def index():
    return render_template("index.html")


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
            visible = [r for r in rows if r["owner_id"] == user["id"] or user["role"] == "parent"]
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


if __name__ == "__main__":
    print(f"HomeNest storage mounted at: {STORAGE_DIR}")
    app.run(host="0.0.0.0", port=5000, debug=True)