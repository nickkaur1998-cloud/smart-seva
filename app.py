import os
import secrets
import json
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import wraps
from io import BytesIO
from html import escape

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    session,
    send_file,
    abort,
    jsonify,
    render_template_string,
)

from psycopg import connect, IntegrityError
from psycopg.rows import dict_row

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


def load_local_env():
    """Load simple KEY=VALUE entries from .env without extra packages."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
    except OSError:
        pass


load_local_env()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_ANON_KEY",
    os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
).strip()

# The Flask server talks directly to Supabase Postgres.
# Keep this value ONLY in the server's .env / deployment environment.
SUPABASE_DB_URL = os.environ.get("SUPABASE_DB_URL", "").strip()

for suffix in ("/rest/v1", "/auth/v1"):
    if SUPABASE_URL.endswith(suffix):
        SUPABASE_URL = SUPABASE_URL[:-len(suffix)]
        break


def supabase_auth_request(endpoint, payload):
    """Call Supabase Auth using the public Supabase API key."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "Supabase Auth is not configured. Add SUPABASE_URL and "
            "SUPABASE_ANON_KEY to your environment or .env file."
        )

    req = Request(
        f"{SUPABASE_URL}/auth/v1/{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=15) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except HTTPError as error:
        try:
            body = error.read().decode("utf-8")
            details = json.loads(body) if body else {}
        except Exception:
            details = {}
        message = (
            details.get("msg")
            or details.get("message")
            or details.get("error_description")
            or "Supabase authentication request failed."
        )
        raise ValueError(message) from error
    except URLError as error:
        raise RuntimeError(
            "Could not reach Supabase. Check your internet connection and Supabase URL."
        ) from error


class PostgresDB:
    """Small compatibility wrapper for the existing Flask SQL code.

    The app historically used SQLite's '?' placeholders. PostgreSQL/psycopg
    uses '%s', so this wrapper translates the placeholders while keeping the
    rest of the route code readable.
    """

    def __init__(self):
        if not SUPABASE_DB_URL:
            raise RuntimeError(
                "Supabase database is not configured. Add SUPABASE_DB_URL "
                "to your .env file using the Supabase PostgreSQL connection string."
            )
        self.conn = connect(
            SUPABASE_DB_URL,
            row_factory=dict_row,
            connect_timeout=15,
            prepare_threshold=None,
        )

    @staticmethod
    def _convert_placeholders(sql):
        # The application's SQL uses '?' only as parameter placeholders.
        # Avoid changing '?' inside quoted SQL strings.
        result = []
        in_single = False
        in_double = False
        i = 0
        while i < len(sql):
            ch = sql[i]
            if ch == "'" and not in_double:
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    result.extend([ch, ch])
                    i += 2
                    continue
                in_single = not in_single
                result.append(ch)
            elif ch == '"' and not in_single:
                if i + 1 < len(sql) and sql[i + 1] == '"':
                    result.extend([ch, ch])
                    i += 2
                    continue
                in_double = not in_double
                result.append(ch)
            elif ch == "?" and not in_single and not in_double:
                result.append("%s")
            else:
                result.append(ch)
            i += 1
        return "".join(result)

    def execute(self, sql, params=()):
        return self.conn.execute(self._convert_placeholders(sql), params)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def get_db():
    return PostgresDB()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def as_datetime(value):
    """Accept either a PostgreSQL datetime or an ISO timestamp string."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def init_db():
    """Verify the Supabase schema and optionally promote the configured admin.

    Tables are managed in Supabase SQL, not created by Flask.
    """
    db = get_db()
    try:
        db.execute("SELECT 1")

        # Safe migrations for features added after the original deployment.
        db.execute("ALTER TABLE seva ADD COLUMN IF NOT EXISTS max_volunteers INTEGER NOT NULL DEFAULT 0")
        db.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id BIGSERIAL PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                link TEXT,
                is_read BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS notifications_user_idx ON notifications(user_id, is_read, created_at DESC)")
        db.commit()

        admin_email = os.environ.get("SMART_SEVA_ADMIN_EMAIL", "").strip().lower()
        if admin_email:
            db.execute(
                "UPDATE public.users SET role = 'admin' WHERE lower(email) = lower(?)",
                (admin_email,)
            )
            db.commit()
    finally:
        db.close()


# ============================================================
# APP CONFIGURATION
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "smart-seva-development-secret-key-change-this"
)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

# Seva schedules are entered as local community times. California is the
# default, but this can be changed with APP_TIMEZONE in Render/.env.
APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/Los_Angeles"))


# Verify the cloud database exists before the application starts serving requests.
init_db()


# ============================================================
# HELPERS
# ============================================================

def clean(value, max_length=500):

    if value is None:
        return ""

    return str(value).strip()[:max_length]


def h(value):
    return escape(str(value or ""))


# ============================================================
# QUANTITY / UNIT HELPERS
# ============================================================

LB_PER_KG = 2.20462262185


def canonical_unit(unit):
    value = clean(unit, 30).lower()
    aliases = {
        "lbs": "lb",
        "pound": "lb",
        "pounds": "lb",
        "kilogram": "kg",
        "kilograms": "kg",
        "kgs": "kg",
    }
    return aliases.get(value, value)


def is_mass_unit(unit):
    return canonical_unit(unit) in {"kg", "lb"}


def convert_mass(value, from_unit, to_unit):
    source = canonical_unit(from_unit)
    target = canonical_unit(to_unit)
    if source == target:
        return float(value)
    if source == "kg" and target == "lb":
        return float(value) * LB_PER_KG
    if source == "lb" and target == "kg":
        return float(value) / LB_PER_KG
    return None


def convert_quantity(value, from_unit, to_unit):
    source = canonical_unit(from_unit)
    target = canonical_unit(to_unit)
    if source == target:
        return float(value)
    if is_mass_unit(source) and is_mass_unit(target):
        return convert_mass(value, source, target)
    return None


def format_quantity(value):
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


# ============================================================
# CSRF
# ============================================================

def csrf_token():

    token = session.get("csrf_token")

    if not token:

        token = secrets.token_urlsafe(32)

        session["csrf_token"] = token

    return token


def validate_csrf():

    submitted = request.form.get(
        "csrf_token",
        ""
    )

    stored = session.get(
        "csrf_token",
        ""
    )

    if (
        not submitted
        or not stored
        or not secrets.compare_digest(
            submitted,
            stored
        )
    ):
        abort(400, "Invalid security token.")


# ============================================================
# AUTH
# ============================================================

def current_user():

    user_id = session.get("user_id")

    if not user_id:
        return None

    db = get_db()

    user = db.execute(
        """
        SELECT *
        FROM users
        WHERE id = ?
        """,
        (user_id,)
    ).fetchone()

    db.close()

    return user


def login_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        if not session.get("user_id"):

            return redirect(
                url_for(
                    "login",
                    next=request.path
                )
            )
        return function(*args, **kwargs)

    return wrapper


def admin_required(function):

    @wraps(function)
    def wrapper(*args, **kwargs):

        user = current_user()

        if not user or user["role"] != "admin":

            return redirect(
                url_for("login")
            )

        return function(*args, **kwargs)

    return wrapper


def notify_user(user_id, title, message, link=None):
    """Create an in-app notification without affecting the main action."""
    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO notifications (user_id, title, message, link)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, title, message, link)
        )
        db.commit()
    finally:
        db.close()


def seva_end_passed(date_value, end_time_value):
    """Return True once the scheduled Seva end time has arrived."""
    try:
        date_text = str(date_value)
        time_text = str(end_time_value)[:5]
        scheduled = datetime.strptime(
            f"{date_text} {time_text}",
            "%Y-%m-%d %H:%M"
        ).replace(tzinfo=APP_TIMEZONE)
        return datetime.now(APP_TIMEZONE) >= scheduled
    except (TypeError, ValueError):
        return False


def format_time_label(value):
    try:
        return datetime.strptime(str(value)[:5], "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return str(value)


def milestone_badges(total_hours, completed_count):
    badges = []
    if completed_count >= 1:
        badges.append("🌱 First Seva")
    if total_hours >= 5:
        badges.append("⭐ 5 Hours")
    if total_hours >= 10:
        badges.append("⭐ 10 Hours")
    if total_hours >= 25:
        badges.append("🏅 25 Hours")
    if total_hours >= 50:
        badges.append("🏆 50 Hours")
    if total_hours >= 100:
        badges.append("🌟 100 Hours")
    return badges


# ============================================================
# STYLES
# ============================================================

STYLE = """
<style>

:root {
    --ink: #04070f;
    --deep: #0b1020;
    --midnight: #111a31;
    --gold: #d7a83e;
    --sun: #ffe7a1;
    --line: rgba(215,168,62,.22);
}

* {
    box-sizing: border-box;
}

html {
    min-height: 100%;
    background: var(--ink);
}

body {
    margin: 0;

    font-family:
        "Trebuchet MS",
        "Segoe UI",
        sans-serif;

    color: white;

    background: #030a1d;

    min-height: 100vh;
    overflow-x: hidden;
    position: relative;
}

.night-sky {
    position: fixed;
    inset: 0;
    z-index: 0;
    pointer-events: none;
    overflow: hidden;
    background: #030a1d;
}

.night-sky::before,
.night-sky::after {
    content: "";
    position: absolute;
    inset: 0;
    opacity: .82;
    background-image:
        radial-gradient(circle, #ffffff 0 1.2px, transparent 1.8px),
        radial-gradient(circle, #ffe7a1 0 1.1px, transparent 1.7px),
        radial-gradient(circle, rgba(184,216,255,.9) 0 1px, transparent 1.6px);
    background-size: 137px 151px, 211px 193px, 97px 113px;
    background-position: 7px 19px, 43px 71px, 29px 3px;
    animation: star-flicker 5s steps(2, end) infinite alternate;
}

.night-sky::after {
    opacity: .45;
    transform: none;
    animation: star-flicker 7s steps(2, end) infinite alternate-reverse;
}

@keyframes star-flicker {
    from { opacity: .32; }
    to { opacity: 1; }
}

.ik-constellation {
    position: absolute;
    color: rgba(255, 231, 161, .12);
    font-family: "Noto Sans Gurmukhi", "Segoe UI Symbol", serif;
    font-size: clamp(70px, 10vw, 160px);
    line-height: 1;
    text-shadow: 0 0 14px rgba(215,168,62,.16);
    animation: ik-drift 16s ease-in-out infinite alternate;
}

.ik-one { top: 13%; left: 7%; animation-delay: -3s; }
.ik-two { top: 38%; right: 8%; font-size: clamp(50px, 7vw, 110px); animation-delay: -10s; }
.ik-three { bottom: 8%; left: 40%; font-size: clamp(45px, 6vw, 95px); animation-delay: -7s; }

@keyframes ik-drift {
    from { transform: translate3d(-12px, 8px, 0) rotate(-7deg); opacity: .1; }
    to { transform: translate3d(20px, -18px, 0) rotate(8deg); opacity: .34; }
}

nav {
    min-height: 70px;

    padding: 0 6%;

    display: flex;

    justify-content: space-between;

    align-items: center;

    background: rgba(3,6,14,.78);
    backdrop-filter: blur(18px);

    border-bottom:
        1px solid
        var(--line);

    position: sticky;

    top: 0;

    z-index: 100;

    box-shadow: 0 12px 40px rgba(0,0,0,.18);
    animation: nav-glow 8s ease-in-out infinite alternate;
}

@keyframes nav-glow {
    from { box-shadow: 0 12px 40px rgba(0,0,0,.18); }
    to { box-shadow: 0 12px 44px rgba(215,168,62,.12); }
}

.logo {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 18px;
    font-weight: bold;
    letter-spacing: .08em;
    text-transform: uppercase;
}

.logo-mark {
    color: var(--gold);
    font-family: "Noto Sans Gurmukhi", "Segoe UI Symbol", serif;
    font-size: 31px;
    line-height: 1;
    text-shadow: 0 0 22px rgba(215,168,62,.5);
    animation: emblem-breathe 5s ease-in-out infinite;
}

@keyframes emblem-breathe {
    0%, 100% { transform: translateY(0) rotate(-2deg); opacity: .86; }
    50% { transform: translateY(-2px) rotate(2deg); opacity: 1; }
}

.gold {
    color: var(--sun);
    text-shadow: 0 0 30px rgba(215,168,62,.26);
}

nav a {
    color: #dce8ef;

    text-decoration: none;

    margin-left: 18px;
}

nav a:hover {
    color: var(--sun);
    text-shadow: 0 0 18px rgba(215,168,62,.5);
}

.container {
    position: relative;
    z-index: 1;
    max-width: 1150px;

    margin: auto;

    padding: 45px 20px;
}

.hero {
    position: relative;
    z-index: 1;
    text-align: center;

    padding: 76px 20px 110px;
}

.hero::after {
    content: "";
    position: absolute;
    left: 50%;
    bottom: 30px;
    width: min(720px, 80vw);
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--gold), transparent);
    opacity: .5;
}

.hero-emblem {
    display: inline-grid;
    place-items: center;
    width: 106px;
    height: 106px;
    margin-bottom: 10px;
    border: 1px solid rgba(215,168,62,.68);
    border-radius: 50% 47% 53% 44%;
    color: var(--sun);
    font-family: "Noto Sans Gurmukhi", "Segoe UI Symbol", serif;
    font-size: 60px;
    line-height: 1;
    box-shadow: 0 0 0 10px rgba(215,168,62,.04), 0 0 50px rgba(215,168,62,.22);
    animation: seal-float 7s ease-in-out infinite;
}

@keyframes seal-float {
    0%, 100% { transform: translateY(0) rotate(-3deg); border-radius: 50% 47% 53% 44%; }
    50% { transform: translateY(-8px) rotate(3deg); border-radius: 46% 54% 45% 55%; }
}

.hero h1 {
    font-size:
        clamp(
            50px,
            8vw,
            90px
        );

    margin: 15px 0;
    animation: title-rise .9s cubic-bezier(.2,.8,.2,1) both;
}

@keyframes title-rise {
    from { opacity: 0; transform: translateY(18px); letter-spacing: .04em; }
    to { opacity: 1; transform: translateY(0); letter-spacing: 0; }
}

h1 {
    font-size: 42px;
}

h2 {
    color: var(--sun);
}

.card {
    position: relative;
    overflow: hidden;
    background: linear-gradient(145deg, rgba(255,255,255,.09), rgba(255,255,255,.025));

    border:
        1px solid
        var(--line);

    border-radius: 18px 18px 26px 16px;

    padding: 28px;

    margin-bottom: 20px;
    box-shadow: 0 18px 50px rgba(0,0,0,.16), inset 0 1px 0 rgba(255,255,255,.08);
    animation: surface-in .65s ease both;
}

.card::before {
    content: "";
    position: absolute;
    top: -70px;
    right: -50px;
    width: 180px;
    height: 110px;
    border-radius: 48% 52% 42% 58%;
    background: rgba(215,168,62,.1);
    filter: blur(4px);
    transform: rotate(-18deg);
    pointer-events: none;
}

@keyframes surface-in {
    from { opacity: 0; transform: translateY(12px); }
    to { opacity: 1; transform: translateY(0); }
}

.card:nth-child(2) { animation-delay: .08s; }
.card:nth-child(3) { animation-delay: .16s; }
.card:nth-child(4) { animation-delay: .24s; }

.grid {
    display: grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(240px, 1fr)
        );

    gap: 20px;
}

.center {
    text-align: center;
}

.muted {
    color: #aeb4c8;

    line-height: 1.6;
}

.small {
    color: #9b9eb3;

    font-size: 13px;

    letter-spacing: 1px;
}

.stat {
    color: var(--sun);

    font-size: 36px;
    font-weight: bold;

    margin: 8px 0;
}

input,
textarea,
select {
    width: 100%;

    padding: 14px;

    margin:
        7px 0
        17px;

    border-radius: 10px;

    border:
        1px solid
        rgba(255,255,255,.15);

    background: #080d1b;

    color: white;

    font-size: 15px;
    outline: none;
    transition: border-color .2s ease, box-shadow .2s ease, background .2s ease;
}

input:focus,
textarea:focus,
select:focus {
    border-color: var(--gold);
    background: #0d1325;
    box-shadow: 0 0 0 3px rgba(215,168,62,.18);
}

textarea {
    min-height: 100px;

    resize: vertical;

    font-family: Arial;
}

button,
.button {
    display: inline-block;

    padding: 13px 20px;

    border: none;

    border-radius: 10px;

    background:
        linear-gradient(
            135deg,
            var(--sun),
            var(--gold)
        );

    color: #061a2b;

    font-weight: bold;

    text-decoration: none;

    cursor: pointer;
    position: relative;
    overflow: hidden;
    transition: transform .22s ease, box-shadow .22s ease;
}

button::after,
.button::after {
    content: "";
    position: absolute;
    top: -40%;
    bottom: -40%;
    left: -40%;
    width: 24%;
    background: rgba(255,255,255,.42);
    transform: rotate(20deg) translateX(-160%);
    transition: transform .65s ease;
}

button:hover::after,
.button:hover::after {
    transform: rotate(20deg) translateX(700%);
}

button:hover,
.button:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 24px rgba(215,168,62,.22);
}

.dark-button {
    background:
        rgba(255,255,255,.08);

    color: white;
}

.danger {
    background: #7d3140;

    color: white;
}

.success {
    color: #70e0a0;
}

.warning {
    color: var(--sun);
}

.alert {
    padding: 15px;

    margin-bottom: 20px;

    border-radius: 10px;

    background:
        rgba(244,204,92,.1);

    border:
        1px solid
        rgba(244,204,92,.25);
}

.error {
    background:
        rgba(200,50,50,.15);

    border:
        1px solid
        rgba(255,80,80,.3);
}

.progress {
    width: 100%;

    height: 22px;

    background:
        rgba(255,255,255,.1);

    border-radius: 20px;

    overflow: hidden;
}

.progress-bar {
    height: 100%;

    background:
        linear-gradient(
            90deg,
            #9d7220,
            #ffe18a
        );
}

footer {
    position: relative;
    z-index: 1;
}

@media (prefers-reduced-motion: reduce) {
    *,
    *::before,
    *::after {
        animation-duration: .01ms !important;
        animation-iteration-count: 1 !important;
        scroll-behavior: auto !important;
    }
}

table {
    width: 100%;

    border-collapse: collapse;
}

th,
td {
    padding: 13px;

    border-bottom:
        1px solid
        rgba(255,255,255,.1);

    text-align: left;
}

th {
    color: var(--sun);
}

footer {
    text-align: center;

    padding: 45px;

    color: #777d96;
}

@media(max-width:700px) {

    nav {
        padding: 12px 15px;

        flex-wrap: wrap;

        gap: 10px;
    }

    nav a {
        margin-left: 7px;

        font-size: 13px;
    }

    h1 {
        font-size: 34px;
    }

    table {
        font-size: 13px;
    }

    .hero {
        padding: 58px 20px 88px;
    }

    .hero-emblem {
        width: 84px;
        height: 84px;
        font-size: 47px;
    }

}

.nav-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.nav-actions a{margin-left:0}.translate-button{padding:9px 13px;font-size:13px}.translate-host{display:none!important}.badge{display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;background:rgba(215,168,62,.12);border:1px solid rgba(215,168,62,.28);color:#ffe7a1;font-size:12px;font-weight:700}.table-wrap{overflow-x:auto}.action-panel{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.status-flow{display:grid;grid-template-columns:repeat(5,minmax(90px,1fr));gap:7px}.status-step{padding:8px 5px;border-radius:9px;background:rgba(255,255,255,.05);text-align:center;font-size:11px;color:#9fa8bd}.status-step.active{color:#ffe7a1;border:1px solid rgba(215,168,62,.45)}.progress-label{display:flex;justify-content:space-between;margin-bottom:7px;font-size:13px;color:#c8cede}.empty-state{text-align:center;padding:35px 20px}@media(max-width:700px){.status-flow{grid-template-columns:1fr}.nav-actions{width:100%}.container{padding:28px 14px}button,.button{min-height:44px}}</style>
"""


# ============================================================
# LAYOUT
# ============================================================

def layout(content, title="Smart Seva"):

    user = current_user()

    unread_count = 0
    if user and user["role"] != "admin":
        try:
            db = get_db()
            unread_count = int(db.execute("SELECT COUNT(*) AS count FROM notifications WHERE user_id = ? AND is_read = FALSE", (user["id"],)).fetchone()["count"] or 0)
            db.close()
        except Exception:
            unread_count = 0

    if user:

        if user["role"] == "admin":

            nav = """
                <a href="/admin">Dashboard</a>
                <a href="/admin/seva">Manage Seva</a>
                <a href="/admin/paath">Manage Paath</a>
                <a href="/admin/pantry">Manage Pantry Needs</a>
                <a href="/logout">Logout</a>
            """

        else:

            nav = """
                <a href="/dashboard">Dashboard</a>
                <a href="/seva">Seva</a>
                <a href="/paath">Paath</a>
                <a href="/pantry">Pantry</a>
                <a href="/profile">Profile</a>
                <a href="/notifications" aria-label="Notifications">🔔{unread_count and f"<span class=\'badge\'>{unread_count}</span>" or ""}</a>
                <a href="/logout">Logout</a>
            """

    else:

        nav = """
            <a href="/login">Login</a>
            <a href="/register">Register</a>
        """

    return f"""
    <!DOCTYPE html>

    <html>

    <head>

        <title>
            {h(title)} | Smart Seva
        </title>

        <meta
            name="viewport"
            content="width=device-width, initial-scale=1"
        >

        {STYLE}

    </head>

    <body>

        <div class="night-sky" aria-hidden="true">
            <span class="ik-constellation ik-one">ੴ</span>
            <span class="ik-constellation ik-two">ੴ</span>
            <span class="ik-constellation ik-three">ੴ</span>
        </div>

        <nav>

            <div class="logo">
                <span class="logo-mark" aria-label="Ik Onkar">ੴ</span>
                <span class="gold">
                    Smart Seva
                </span>
            </div>

            <div class="nav-actions">
                {nav}
                <button type="button" class="translate-button dark-button" onclick="translatePageToPunjabi()">ਪੰਜਾਬੀ</button>
            </div>
            <div id="google_translate_element" class="translate-host" aria-hidden="true"></div>

        </nav>

        {content}

        <footer>
            ੴ • Seva • Sangat • Chardi Kala
        </footer>
        <script>
        function googleTranslateElementInit(){{
            new google.translate.TranslateElement(
                {{pageLanguage:"en",includedLanguages:"pa",autoDisplay:false}},
                "google_translate_element"
            );
        }}

        function fireTranslateChange(element){{
            if(!element) return;
            try{{
                element.dispatchEvent(new Event("change",{{bubbles:true}}));
            }}catch(e){{
                var event=document.createEvent("HTMLEvents");
                event.initEvent("change",true,true);
                element.dispatchEvent(event);
            }}
        }}

        function translatePageToPunjabi(){{
            var select=document.querySelector(".goog-te-combo");
            if(select){{
                select.value="pa";
                fireTranslateChange(select);
                return;
            }}
            setTimeout(function(){{
                var retry=document.querySelector(".goog-te-combo");
                if(retry){{
                    retry.value="pa";
                    fireTranslateChange(retry);
                }}
            }},500);
        }}</script>
        <script src="https://translate.google.com/translate_a/element.js?cb=googleTranslateElementInit"></script>

    </body>

    </html>
    """


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    return layout(
        """
        <section class="hero">

            <div class="hero-emblem" aria-label="Ik Onkar emblem">
                ੴ
            </div>

            <div class="small">
                SEVA • SANGAT • COMMUNITY
            </div>

            <h1>
                Serve.
                <span class="gold">
                    Track.
                </span>
                Achieve.
            </h1>

            <p
                class="muted"
                style="max-width:700px;margin:25px auto;font-size:19px"
            >
                Smart Seva helps students participate in
                community service, track verified hours,
                and earn certificates.
            </p>

            <br>

            <a
                class="button"
                href="/register"
            >
                Create Account →
            </a>

            <a
                class="button dark-button"
                href="/login"
                style="margin-left:10px"
            >
                Login
            </a>

        </section>

        <div class="container">

            <div class="grid">

                <div class="card center">
                    <div class="stat">🙏</div>
                    <h3>Discover Seva</h3>
                    <p class="muted">
                        Find community service opportunities.
                    </p>
                </div>

                <div class="card center">
                    <div class="stat">⏱</div>
                    <h3>Track Hours</h3>
                    <p class="muted">
                        Check in and check out of seva.
                    </p>
                </div>

                <div class="card center">
                    <div class="stat">🏆</div>
                    <h3>Earn Certificates</h3>
                    <p class="muted">
                        Reach your goal and receive a certificate.
                    </p>
                </div>

            </div>

        </div>
        """,
        "Home"
    )


# ============================================================
# REGISTER
# ============================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    error = ""

    if request.method == "POST":
        validate_csrf()

        name = clean(request.form.get("name"), 100)
        email = clean(request.form.get("email"), 255).lower()
        password = request.form.get("password", "")
        goal_raw = request.form.get("goal", "40")

        try:
            goal = float(goal_raw)
        except (TypeError, ValueError):
            goal = 0

        if not name or not email or not password:
            error = "Please fill out every field."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif goal < 1 or goal > 10000:
            error = "Goal must be between 1 and 10,000 hours."

        if not error:
            try:
                auth = supabase_auth_request(
                    "signup",
                    {
                        "email": email,
                        "password": password,
                        "data": {
                            "name": name,
                            "goal_hours": goal,
                        },
                    },
                )

                auth_user = auth.get("user") or {}
                auth_user_id = auth_user.get("id")

                if not auth_user_id:
                    raise ValueError(
                        "Supabase did not return a user. Check your Supabase email settings."
                    )

                # The Supabase trigger creates public.users automatically.
                # No password is stored in Smart Seva's database.
                db = get_db()
                profile = db.execute(
                    "SELECT id FROM public.users WHERE id = ?",
                    (auth_user_id,)
                ).fetchone()
                db.close()

                if not profile:
                    raise RuntimeError(
                        "Supabase created the account but did not create the Smart Seva profile. "
                        "Check the on_auth_user_created trigger."
                    )

                return redirect(url_for("login", registered=1))

            except (ValueError, RuntimeError) as exc:
                error = str(exc)
            except IntegrityError:
                error = "That email is already registered."

    token = h(csrf_token())

    return layout(
        f"""
        <div class="container">
            <div class="card" style="max-width:520px;margin:auto">
                <h1>Create Account</h1>
                {f'<div class="alert error">{h(error)}</div>' if error else ""}
                <form method="POST">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <label>Full Name</label>
                    <input name="name" maxlength="100" required>
                    <label>Email</label>
                    <input type="email" name="email" required>
                    <label>Password</label>
                    <input type="password" name="password" minlength="8" required>
                    <label>Seva Goal (hours)</label>
                    <input type="number" name="goal" value="40" min="1" max="10000" required>
                    <button>Create Account</button>
                </form>
            </div>
        </div>
        """,
        "Register"
    )


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    error = ""

    if request.method == "POST":
        validate_csrf()

        email = clean(request.form.get("email"), 255).lower()
        password = request.form.get("password", "")

        try:
            auth = supabase_auth_request(
                "token?grant_type=password",
                {
                    "email": email,
                    "password": password,
                },
            )

            auth_user = auth.get("user") or {}
            auth_user_id = auth_user.get("id")

            if not auth_user_id:
                raise ValueError("Incorrect email or password.")

            db = get_db()
            user = db.execute(
                "SELECT * FROM public.users WHERE id = ?",
                (auth_user_id,)
            ).fetchone()

            # Normally the trigger has already created this profile.
            # This fallback keeps existing Auth accounts usable if the trigger
            # was temporarily missing when they were created.
            if not user:
                metadata = auth_user.get("user_metadata") or {}
                name = clean(metadata.get("name") or email.split("@")[0], 100)
                try:
                    goal = float(metadata.get("goal_hours") or 40)
                except (TypeError, ValueError):
                    goal = 40
                goal = min(max(goal, 1), 10000)

                db.execute(
                    """
                    INSERT INTO public.users (id, name, email, role, goal_hours)
                    VALUES (?, ?, ?, 'student', ?)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (auth_user_id, name, email, goal)
                )
                db.commit()
                user = db.execute(
                    "SELECT * FROM public.users WHERE id = ?",
                    (auth_user_id,)
                ).fetchone()

            db.close()

            if not user:
                raise RuntimeError("Your Supabase account does not have a Smart Seva profile.")

            session.clear()
            session["user_id"] = str(user["id"])
            session["csrf_token"] = secrets.token_urlsafe(32)

            if user["role"] == "admin":
                return redirect(url_for("admin"))

            return redirect(url_for("dashboard"))

        except (ValueError, RuntimeError) as exc:
            error = str(exc) if str(exc) else "Incorrect email or password."

    token = h(csrf_token())
    registered = request.args.get("registered")
    message = "" if not registered else """
        <div class="alert success">
            Account created successfully. You can now log in.
        </div>
    """

    return layout(
        f"""
        <div class="container">
            <div class="card" style="max-width:520px;margin:auto">
                <h1>Login</h1>
                {message}
                {f'<div class="alert error">{h(error)}</div>' if error else ""}
                <form method="POST">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <label>Email</label>
                    <input type="email" name="email" required>
                    <label>Password</label>
                    <input type="password" name="password" required>
                    <button>Login</button>
                </form>
                <br>
                <p class="muted">Student? <a href="/register" style="color:#ffe7a1">Create an account</a></p>
            </div>
        </div>
        """,
        "Login"
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")