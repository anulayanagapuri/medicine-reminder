"""
Medicine Reminder â€” single-file Flask application.

Contains:
  - SQLite setup + schema (users, medicines)
  - Auth (register / login / logout) with hashed passwords
  - Profile management (view + edit, incl. phone & SMS preference)
  - Full CRUD for users (admin only) and medicines
  - Per-medicine SMS toggle
  - Background scheduler that fires reminders at scheduled times
  - Twilio SMS integration (gracefully no-ops if not configured)

Run:  python app.py
"""

import os
import sqlite3
from datetime import datetime, date

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, g, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

from config import Config

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, Config.DB_NAME)

app = Flask(__name__)
app.config.from_object(Config)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _connect():
    """Standalone connection (used by scheduler outside request context)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create tables and seed the test admin user + sample medicine."""
    conn = _connect()
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name     TEXT,
            email         TEXT,
            phone         TEXT,
            sms_enabled   INTEGER NOT NULL DEFAULT 0,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS medicines (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            dosage      TEXT,
            frequency   TEXT NOT NULL,        -- human label, e.g. "Twice daily"
            times       TEXT NOT NULL,        -- comma separated HH:MM list
            start_date  TEXT,
            end_date    TEXT,
            notes       TEXT,
            sms_enabled INTEGER NOT NULL DEFAULT 0,  -- per-medicine SMS toggle
            created_at  TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reminder_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            medicine_id INTEGER NOT NULL,
            fired_at    TEXT NOT NULL,
            channel     TEXT NOT NULL,   -- 'sms' or 'skipped'
            detail      TEXT
        );

        -- Guarantees a reminder fires only ONCE per medicine per scheduled
        -- time per day. The UNIQUE constraint is the de-duplication lock:
        -- the next send happens at the next scheduled time, the next day,
        -- or after the frequency/time is updated (a new time => new row).
        CREATE TABLE IF NOT EXISTS sent_reminders (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            medicine_id    INTEGER NOT NULL,
            scheduled_time TEXT NOT NULL,   -- HH:MM
            sent_date      TEXT NOT NULL,   -- YYYY-MM-DD
            sent_at        TEXT NOT NULL,
            UNIQUE(medicine_id, scheduled_time, sent_date)
        );
        """
    )

    # Seed admin user (admin / admin123) if not present
    cur.execute("SELECT id FROM users WHERE username = ?", ("admin",))
    admin = cur.fetchone()
    if admin is None:
        cur.execute(
            """INSERT INTO users
               (username, password_hash, full_name, email, phone, sms_enabled, is_admin, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "admin",
                generate_password_hash("admin123"),
                "Administrator",
                "admin@example.com",
                "+919866212932",
                1,
                1,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        admin_id = cur.lastrowid

        # Seed one sample medicine with a frequency
        cur.execute(
            """INSERT INTO medicines
               (user_id, name, dosage, frequency, times, start_date, end_date, notes, sms_enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                admin_id,
                "Vitamin D",
                "1 tablet (1000 IU)",
                "Twice daily",
                "09:00,21:00",
                date.today().isoformat(),
                "",
                "Take with food.",
                1,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Twilio SMS
# ---------------------------------------------------------------------------
def send_sms(to_number, body):
    """
    Send an SMS via Twilio. Returns (ok: bool, detail: str).
    If Twilio is not configured or the library is missing, it logs and skips
    gracefully so the rest of the app keeps working.
    """
    if not to_number:
        return False, "no phone number on file"

    if not Config.twilio_is_configured():
        msg = f"[SMS skipped â€“ Twilio not configured] to={to_number} :: {body}"
        print(msg)
        return False, "twilio not configured"

    try:
        from twilio.rest import Client
    except ImportError:
        print("[SMS skipped] twilio package not installed (pip install twilio)")
        return False, "twilio package not installed"

    try:
        client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
        kwargs = {"body": body, "to": to_number}
        # Prefer Messaging Service (the sender type used in your console),
        # otherwise fall back to a Twilio "From" number.
        if Config.TWILIO_MESSAGING_SERVICE_SID:
            kwargs["messaging_service_sid"] = Config.TWILIO_MESSAGING_SERVICE_SID
        else:
            kwargs["from_"] = Config.TWILIO_FROM_NUMBER
        message = client.messages.create(**kwargs)
        print(f"[SMS sent] sid={message.sid} to={to_number}")
        return True, f"sent sid={message.sid}"
    except Exception as exc:  # noqa: BLE001
        print(f"[SMS error] {exc}")
        return False, f"error: {exc}"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if not user["is_admin"]:
            flash("Admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


# ---------------------------------------------------------------------------
# Routes â€” Auth
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        sms_enabled = 1 if request.form.get("sms_enabled") == "on" else 0

        if not username or not password:
            flash("Username and password are required.", "danger")
            return render_template("register.html", form=request.form)

        db = get_db()
        exists = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            flash("That username is already taken.", "danger")
            return render_template("register.html", form=request.form)

        db.execute(
            """INSERT INTO users
               (username, password_hash, full_name, email, phone, sms_enabled, is_admin, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                username,
                generate_password_hash(password),
                full_name,
                email,
                phone,
                sms_enabled,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['full_name'] or user['username']}!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes â€” Dashboard & Profile
# ---------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user = current_user()
    medicines = db.execute(
        "SELECT * FROM medicines WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()
    return render_template("dashboard.html", medicines=medicines)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    db = get_db()
    user = current_user()

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        sms_enabled = 1 if request.form.get("sms_enabled") == "on" else 0
        new_password = request.form.get("password", "")

        if new_password:
            db.execute(
                "UPDATE users SET full_name=?, email=?, phone=?, sms_enabled=?, password_hash=? WHERE id=?",
                (full_name, email, phone, sms_enabled, generate_password_hash(new_password), user["id"]),
            )
        else:
            db.execute(
                "UPDATE users SET full_name=?, email=?, phone=?, sms_enabled=? WHERE id=?",
                (full_name, email, phone, sms_enabled, user["id"]),
            )
        db.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html", user=user)


# ---------------------------------------------------------------------------
# Routes â€” Medicine CRUD
# ---------------------------------------------------------------------------
FREQUENCY_PRESETS = {
    "Once daily": 1,
    "Twice daily": 2,
    "Three times daily": 3,
    "Four times daily": 4,
    "Every other day": 1,
    "Weekly": 1,
    "Custom": 0,
}


@app.route("/medicine/add", methods=["GET", "POST"])
@login_required
def add_medicine():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        dosage = request.form.get("dosage", "").strip()
        frequency = request.form.get("frequency", "").strip()
        times = ",".join(t.strip() for t in request.form.getlist("times") if t.strip())
        if not times:
            times = request.form.get("times_text", "").strip()
        start_date = request.form.get("start_date", "").strip()
        end_date = request.form.get("end_date", "").strip()
        notes = request.form.get("notes", "").strip()
        sms_enabled = 1 if request.form.get("sms_enabled") == "on" else 0

        if not name or not frequency or not times:
            flash("Name, frequency and at least one time are required.", "danger")
            return render_template(
                "medicine_form.html", form=request.form, frequencies=FREQUENCY_PRESETS, action="add"
            )

        db = get_db()
        db.execute(
            """INSERT INTO medicines
               (user_id, name, dosage, frequency, times, start_date, end_date, notes, sms_enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user["id"], name, dosage, frequency, times, start_date, end_date, notes,
                sms_enabled, datetime.now().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        flash(f"Medicine '{name}' added.", "success")
        return redirect(url_for("dashboard"))

    return render_template("medicine_form.html", form={}, frequencies=FREQUENCY_PRESETS, action="add")


@app.route("/medicine/<int:med_id>/edit", methods=["GET", "POST"])
@login_required
def edit_medicine(med_id):
    db = get_db()
    user = current_user()
    med = db.execute("SELECT * FROM medicines WHERE id = ?", (med_id,)).fetchone()
    if not med or (med["user_id"] != user["id"] and not user["is_admin"]):
        flash("Medicine not found.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        dosage = request.form.get("dosage", "").strip()
        frequency = request.form.get("frequency", "").strip()
        times = ",".join(t.strip() for t in request.form.getlist("times") if t.strip())
        if not times:
            times = request.form.get("times_text", "").strip()
        start_date = request.form.get("start_date", "").strip()
        end_date = request.form.get("end_date", "").strip()
        notes = request.form.get("notes", "").strip()
        sms_enabled = 1 if request.form.get("sms_enabled") == "on" else 0

        db.execute(
            """UPDATE medicines
               SET name=?, dosage=?, frequency=?, times=?, start_date=?, end_date=?, notes=?, sms_enabled=?
               WHERE id=?""",
            (name, dosage, frequency, times, start_date, end_date, notes, sms_enabled, med_id),
        )
        db.commit()
        flash(f"Medicine '{name}' updated.", "success")
        return redirect(url_for("dashboard"))

    return render_template(
        "medicine_form.html", form=med, frequencies=FREQUENCY_PRESETS, action="edit", med_id=med_id
    )


@app.route("/medicine/<int:med_id>/delete", methods=["POST"])
@login_required
def delete_medicine(med_id):
    db = get_db()
    user = current_user()
    med = db.execute("SELECT * FROM medicines WHERE id = ?", (med_id,)).fetchone()
    if med and (med["user_id"] == user["id"] or user["is_admin"]):
        db.execute("DELETE FROM medicines WHERE id = ?", (med_id,))
        db.commit()
        flash("Medicine deleted.", "info")
    else:
        flash("Medicine not found.", "danger")
    return redirect(url_for("dashboard"))


@app.route("/medicine/<int:med_id>/toggle-sms", methods=["POST"])
@login_required
def toggle_sms(med_id):
    """AJAX endpoint for the SMS toggle button on each medicine."""
    db = get_db()
    user = current_user()
    med = db.execute("SELECT * FROM medicines WHERE id = ?", (med_id,)).fetchone()
    if not med or (med["user_id"] != user["id"] and not user["is_admin"]):
        return jsonify({"ok": False, "error": "not found"}), 404
    new_val = 0 if med["sms_enabled"] else 1
    db.execute("UPDATE medicines SET sms_enabled=? WHERE id=?", (new_val, med_id))
    db.commit()
    return jsonify({"ok": True, "sms_enabled": bool(new_val)})


@app.route("/medicine/<int:med_id>/test-sms", methods=["POST"])
@login_required
def test_sms(med_id):
    """Manually trigger a reminder SMS for a medicine (useful for demos)."""
    db = get_db()
    user = current_user()
    med = db.execute("SELECT * FROM medicines WHERE id = ?", (med_id,)).fetchone()
    if not med or (med["user_id"] != user["id"] and not user["is_admin"]):
        flash("Medicine not found.", "danger")
        return redirect(url_for("dashboard"))

    if not med["sms_enabled"]:
        flash("SMS is toggled OFF for this medicine.", "warning")
        return redirect(url_for("dashboard"))

    body = Config.build_sms_body(med["name"], med["dosage"])
    ok, detail = send_sms(user["phone"], body)
    flash(f"Test SMS: {detail}", "success" if ok else "warning")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Routes â€” User management (admin) â€” full CRUD
# ---------------------------------------------------------------------------
@app.route("/users")
@admin_required
def users_list():
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return render_template("users.html", users=users)


@app.route("/users/add", methods=["GET", "POST"])
@admin_required
def add_user():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        sms_enabled = 1 if request.form.get("sms_enabled") == "on" else 0
        is_admin = 1 if request.form.get("is_admin") == "on" else 0

        if not username or not password:
            flash("Username and password required.", "danger")
            return render_template("user_form.html", form=request.form, action="add")

        db = get_db()
        if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            flash("Username already taken.", "danger")
            return render_template("user_form.html", form=request.form, action="add")

        db.execute(
            """INSERT INTO users
               (username, password_hash, full_name, email, phone, sms_enabled, is_admin, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                username, generate_password_hash(password), full_name, email, phone,
                sms_enabled, is_admin, datetime.now().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        flash("User created.", "success")
        return redirect(url_for("users_list"))

    return render_template("user_form.html", form={}, action="add")


@app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("users_list"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        sms_enabled = 1 if request.form.get("sms_enabled") == "on" else 0
        is_admin = 1 if request.form.get("is_admin") == "on" else 0
        new_password = request.form.get("password", "")

        if new_password:
            db.execute(
                "UPDATE users SET full_name=?, email=?, phone=?, sms_enabled=?, is_admin=?, password_hash=? WHERE id=?",
                (full_name, email, phone, sms_enabled, is_admin, generate_password_hash(new_password), user_id),
            )
        else:
            db.execute(
                "UPDATE users SET full_name=?, email=?, phone=?, sms_enabled=?, is_admin=? WHERE id=?",
                (full_name, email, phone, sms_enabled, is_admin, user_id),
            )
        db.commit()
        flash("User updated.", "success")
        return redirect(url_for("users_list"))

    return render_template("user_form.html", form=user, action="edit", user_id=user_id)


@app.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    db = get_db()
    me = current_user()
    if user_id == me["id"]:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("users_list"))
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    flash("User deleted.", "info")
    return redirect(url_for("users_list"))


# ---------------------------------------------------------------------------
# Reminder scheduler
# ---------------------------------------------------------------------------
def check_and_send_reminders():
    """
    Runs in the background. Finds medicines whose scheduled time matches the
    current HH:MM and, when their SMS toggle is ON, sends ONE SMS to the owner.

    Once-only guarantee: before sending we try to claim the slot by inserting
    a row into sent_reminders with UNIQUE(medicine_id, scheduled_time, date).
    If the row already exists, the reminder was already sent today at that time
    and we skip. The next SMS therefore goes out at the next scheduled time,
    the next day, or after the frequency/time is changed (a new time creates a
    new, unclaimed slot).
    """
    import sqlite3 as _sqlite3

    now = datetime.now()
    now_hhmm = now.strftime("%H:%M")
    today = now.date().isoformat()

    conn = _connect()
    cur = conn.cursor()
    rows = cur.execute(
        """SELECT m.*, u.phone AS user_phone, u.sms_enabled AS user_sms, u.full_name
           FROM medicines m JOIN users u ON u.id = m.user_id"""
    ).fetchall()

    for m in rows:
        times = [t.strip() for t in (m["times"] or "").split(",") if t.strip()]
        if now_hhmm not in times:
            continue

        # date window check
        if m["start_date"] and today < m["start_date"]:
            continue
        if m["end_date"] and today > m["end_date"]:
            continue

        # ---- Claim this slot exactly once (de-duplication lock) ----
        try:
            cur.execute(
                """INSERT INTO sent_reminders (medicine_id, scheduled_time, sent_date, sent_at)
                   VALUES (?, ?, ?, ?)""",
                (m["id"], now_hhmm, today, now.isoformat(timespec="seconds")),
            )
        except _sqlite3.IntegrityError:
            # Already fired for this medicine at this time today -> skip.
            continue

        # Recipient = the registered user's phone number on file.
        body = Config.build_sms_body(m["name"], m["dosage"])

        if m["sms_enabled"]:
            ok, detail = send_sms(m["user_phone"], body)
            channel = "sms" if ok else "skipped"
        else:
            detail = "medicine SMS toggle OFF"
            channel = "skipped"
            print(f"[Reminder] {m['name']} due but SMS toggle OFF â€” skipped")

        cur.execute(
            "INSERT INTO reminder_log (medicine_id, fired_at, channel, detail) VALUES (?, ?, ?, ?)",
            (m["id"], now.isoformat(timespec="seconds"), channel, detail),
        )
        conn.commit()

    conn.commit()
    conn.close()


def start_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        check_and_send_reminders,
        "interval",
        seconds=Config.SCHEDULER_INTERVAL_SECONDS,
        id="medicine_reminders",
        replace_existing=True,
    )
    scheduler.start()
    print(f"[Scheduler] started â€” checking every {Config.SCHEDULER_INTERVAL_SECONDS}s")
    return scheduler


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
init_db()

if __name__ == "__main__":
    # Avoid double-starting the scheduler under the Flask reloader.
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=True)
