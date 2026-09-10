import io
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime

import pyotp
import qrcode
from flask import Flask, request, render_template, send_file

app = Flask(__name__)

QR_INTERVAL_SECONDS = 10
FORM_SESSION_TIMEOUT_SECONDS = 600

SECRET = pyotp.random_base32()
TOTP = pyotp.TOTP(SECRET, interval=QR_INTERVAL_SECONDS, digits=8)

CLASS_SESSION_ID = str(uuid.uuid4())

DB_PATH = "attendance.db"


def init_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            student_id TEXT NOT NULL,
            class_session_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            UNIQUE(student_id, class_session_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_scans (
            session_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()


def cleanup_expired_sessions(conn):
    cutoff = time.time() - FORM_SESSION_TIMEOUT_SECONDS
    conn.execute("DELETE FROM pending_scans WHERE created_at < ?", (cutoff,))


@app.route("/display")
def display():
    return render_template("display.html")


@app.route("/qr.png")
def qr_png():
    token = TOTP.now()
    attend_url = f"{request.host_url}attend?token={token}"

    img = qrcode.make(attend_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/attend", methods=["GET"])
def attend_form():
    token = request.args.get("token", "")

    if not TOTP.verify(token, valid_window=1):
        return render_template(
            "attend.html",
            error="QR kodun süresi doldu, lütfen ekrandaki güncel QR kodu tekrar okutun.",
            session_id=None,
        )

    session_id = secrets.token_urlsafe(24)
    conn = sqlite3.connect(DB_PATH)
    cleanup_expired_sessions(conn)
    conn.execute(
        "INSERT INTO pending_scans (session_id, created_at) VALUES (?, ?)",
        (session_id, time.time()),
    )
    conn.commit()
    conn.close()

    return render_template("attend.html", error=None, session_id=session_id)


@app.route("/attend", methods=["POST"])
def attend_submit():
    session_id = request.form.get("session_id", "")
    full_name = request.form.get("full_name", "").strip()
    student_id = request.form.get("student_id", "").strip()

    conn = sqlite3.connect(DB_PATH)
    cleanup_expired_sessions(conn)

    row = conn.execute(
        "SELECT created_at FROM pending_scans WHERE session_id = ?", (session_id,)
    ).fetchone()

    if row is None:
        conn.close()
        return render_template(
            "attend.html",
            error="Oturumun süresi doldu ya da geçersiz. Lütfen ekrandaki QR kodu tekrar okutun.",
            session_id=None,
        )

    if not full_name or not student_id:
        conn.close()
        return render_template(
            "attend.html",
            error="Lütfen ad soyad ve öğrenci numarasını eksiksiz girin.",
            session_id=session_id,
        )

    try:
        conn.execute(
            "INSERT INTO attendance (full_name, student_id, class_session_id, timestamp) VALUES (?, ?, ?, ?)",
            (full_name, student_id, CLASS_SESSION_ID, datetime.now().isoformat()),
        )
        conn.commit()
        message = f"{full_name} ({student_id}) için yoklama kaydedildi."
    except sqlite3.IntegrityError:
        message = "Bu yoklama zaten kaydedilmiş görünüyor."
    finally:
        conn.execute("DELETE FROM pending_scans WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()

    return render_template("success.html", message=message)


@app.route("/report")
def report():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT full_name, student_id, timestamp FROM attendance "
        "WHERE class_session_id = ? ORDER BY timestamp DESC",
        (CLASS_SESSION_ID,),
    ).fetchall()
    conn.close()
    return render_template("report.html", rows=rows)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)