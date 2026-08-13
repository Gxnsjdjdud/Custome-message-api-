import os
import re
import sqlite3
from datetime import datetime
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, session, send_file

load_dotenv()

app = Flask(__name__)

# =========================
# CONFIG
# =========================

app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-key")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")

API_TOKEN = os.getenv("API_TOKEN", "")

SMS_API_URL = "https://api.bdbulksms.net/api.php"
GENERAL_API_URL = "https://api.bdbulksms.net/g_api.php"

DB_FILE = "sms_history.db"


# =========================
# DATABASE
# =========================

def init_db():
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sms_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL,
            response TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


init_db()


# =========================
# AUTH
# =========================

def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return jsonify({
                "success": False,
                "error": "Unauthorized"
            }), 401

        return func(*args, **kwargs)

    return wrapper


# =========================
# BANGLADESH NUMBER VALIDATION
# =========================

def normalize_bd_number(number):
    if not number:
        return None

    number = re.sub(r"\s+", "", str(number))
    number = number.replace("-", "")

    # +8801XXXXXXXXX
    if number.startswith("+880"):
        number = "0" + number[4:]

    # 8801XXXXXXXXX
    elif number.startswith("880"):
        number = "0" + number[3:]

    # 1XXXXXXXXX
    elif re.fullmatch(r"1[3-9]\d{8}", number):
        number = "0" + number

    if re.fullmatch(r"01[3-9]\d{8}", number):
        return number

    return None


# =========================
# FRONTEND
# =========================

@app.route("/")
def index():
    return send_file("index.html")


# =========================
# LOGIN
# =========================

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}

    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session["logged_in"] = True

        return jsonify({
            "success": True,
            "message": "Login successful"
        })

    return jsonify({
        "success": False,
        "error": "Invalid username or password"
    }), 401


@app.post("/api/logout")
@login_required
def logout():
    session.clear()

    return jsonify({
        "success": True
    })


@app.get("/api/me")
def current_user():
    return jsonify({
        "logged_in": bool(session.get("logged_in"))
    })


# =========================
# SEND SINGLE SMS
# =========================

@app.post("/api/send-sms")
@login_required
def send_sms():
    if not API_TOKEN:
        return jsonify({
            "success": False,
            "error": "API_TOKEN is not configured"
        }), 500

    data = request.get_json(silent=True) or {}

    raw_phone = str(data.get("phone", "")).strip()
    message = str(data.get("message", "")).strip()

    phone = normalize_bd_number(raw_phone)

    if not phone:
        return jsonify({
            "success": False,
            "error": "Invalid Bangladesh mobile number"
        }), 400

    if not message:
        return jsonify({
            "success": False,
            "error": "Message is required"
        }), 400

    if len(message) > 1000:
        return jsonify({
            "success": False,
            "error": "Message is too long"
        }), 400

    payload = {
        "token": API_TOKEN,
        "to": phone,
        "message": message
    }

    try:
        response = requests.post(
            SMS_API_URL,
            data=payload,
            timeout=20
        )

        response_text = response.text[:2000]

        success = response.ok

        conn = sqlite3.connect(DB_FILE)

        conn.execute("""
            INSERT INTO sms_history
            (phone, message, status, response, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            phone,
            message,
            "success" if success else "failed",
            response_text,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

        conn.commit()
        conn.close()

        return jsonify({
            "success": success,
            "phone": phone,
            "response": response_text
        }), 200 if success else 502

    except requests.RequestException as e:

        conn = sqlite3.connect(DB_FILE)

        conn.execute("""
            INSERT INTO sms_history
            (phone, message, status, response, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            phone,
            message,
            "failed",
            str(e),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

        conn.commit()
        conn.close()

        return jsonify({
            "success": False,
            "error": "SMS API connection failed"
        }), 502


# =========================
# MULTIPLE NUMBERS SMS
# =========================

@app.post("/api/send-bulk")
@login_required
def send_bulk():
    if not API_TOKEN:
        return jsonify({
            "success": False,
            "error": "API_TOKEN is not configured"
        }), 500

    data = request.get_json(silent=True) or {}

    numbers = data.get("numbers", [])
    message = str(data.get("message", "")).strip()

    if not isinstance(numbers, list):
        return jsonify({
            "success": False,
            "error": "numbers must be an array"
        }), 400

    if not message:
        return jsonify({
            "success": False,
            "error": "Message is required"
        }), 400

    # Safety limit for one browser request.
    if len(numbers) > 100:
        return jsonify({
            "success": False,
            "error": "Maximum 100 numbers per request"
        }), 400

    valid_numbers = []

    for number in numbers:
        normalized = normalize_bd_number(number)

        if normalized and normalized not in valid_numbers:
            valid_numbers.append(normalized)

    if not valid_numbers:
        return jsonify({
            "success": False,
            "error": "No valid Bangladesh numbers found"
        }), 400

    results = []

    for phone in valid_numbers:

        payload = {
            "token": API_TOKEN,
            "to": phone,
            "message": message
        }

        try:
            response = requests.post(
                SMS_API_URL,
                data=payload,
                timeout=20
            )

            response_text = response.text[:2000]
            status = "success" if response.ok else "failed"

        except requests.RequestException as e:
            response_text = str(e)
            status = "failed"

        conn = sqlite3.connect(DB_FILE)

        conn.execute("""
            INSERT INTO sms_history
            (phone, message, status, response, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            phone,
            message,
            status,
            response_text,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

        conn.commit()
        conn.close()

        results.append({
            "phone": phone,
            "status": status,
            "response": response_text
        })

    success_count = sum(
        1 for item in results
        if item["status"] == "success"
    )

    return jsonify({
        "success": True,
        "total": len(results),
        "sent": success_count,
        "failed": len(results) - success_count,
        "results": results
    })


# =========================
# BALANCE
# =========================

@app.get("/api/balance")
@login_required
def balance():
    if not API_TOKEN:
        return jsonify({
            "success": False,
            "error": "API_TOKEN is not configured"
        }), 500

    try:
        response = requests.get(
            GENERAL_API_URL,
            params={
                "token": API_TOKEN,
                "balance": "",
                "json": ""
            },
            timeout=15
        )

        return jsonify({
            "success": response.ok,
            "data": response.json()
            if "application/json" in response.headers.get(
                "Content-Type", ""
            )
            else response.text
        })

    except Exception:
        return jsonify({
            "success": False,
            "error": "Unable to fetch balance"
        }), 502


# =========================
# STATISTICS
# =========================

@app.get("/api/stats")
@login_required
def stats():
    conn = sqlite3.connect(DB_FILE)

    total = conn.execute(
        "SELECT COUNT(*) FROM sms_history"
    ).fetchone()[0]

    successful = conn.execute(
        "SELECT COUNT(*) FROM sms_history WHERE status='success'"
    ).fetchone()[0]

    failed = conn.execute(
        "SELECT COUNT(*) FROM sms_history WHERE status='failed'"
    ).fetchone()[0]

    conn.close()

    return jsonify({
        "success": True,
        "total": total,
        "successful": successful,
        "failed": failed
    })


# =========================
# SMS HISTORY
# =========================

@app.get("/api/history")
@login_required
def history():
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50

    limit = max(1, min(limit, 200))

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, phone, message, status, response, created_at
        FROM sms_history
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()

    conn.close()

    return jsonify({
        "success": True,
        "history": [dict(row) for row in rows]
    })


# =========================
# HEALTH CHECK
# =========================

@app.get("/health")
def health():
    return jsonify({
        "status": "ok"
    })


# =========================
# RUN
# =========================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
  )
