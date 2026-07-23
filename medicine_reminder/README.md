# Medicine Reminder

A Flask + SQLite web app that lets users register medicines, set how often to take
them, and receive **SMS reminders via Twilio** at the scheduled times. Each medicine
has its own **SMS toggle** â€” if it's ON the reminder is texted, if OFF the reminder is
skipped.

---

## 1. Tech stack

| Layer        | Technology                          |
|--------------|-------------------------------------|
| Backend      | Python 3, **Flask**                 |
| Database     | **SQLite** (file-based, no server)  |
| Frontend     | **HTML** (Jinja2 templates) + **CSS** |
| SMS API      | **Twilio** REST API                 |
| Scheduler    | APScheduler (background reminders)  |
| Auth         | Werkzeug password hashing + sessions |

---

## 2. Project structure

```
medicine_reminder/
â”œâ”€ app.py                 # ENTIRE backend in one file (routes, DB, CRUD, scheduler, SMS)
â”œâ”€ config.py              # ONE config file (Flask secret, DB name, Twilio creds)
â”œâ”€ requirements.txt       # Python dependencies
â”œâ”€ README.md              # This file
â”œâ”€ static/
â”‚   â””â”€ style.css          # All styling
â””â”€ templates/
    â”œâ”€ base.html          # Shared layout + nav + flash messages
    â”œâ”€ login.html         # Login screen
    â”œâ”€ register.html      # Register screen
    â”œâ”€ profile.html       # Profile (view/edit, phone + SMS preference)
    â”œâ”€ dashboard.html     # Lists medicines, SMS toggle, edit/delete/test
    â”œâ”€ medicine_form.html # Add / edit medicine + frequency + reminder times
    â”œâ”€ users.html         # Admin: list all users
    â””â”€ user_form.html     # Admin: add / edit user
```

The SQLite database (`medicine_reminder.db`) is created automatically on first run.

---

## 3. Features / Screens (as requested)

- **Login** â€“ `/login`
- **Register** â€“ `/register` (self sign-up, standard user)
- **Profile** â€“ `/profile` (edit name, email, phone, password, account-level SMS)
- **Add Medicine** â€“ `/medicine/add`
- **Frequency of medicine** â€“ chosen on the medicine form (Once/Twice/Three/Four times
  daily, Every other day, Weekly, Custom) **plus** one or more exact reminder times (HH:MM).
- **Reminder over message** â€“ background scheduler sends a Twilio SMS at each time.
- **SMS toggle** â€“ every medicine card has an ON/OFF switch. ON â†’ SMS sent; OFF â†’ skipped.
- **Full CRUD**
  - Medicines: create, read (dashboard), update (edit), delete.
  - Users: admin can create, read (list), update, delete users.

### Seeded data (ready to use)
- **Test user** â†’ username `admin`, password `admin123` (admin role).
- **Sample medicine** â†’ *Vitamin D*, dosage *1 tablet (1000 IU)*, frequency *Twice daily*,
  times *09:00* and *21:00*, SMS toggle ON.

---

## 4. Setup & run

```bash
# 1. (optional) create a virtual environment
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 2. install dependencies
pip install -r requirements.txt

# 3. run
python app.py
```

Then open **http://localhost:5000** and log in with `admin` / `admin123`.

---

## 5. Twilio configuration

The **Account SID** from your console is already filled in
(`AC09db59d82fc81e65d7bc89f44b1c2552`). You still need to supply the **Auth Token**
(masked in the console) and **one sender**. Your console used the **Messaging Service**
sender type, so that is preferred:

```python
TWILIO_AUTH_TOKEN            = "your_real_auth_token"
TWILIO_MESSAGING_SERVICE_SID = "MGxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # preferred
# or, if not using a Messaging Service:
TWILIO_FROM_NUMBER           = "+1XXXXXXXXXX"
```

Recommended via environment variables:

```bash
export TWILIO_AUTH_TOKEN="..."
export TWILIO_MESSAGING_SERVICE_SID="MG..."
```

**Recipient number is not hard-coded.** Each reminder is sent to the phone number on
the **registered user** who owns the medicine. The seeded `admin` user is pre-filled
with your verified test number **+919866212932**, so logging in as admin and letting the
sample medicine fire (or pressing "Test SMS") will text that number once the auth token
is set.

If credentials are incomplete the app runs in **dry-run** mode â€” the message is printed
to the console instead of sent â€” so you can demo without sending real texts.

### SMS template (2 lines, includes medicine name)
Defined in `config.py` as `SMS_TEMPLATE`:

```
Medicine Reminder
It's time to take your {name} ({dosage}). Stay healthy!
```

`{name}` and `{dosage}` are filled from the medicine record.

---

## 6. How reminders work (once-only)

1. A background scheduler runs every `SCHEDULER_INTERVAL_SECONDS` (default 30s).
2. On each tick it compares the current `HH:MM` to every medicine's reminder times.
3. On a match it **claims the slot** by inserting into `sent_reminders` with a UNIQUE
   `(medicine_id, scheduled_time, date)` constraint:
   - first time that day at that time â†’ claim succeeds â†’ **send one SMS** (if the
     medicine's SMS toggle is ON);
   - already claimed â†’ **skip** (no duplicate texts, no matter how often it ticks).
4. The next SMS goes out at the **next scheduled time**, the **next day**, or **after you
   update the frequency/time** (a new time is an unclaimed slot, so it fires again).
5. Every fire is recorded in `reminder_log`.

The dashboard's **"Test SMS"** button still sends on demand (bypasses the once-only lock)
so you can verify your Twilio setup immediately.

---

## 7. Database schema

**users**: `id, username, password_hash, full_name, email, phone, sms_enabled,
is_admin, created_at`

**medicines**: `id, user_id (FK), name, dosage, frequency, times (CSV of HH:MM),
start_date, end_date, notes, sms_enabled, created_at`

**reminder_log**: `id, medicine_id, fired_at, channel ('sms'|'skipped'), detail`

**sent_reminders**: `id, medicine_id, scheduled_time, sent_date, sent_at` with
`UNIQUE(medicine_id, scheduled_time, sent_date)` â€” the lock that enforces one
SMS per scheduled time per day.

---

## 8. Route reference

| Method | Path                          | Purpose                         |
|--------|-------------------------------|---------------------------------|
| GET/POST | `/login`                    | Login                           |
| GET/POST | `/register`                 | Register                        |
| GET    | `/logout`                     | Logout                          |
| GET    | `/dashboard`                  | List my medicines               |
| GET/POST | `/profile`                  | View / edit profile             |
| GET/POST | `/medicine/add`             | Create medicine                 |
| GET/POST | `/medicine/<id>/edit`       | Update medicine                 |
| POST   | `/medicine/<id>/delete`       | Delete medicine                 |
| POST   | `/medicine/<id>/toggle-sms`   | Toggle SMS (AJAX)               |
| POST   | `/medicine/<id>/test-sms`     | Send a test reminder now        |
| GET    | `/users`                      | Admin: list users               |
| GET/POST | `/users/add`                | Admin: create user              |
| GET/POST | `/users/<id>/edit`          | Admin: update user              |
| POST   | `/users/<id>/delete`          | Admin: delete user              |

---

## 9. Security notes

- Passwords are stored hashed (never in plain text).
- Change `SECRET_KEY` before any real deployment.
- Keep Twilio credentials in environment variables, not in source control.
- For production use a WSGI server (gunicorn/uWSGI) behind HTTPS rather than the
  built-in dev server.
