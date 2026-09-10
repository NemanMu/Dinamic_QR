import io
import os
import secrets
import psycopg
import time
import uuid
from datetime import datetime
from functools import wraps

import pyotp
import qrcode
from flask import Flask, request, render_template, send_file, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# Oturum çerezlerini imzalamak için gerekli. Üretimde bunu ortam değişkeninden
# (env var) okuyun, yoksa sunucu her yeniden başladığında tüm kullanıcılar
# oturumdan atılır (tekrar giriş yapmaları gerekir).
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

QR_INTERVAL_SECONDS = 10
FORM_SESSION_TIMEOUT_SECONDS = 600

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set.")

# Some providers still expose postgres:// URLs; psycopg expects postgresql://.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def get_db():
    return psycopg.connect(DATABASE_URL)

# Her öğretmenin o anki aktif QR oturumunu tutan bellek-içi sözlük:
# { user_id: {"secret", "totp", "class_session_id"} }
# NOT: Uygulama yeniden başladığında bu bilgi sıfırlanır (aktif oturumlar
# kapanmış sayılır), ama hesaplar ve geçmiş yoklama kayıtları veritabanında
# kalıcı olduğu için kaybolmaz.
active_sessions = {}


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS class_sessions (
                id TEXT PRIMARY KEY,
                teacher_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                ended_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id SERIAL PRIMARY KEY,
                teacher_id INTEGER NOT NULL,
                class_session_id TEXT NOT NULL,
                full_name TEXT NOT NULL,
                student_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                UNIQUE(teacher_id, student_id, class_session_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_scans (
                session_id TEXT PRIMARY KEY,
                teacher_id INTEGER NOT NULL,
                class_session_id TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL
            )
        """)


init_db()


def cleanup_expired_scans(conn):
    cutoff = time.time() - FORM_SESSION_TIMEOUT_SECONDS
    conn.execute("DELETE FROM pending_scans WHERE created_at < %s", (cutoff,))


def format_dt(iso_string):
    if not iso_string:
        return None
    return datetime.fromisoformat(iso_string).strftime("%d.%m.%Y %H:%M")


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, name, email FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "email": row[2]}


def current_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return get_user_by_id(user_id)


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if session.get("user_id") is None:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped


# ----------------------------------------------------------------
# HERKESE AÇIK TANITIM SAYFASI
# ----------------------------------------------------------------
@app.route("/")
def landing():
    return render_template("index.html", user=current_user())


# ----------------------------------------------------------------
# HESAP OLUŞTURMA / GİRİŞ / ÇIKIŞ
# ----------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", error=None)

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not name or not email or not password:
        return render_template("register.html", error="Lütfen tüm alanları doldurun.")

    if len(password) < 6:
        return render_template("register.html", error="Şifre en az 6 karakter olmalı.")

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (name, email, password_hash, created_at) VALUES (%s, %s, %s, %s)",
            (name, email, generate_password_hash(password), datetime.now().isoformat()),
        )
        conn.commit()
        user_id = conn.execute(
            "SELECT id FROM users WHERE email = %s", (email,)
        ).fetchone()[0]
    except psycopg.IntegrityError:
        conn.close()
        return render_template("register.html", error="Bu e-posta zaten kayıtlı.")
    conn.close()

    session["user_id"] = user_id
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None)

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    conn = get_db()
    row = conn.execute(
        "SELECT id, password_hash FROM users WHERE email = %s", (email,)
    ).fetchone()
    conn.close()

    if row is None or not check_password_hash(row[1], password):
        return render_template("login.html", error="E-posta veya şifre hatalı.")

    session["user_id"] = row[0]
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


# ----------------------------------------------------------------
# ÖĞRETMEN PANELİ (giriş gerektirir)
# ----------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    active_info = active_sessions.get(user["id"])

    conn = get_db()
    session_rows = conn.execute(
        "SELECT id, created_at, ended_at FROM class_sessions "
        "WHERE teacher_id = %s ORDER BY created_at DESC",
        (user["id"],),
    ).fetchall()

    past_sessions = []
    for sid, created_at, ended_at in session_rows:
        count = conn.execute(
            "SELECT COUNT(*) FROM attendance WHERE teacher_id = %s AND class_session_id = %s",
            (user["id"], sid),
        ).fetchone()[0]
        is_active = active_info is not None and active_info["class_session_id"] == sid
        past_sessions.append({
            "id": sid,
            "created_at": format_dt(created_at),
            "ended_at": format_dt(ended_at),
            "count": count,
            "active": is_active,
        })

    conn.close()

    return render_template(
        "dashboard.html",
        user=user,
        has_active=active_info is not None,
        sessions=past_sessions,
    )


@app.route("/new-session", methods=["POST"])
@login_required
def new_session():
    user = current_user()

    # Zaten aktif bir oturum varsa, yenisini başlatmadan önce onu kapat.
    old_info = active_sessions.pop(user["id"], None)
    conn = get_db()
    if old_info:
        conn.execute(
            "UPDATE class_sessions SET ended_at = %s WHERE id = %s",
            (datetime.now().isoformat(), old_info["class_session_id"]),
        )

    class_session_id = str(uuid.uuid4())
    secret = pyotp.random_base32()
    active_sessions[user["id"]] = {
        "secret": secret,
        "totp": pyotp.TOTP(secret, interval=QR_INTERVAL_SECONDS, digits=8),
        "class_session_id": class_session_id,
    }

    conn.execute(
        "INSERT INTO class_sessions (id, teacher_id, created_at, ended_at) VALUES (%s, %s, %s, NULL)",
        (class_session_id, user["id"], datetime.now().isoformat()),
    )
    conn.execute("DELETE FROM pending_scans WHERE teacher_id = %s", (user["id"],))
    conn.commit()
    conn.close()

    return redirect(url_for("display"))


@app.route("/end-session", methods=["POST"])
@login_required
def end_session():
    user = current_user()
    info = active_sessions.pop(user["id"], None)

    ended_session_id = ""
    if info:
        ended_session_id = info["class_session_id"]
        conn = get_db()
        conn.execute(
            "UPDATE class_sessions SET ended_at = %s WHERE id = %s",
            (datetime.now().isoformat(), ended_session_id),
        )
        conn.commit()
        conn.close()

    return redirect(url_for("display", ended=ended_session_id))


@app.route("/display")
@login_required
def display():
    user = current_user()
    info = active_sessions.get(user["id"])
    ended_session_id = request.args.get("ended") or None
    return render_template(
        "display.html",
        active=info is not None,
        ended_session_id=ended_session_id,
    )


@app.route("/qr.png")
@login_required
def qr_png():
    user = current_user()
    info = active_sessions.get(user["id"])
    if info is None:
        return "Oturum aktif değil.", 403

    token = info["totp"].now()
    attend_url = f"{request.host_url}attend?token={token}&t={user['id']}"

    img = qrcode.make(attend_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ----------------------------------------------------------------
# ÖĞRENCİNİN TARAYINCA GÖRDÜĞÜ SAYFA (herkese açık)
# ----------------------------------------------------------------
@app.route("/attend", methods=["GET"])
def attend_form():
    token = request.args.get("token", "")
    teacher_id_raw = request.args.get("t", "")

    info = None
    if teacher_id_raw.isdigit():
        info = active_sessions.get(int(teacher_id_raw))

    if info is None:
        return render_template(
            "attend.html",
            error="Şu anda aktif bir yoklama oturumu yok. Öğretmeninize danışın.",
            session_id=None,
        )

    if not info["totp"].verify(token, valid_window=1):
        return render_template(
            "attend.html",
            error="QR kodun süresi doldu, lütfen ekrandaki güncel QR kodu tekrar okutun.",
            session_id=None,
        )

    session_id = secrets.token_urlsafe(24)
    conn = get_db()
    cleanup_expired_scans(conn)
    conn.execute(
        "INSERT INTO pending_scans (session_id, teacher_id, class_session_id, created_at) VALUES (%s, %s, %s, %s)",
        (session_id, int(teacher_id_raw), info["class_session_id"], time.time()),
    )
    conn.commit()
    conn.close()

    return render_template("attend.html", error=None, session_id=session_id)


@app.route("/attend", methods=["POST"])
def attend_submit():
    session_id = request.form.get("session_id", "")
    full_name = request.form.get("full_name", "").strip()
    student_id = request.form.get("student_id", "").strip()

    conn = get_db()
    cleanup_expired_scans(conn)

    row = conn.execute(
        "SELECT teacher_id, class_session_id FROM pending_scans WHERE session_id = %s",
        (session_id,),
    ).fetchone()

    if row is None:
        conn.close()
        return render_template(
            "attend.html",
            error="Oturumun süresi doldu ya da geçersiz. Lütfen ekrandaki QR kodu tekrar okutun.",
            session_id=None,
        )

    teacher_id, class_session_id = row

    if not full_name or not student_id:
        conn.close()
        return render_template(
            "attend.html",
            error="Lütfen ad soyad ve öğrenci numarasını eksiksiz girin.",
            session_id=session_id,
        )

    try:
        conn.execute(
            "INSERT INTO attendance (teacher_id, class_session_id, full_name, student_id, timestamp) VALUES (%s, %s, %s, %s, %s)",
            (teacher_id, class_session_id, full_name, student_id, datetime.now().isoformat()),
        )
        conn.commit()
        message = f"{full_name} ({student_id}) için yoklama kaydedildi."
    except psycopg.IntegrityError:
        message = "Bu yoklama zaten kaydedilmiş görünüyor."
    finally:
        conn.execute("DELETE FROM pending_scans WHERE session_id = %s", (session_id,))
        conn.commit()
        conn.close()

    return render_template("success.html", message=message)


# ----------------------------------------------------------------
# TEK BİR OTURUMUN YOKLAMA LİSTESİ (giriş gerektirir, sadece sahibi görebilir)
# ----------------------------------------------------------------
@app.route("/report/<session_id>")
@login_required
def report_detail(session_id):
    user = current_user()

    conn = get_db()
    owner_row = conn.execute(
        "SELECT teacher_id, created_at FROM class_sessions WHERE id = %s", (session_id,)
    ).fetchone()

    if owner_row is None or owner_row[0] != user["id"]:
        conn.close()
        return "Bu oturum bulunamadı.", 404

    rows = conn.execute(
        "SELECT full_name, student_id, timestamp FROM attendance "
        "WHERE teacher_id = %s AND class_session_id = %s ORDER BY timestamp",
        (user["id"], session_id),
    ).fetchall()
    conn.close()

    return render_template(
        "report.html", rows=rows, created_at=format_dt(owner_row[1])
    )


if __name__ == "__main__":
    # debug=True sadece geliştirme aşamasında kullanılmalı.
    app.run(debug=True, host="0.0.0.0", port=5000)
