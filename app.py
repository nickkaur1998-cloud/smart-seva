import os
import secrets
import json
import re
import qrcode
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

        role_emails = {
            "admin": os.environ.get("SMART_SEVA_ADMIN_EMAIL", "").strip().lower(),
            "seva_admin": os.environ.get("SMART_SEVA_SEVA_ADMIN_EMAIL", "").strip().lower(),
            "paath_admin": os.environ.get("SMART_SEVA_PAATH_ADMIN_EMAIL", "").strip().lower(),
            "pantry_admin": os.environ.get("SMART_SEVA_PANTRY_ADMIN_EMAIL", "").strip().lower(),
            "events_admin": os.environ.get("SMART_SEVA_EVENTS_ADMIN_EMAIL", "").strip().lower(),
        }

        configured_emails = [email for email in role_emails.values() if email]
        if len(configured_emails) != len(set(configured_emails)):
            raise RuntimeError("Each Smart Seva admin role must use a different email address.")

        # These management roles are controlled by the deployment settings.
        # Reset them first so placeholder emails can safely be replaced later.
        db.execute(
            "UPDATE public.users SET role = 'student' WHERE role IN ('admin', 'seva_admin', 'paath_admin', 'pantry_admin', 'events_admin')"
        )

        for role, email in role_emails.items():
            if email:
                db.execute(
                    "UPDATE public.users SET role = ? WHERE lower(email) = lower(?)",
                    (role, email)
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


def role_required(*allowed_roles):
    def decorator(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login", next=request.path))
            if user["role"] not in allowed_roles:
                abort(403)
            return function(*args, **kwargs)
        return wrapper
    return decorator


def admin_required(function):
    return role_required("admin")(function)


def seva_admin_required(function):
    return role_required("admin", "seva_admin")(function)


def paath_admin_required(function):
    return role_required("admin", "paath_admin")(function)


def pantry_admin_required(function):
    return role_required("admin", "pantry_admin")(function)


def events_admin_required(function):
    return role_required("admin", "events_admin")(function)


def management_home():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    destinations = {
        "admin": "admin",
        "seva_admin": "admin_seva",
        "paath_admin": "admin_paath",
        "pantry_admin": "admin_pantry",
        "events_admin": "admin_events",
    }
    return redirect(url_for(destinations.get(user["role"], "dashboard")))


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

.filter-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;align-items:end}.filter-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.filter-summary{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.filter-chip{padding:7px 11px;border:1px solid rgba(215,168,62,.25);border-radius:999px;background:rgba(255,255,255,.05);font-size:12px}.capacity-bar{height:8px;border-radius:20px;background:rgba(255,255,255,.1);overflow:hidden;margin:7px 0 10px}.capacity-fill{height:100%;background:linear-gradient(90deg,#4d9f7a,#ffe18a)}.calendar{display:grid;grid-template-columns:repeat(7,1fr);gap:7px}.calendar-head,.calendar-day{min-height:72px;padding:8px;border:1px solid rgba(255,255,255,.08);border-radius:9px}.calendar-head{min-height:auto;text-align:center;color:#ffe7a1;font-size:12px}.calendar-day{background:rgba(255,255,255,.025)}.calendar-day.today{border-color:rgba(215,168,62,.65);box-shadow:inset 0 0 0 1px rgba(215,168,62,.2)}.calendar-day.empty{opacity:.25}.calendar-num{font-weight:bold}.calendar-event{display:block;margin-top:5px;padding:4px 5px;border-radius:6px;background:rgba(215,168,62,.13);color:#ffe7a1;text-decoration:none;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.calendar-nav{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}.calendar-nav button{padding:8px 12px}.flow-card{margin-top:14px;padding:14px;border-radius:12px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08)}@media(max-width:700px){.calendar{gap:3px}.calendar-head,.calendar-day{min-height:58px;padding:5px;font-size:11px}.calendar-event{font-size:9px;padding:3px}.filter-grid{grid-template-columns:1fr}}
.nav-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.nav-actions a{margin-left:0}.translate-button{padding:9px 13px;font-size:13px}.translate-host{display:none!important}.badge{display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;background:rgba(215,168,62,.12);border:1px solid rgba(215,168,62,.28);color:#ffe7a1;font-size:12px;font-weight:700}.table-wrap{overflow-x:auto}.action-panel{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.status-flow{display:grid;grid-template-columns:repeat(5,minmax(90px,1fr));gap:7px}.status-step{padding:8px 5px;border-radius:9px;background:rgba(255,255,255,.05);text-align:center;font-size:11px;color:#9fa8bd}.status-step.active{color:#ffe7a1;border:1px solid rgba(215,168,62,.45)}.progress-label{display:flex;justify-content:space-between;margin-bottom:7px;font-size:13px;color:#c8cede}.pantry-progress{margin:14px 0 18px}.pantry-progress-track{height:10px;border-radius:999px;background:rgba(255,255,255,.09);border:1px solid rgba(215,168,62,.18);overflow:hidden}.pantry-progress-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#b9872d,#ffe7a1);box-shadow:0 0 16px rgba(215,168,62,.28);transition:width .5s ease}.empty-state{text-align:center;padding:35px 20px}@media(max-width:700px){.status-flow{grid-template-columns:1fr}.nav-actions{width:100%}.container{padding:28px 14px}button,.button{min-height:44px}}
.qr-menu-button{padding:9px 13px;font-size:13px;cursor:pointer;border:1px solid rgba(215,168,62,.3);border-radius:10px;background:rgba(255,255,255,.05);color:#dce8ef}.qr-menu-button:hover{color:#ffe7a1;border-color:rgba(215,168,62,.65)}.qr-modal{display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.72);align-items:center;justify-content:center;padding:20px}.qr-modal.open{display:flex}.qr-box{width:min(360px,100%);text-align:center;background:#0b1020;border:1px solid rgba(215,168,62,.35);border-radius:20px;padding:28px;box-shadow:0 25px 80px rgba(0,0,0,.5)}.qr-box img{width:230px;height:230px;max-width:80vw;background:white;padding:10px;border-radius:12px}.qr-close{margin-top:16px}.qr-url{word-break:break-all;font-size:12px;color:#9fa8bd;margin-top:12px}
.goog-te-banner-frame,
.goog-te-banner-frame.skiptranslate,
iframe.goog-te-banner-frame {
    display: none !important;
}

body {
    top: 0 !important;
}

#google_translate_element,
.goog-te-banner-frame,
iframe.goog-te-banner-frame,
.goog-te-balloon-frame,
#goog-gt-tt,
.goog-tooltip,
.goog-te-spinner-pos {
    display: none !important;
}

.goog-text-highlight {
    background: transparent !important;
    box-shadow: none !important;
}
</style>
"""


# ============================================================
# SITE QR CODE
# ============================================================

@app.route("/qr-code")
def qr_code():
    """Generate a QR code that opens the current Smart Seva site."""
    site_url = request.url_root.rstrip("/")
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(site_url)
    qr.make(fit=True)

    image = qr.make_image(fill_color="black", back_color="white")
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)

    return send_file(
        output,
        mimetype="image/png",
        download_name="smart-seva-qr.png",
        max_age=300,
    )


# ============================================================
# LAYOUT
# ============================================================

def layout(content, title="Smart Seva"):

    user = current_user()

    unread_count = 0
    if user and user["role"] == "student":
        try:
            db = get_db()
            unread_count = int(db.execute("SELECT COUNT(*) AS count FROM notifications WHERE user_id = ? AND is_read = FALSE", (user["id"],)).fetchone()["count"] or 0)
            db.close()
        except Exception:
            unread_count = 0

    notification_link = f'<span class="badge">{unread_count}</span>' if unread_count else ""

    if user:

        if user["role"] == "admin":
            nav = """
                <a href="/admin">Dashboard</a>
                <a href="/admin/seva">Manage Seva</a>
                <a href="/admin/paath">Manage Paath</a>
                <a href="/admin/pantry">Manage Pantry</a>
                <a href="/admin/events">Manage Events</a>
                <a href="/events">Events</a>
                <a href="/my-calendar">My Calendar</a>
                <a href="/impact">Community Impact</a>
                <a href="/logout">Logout</a>
            """
        elif user["role"] == "seva_admin":
            nav = """
                <a href="/admin/seva">Manage Seva</a>
                <a href="/seva">Seva</a>
                <a href="/events">Events</a>
                <a href="/my-calendar">My Calendar</a>
                <a href="/impact">Community Impact</a>
                <a href="/logout">Logout</a>
            """
        elif user["role"] == "paath_admin":
            nav = """
                <a href="/admin/paath">Manage Paath</a>
                <a href="/paath">Paath</a>
                <a href="/events">Events</a>
                <a href="/my-calendar">My Calendar</a>
                <a href="/impact">Community Impact</a>
                <a href="/logout">Logout</a>
            """
        elif user["role"] == "pantry_admin":
            nav = """
                <a href="/admin/pantry">Manage Pantry</a>
                <a href="/pantry">Pantry</a>
                <a href="/events">Events</a>
                <a href="/my-calendar">My Calendar</a>
                <a href="/impact">Community Impact</a>
                <a href="/logout">Logout</a>
            """
        elif user["role"] == "events_admin":
            nav = """
                <a href="/admin/events">Manage Events</a>
                <a href="/events">Events</a>
                <a href="/my-calendar">My Calendar</a>
                <a href="/impact">Community Impact</a>
                <a href="/logout">Logout</a>
            """
        else:
            nav = """
                <a href="/dashboard">Dashboard</a>
                <a href="/seva">Seva</a>
                <a href="/my-calendar">My Calendar</a>
                <a href="/impact">Community Impact</a>
                <a href="/paath">Paath</a>
                <a href="/pantry">Pantry</a>
                <a href="/events">Events</a>
                <a href="/profile">Profile</a>
                <a href="/notifications" aria-label="Notifications">🔔{notification_link}</a>
                <a href="/logout">Logout</a>
            """
            nav = nav.replace("{notification_link}", notification_link)

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
                <button type="button" class="qr-menu-button" onclick="openQrModal()" aria-label="Show QR code for Smart Seva">QR Code</button>
                <button type="button" class="translate-button dark-button" onclick="translatePageToPunjabi()">ਪੰਜਾਬੀ</button>
            </div>

        </nav>

        <div id="qr-modal" class="qr-modal" role="dialog" aria-modal="true" aria-labelledby="qr-title" onclick="closeQrModal(event)">
            <div class="qr-box" onclick="event.stopPropagation()">
                <h2 id="qr-title">📱 Scan to Open Smart Seva</h2>
                <img src="/qr-code" alt="QR code for the Smart Seva website">
                <p class="qr-url" id="qr-site-url"></p>
                <button type="button" class="qr-close" onclick="closeQrModal()">Close</button>
            </div>
        </div>

        <script>
        function openQrModal() {{
            const modal = document.getElementById("qr-modal");
            const url = window.location.origin;
            document.getElementById("qr-site-url").textContent = url;
            modal.classList.add("open");
        }}

        function closeQrModal(event) {{
            if (event && event.target !== event.currentTarget) return;
            document.getElementById("qr-modal").classList.remove("open");
        }}

        document.addEventListener("keydown", function(event) {{
            if (event.key === "Escape") closeQrModal();
        }});
        </script>

        {content}

        <footer>
            ੴ • Seva • Sangat • Chardi Kala
        </footer>
        <script>
        async function translatePageToPunjabi() {{
            const button = document.querySelector(".translate-button");
            if (button) {{
                button.disabled = true;
                button.textContent = "ਪੰਜਾਬੀ…";
            }}

            const nodes = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {{
                acceptNode(node) {{
                    const parent = node.parentElement;
                    if (!parent) return NodeFilter.FILTER_REJECT;
                    if (parent.closest("script, style, noscript, textarea, input, select, option, button")) {{
                        return NodeFilter.FILTER_REJECT;
                    }}
                    if (!node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
                    return NodeFilter.FILTER_ACCEPT;
                }}
            }});

            let node;
            while ((node = walker.nextNode())) nodes.push(node);
            const texts = nodes.map(n => n.nodeValue);

            try {{
                const response = await fetch("/api/translate", {{
                    method: "POST",
                    headers: {{"Content-Type": "application/json"}},
                    body: JSON.stringify({{texts: texts, target: "pa"}})
                }});
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || "Translation failed.");

                if (Array.isArray(data.translations)) {{
                    nodes.forEach((n, i) => {{
                        if (data.translations[i]) n.nodeValue = data.translations[i];
                    }});
                }}
            }} catch (error) {{
                console.error(error);
                alert("Punjabi translation is temporarily unavailable.");
            }} finally {{
                if (button) {{
                    button.disabled = false;
                    button.textContent = "ਪੰਜਾਬੀ";
                }}
            }}
        }}
        </script>

    </body>

    </html>
    """


# ============================================================
# TRANSLATION
# ============================================================

@app.route("/api/translate", methods=["POST"])
@login_required
def translate_api():
    """Translate page text through Google Cloud Translation Basic (v2)."""
    api_key = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "Translation API is not configured."}), 503

    payload = request.get_json(silent=True) or {}
    texts = payload.get("texts", [])
    target = clean(payload.get("target", "pa"), 10)

    if target != "pa" or not isinstance(texts, list):
        return jsonify({"error": "Invalid translation request."}), 400

    texts = [str(item) for item in texts if str(item).strip()]
    if not texts:
        return jsonify({"translations": []})
    if len(texts) > 128:
        return jsonify({"error": "Too many text items in one request."}), 400
    if sum(len(item) for item in texts) > 30000:
        return jsonify({"error": "Page is too large to translate at once."}), 400

    req = Request(
        "https://translation.googleapis.com/language/translate/v2?key=" + api_key,
        data=json.dumps({
            "q": texts,
            "source": "en",
            "target": "pa",
            "format": "text"
        }).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json"
        }
    )

    try:
        with urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
        translations = [
            item.get("translatedText", "")
            for item in result.get("data", {}).get("translations", [])
        ]
        return jsonify({"translations": translations})
    except HTTPError as error:
        try:
            details = json.loads(error.read().decode("utf-8"))
        except Exception:
            details = {}
        message = details.get("error", {}).get("message", "Translation service request failed.")
        return jsonify({"error": message}), 502
    except (URLError, TimeoutError):
        return jsonify({"error": "Translation service could not be reached."}), 502


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

            if user["role"] in ("admin", "seva_admin", "paath_admin", "pantry_admin", "events_admin"):
                return management_home()

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
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


# ============================================================
# SEVA LIST
# ============================================================

@app.route("/seva")
@login_required
def seva():

    user = current_user()
    db = get_db()

    q = clean(request.args.get("q", ""), 100)
    date_filter = clean(request.args.get("date", ""), 20)
    location_filter = clean(request.args.get("location", ""), 100)
    availability = clean(request.args.get("availability", "all"), 20)
    time_filter = clean(request.args.get("time", "all"), 20)
    sort = clean(request.args.get("sort", "soonest"), 20)

    where = []
    params = [user["id"]]

    if q:
        where.append("(lower(seva.title) LIKE lower(?) OR lower(seva.description) LIKE lower(?) OR lower(seva.location) LIKE lower(?))")
        term = "%" + q + "%"
        params.extend([term, term, term])
    if date_filter:
        where.append("seva.date = ?")
        params.append(date_filter)
    if location_filter:
        where.append("lower(seva.location) LIKE lower(?)")
        params.append("%" + location_filter + "%")
    if availability == "available":
        where.append("(seva.max_volunteers = 0 OR (SELECT COUNT(*) FROM signups s2 WHERE s2.seva_id = seva.id AND s2.status != 'rejected') < seva.max_volunteers)")
    elif availability == "full":
        where.append("(seva.max_volunteers > 0 AND (SELECT COUNT(*) FROM signups s2 WHERE s2.seva_id = seva.id AND s2.status != 'rejected') >= seva.max_volunteers)")
    if time_filter == "morning":
        where.append("CAST(seva.start_time AS time) < CAST('12:00' AS time)")
    elif time_filter == "afternoon":
        where.append("CAST(seva.start_time AS time) >= CAST('12:00' AS time) AND CAST(seva.start_time AS time) < CAST('17:00' AS time)")
    elif time_filter == "evening":
        where.append("CAST(seva.start_time AS time) >= CAST('17:00' AS time)")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = "seva.date ASC, seva.start_time ASC" if sort == "soonest" else "seva.date DESC, seva.start_time DESC"

    opportunities = db.execute(
        f"""
        SELECT seva.*,
               signups.id AS signup_id,
               (SELECT COUNT(*) FROM signups s2 WHERE s2.seva_id = seva.id AND s2.status != 'rejected') AS signup_count
        FROM seva
        LEFT JOIN signups ON seva.id = signups.seva_id AND signups.user_id = ?
        {where_sql}
        ORDER BY {order_sql}
        """,
        tuple(params)
    ).fetchall()

    calendar_rows = db.execute(
        "SELECT id, title, date, start_time, end_time, location FROM seva ORDER BY date ASC, start_time ASC"
    ).fetchall()
    db.close()

    token = h(csrf_token())
    cards = ""
    for item in opportunities:
        filled = int(item["signup_count"] or 0)
        capacity = int(item["max_volunteers"] or 0)
        full = bool(capacity and filled >= capacity)
        remaining = max(0, capacity - filled) if capacity else None
        capacity_pct = min(100, round(filled / capacity * 100)) if capacity else 0

        if item["signup_id"]:
            action = '<span class="success">✓ Signed Up</span>'
        elif full:
            action = '<span class="warning">Full — no spots remaining</span>'
        else:
            action = f"""
            <form method="POST" action="/signup/{item["id"]}">
                <input type="hidden" name="csrf_token" value="{token}">
                <button>Sign Up</button>
            </form>
            """

        capacity_html = (
            f'<div class="small">👥 {filled} / {capacity} volunteers</div>'
            f'<div class="capacity-bar"><div class="capacity-fill" style="width:{capacity_pct}%"></div></div>'
            f'<div class="small">{remaining} spot(s) remaining</div>'
            if capacity else
            '<div class="small">👥 Open capacity</div>'
        )

        cards += f"""
        <div class="card">
            <h2>{h(item["title"])}</h2>
            <p class="muted">{h(item["description"])}</p>
            <p>📍 {h(item["location"])}</p>
            <p>📅 {h(item["date"])}</p>
            <p>⏰ {h(format_time_label(item["start_time"]))} – {h(format_time_label(item["end_time"]))}</p>
            {capacity_html}
            <div class="action-panel">{action}</div>
        </div>
        """

    if not cards:
        cards = """
        <div class="card empty-state">
            <h2>🌱 No Sevas Found</h2>
            <p class="muted">Try changing your filters or check back soon.</p>
        </div>
        """

    calendar_data = json.dumps([
        {"id": int(x["id"]), "title": str(x["title"]), "date": str(x["date"]),
         "start_time": str(x["start_time"])[:5], "location": str(x["location"])}
        for x in calendar_rows
    ], ensure_ascii=False)

    active_filters = []
    if q: active_filters.append("Search: " + q)
    if date_filter: active_filters.append("Date: " + date_filter)
    if location_filter: active_filters.append("Location: " + location_filter)
    if availability != "all": active_filters.append(availability.title())
    if time_filter != "all": active_filters.append(time_filter.title())

    chips = "".join(f'<span class="filter-chip">{h(x)}</span>' for x in active_filters)
    filter_summary = f'<div class="filter-summary">{chips}</div>' if chips else '<p class="small">Showing all Seva opportunities.</p>'

    return layout(
        f"""
        <div class="container">
            <h1>Seva Opportunities</h1>
            <p class="muted">Find the right opportunity, check availability, and plan your service.</p>

            <div class="card">
                <h2>🔎 Find Your Seva</h2>
                <form method="GET">
                    <div class="filter-grid">
                        <label>Search
                            <input name="q" value="{h(q)}" placeholder="Food, cleanup, community...">
                        </label>
                        <label>Date
                            <input type="date" name="date" value="{h(date_filter)}">
                        </label>
                        <label>Location
                            <input name="location" value="{h(location_filter)}" placeholder="Gurdwara, Fremont...">
                        </label>
                        <label>Availability
                            <select name="availability">
                                <option value="all" {"selected" if availability == "all" else ""}>All</option>
                                <option value="available" {"selected" if availability == "available" else ""}>Available spots</option>
                                <option value="full" {"selected" if availability == "full" else ""}>Full</option>
                            </select>
                        </label>
                        <label>Time
                            <select name="time">
                                <option value="all" {"selected" if time_filter == "all" else ""}>Any time</option>
                                <option value="morning" {"selected" if time_filter == "morning" else ""}>Morning</option>
                                <option value="afternoon" {"selected" if time_filter == "afternoon" else ""}>Afternoon</option>
                                <option value="evening" {"selected" if time_filter == "evening" else ""}>Evening</option>
                            </select>
                        </label>
                        <label>Sort
                            <select name="sort">
                                <option value="soonest" {"selected" if sort == "soonest" else ""}>Soonest first</option>
                                <option value="latest" {"selected" if sort == "latest" else ""}>Latest first</option>
                            </select>
                        </label>
                    </div>
                    <div class="filter-actions">
                        <button>Apply Filters</button>
                        <a class="button dark-button" href="/seva">Clear Filters</a>
                    </div>
                </form>
                {filter_summary}
            </div>

            <div class="card">
                <div class="calendar-nav">
                    <button type="button" class="dark-button" onclick="changeCalendar(-1)">← Previous</button>
                    <h2 id="calendar-title" style="margin:0">Seva Calendar</h2>
                    <button type="button" class="dark-button" onclick="changeCalendar(1)">Next →</button>
                </div>
                <div id="seva-calendar" class="calendar"></div>
                <p class="small">Click a Seva on the calendar to filter the list by that date.</p>
            </div>

            <div class="grid" id="seva-list">
                {cards}
            </div>

            <script>
            const calendarItems = {calendar_data};
            let calendarCursor = new Date();
            function renderCalendar() {{
                const root = document.getElementById("seva-calendar");
                const title = document.getElementById("calendar-title");
                if (!root || !title) return;
                const year = calendarCursor.getFullYear();
                const month = calendarCursor.getMonth();
                const first = new Date(year, month, 1);
                const last = new Date(year, month + 1, 0);
                title.textContent = first.toLocaleString(undefined, {{month:"long", year:"numeric"}}) + " Seva Calendar";
                const names = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
                root.innerHTML = names.map(function(n) {{ return '<div class="calendar-head">' + n + '</div>'; }}).join("");
                for (let i = 0; i < first.getDay(); i++) root.innerHTML += '<div class="calendar-day empty"></div>';
                const today = new Date();
                for (let day = 1; day <= last.getDate(); day++) {{
                    const key = year + "-" + String(month + 1).padStart(2,"0") + "-" + String(day).padStart(2,"0");
                    const events = calendarItems.filter(function(x) {{ return x.date === key; }});
                    const isToday = today.getFullYear() === year && today.getMonth() === month && today.getDate() === day;
                    let eventHtml = "";
                    events.forEach(function(x) {{
                        eventHtml += '<a class="calendar-event" href="/seva?date=' + encodeURIComponent(x.date) + '">' + x.start_time + ' · ' + x.title + '</a>';
                    }});
                    root.innerHTML += '<div class="calendar-day ' + (isToday ? "today" : "") + '"><div class="calendar-num">' + day + '</div>' + eventHtml + '</div>';
                }}
            }}
            function changeCalendar(delta) {{
                calendarCursor.setMonth(calendarCursor.getMonth() + delta);
                renderCalendar();
            }}
            renderCalendar();
            </script>
        </div>
        """,
        "Seva"
    )


@app.route("/api/seva")
@login_required
def seva_api():

    user = current_user()
    db = get_db()
    opportunities = db.execute(
        """
        SELECT seva.id, seva.title, seva.description, seva.location, seva.date,
               seva.start_time, seva.end_time, seva.max_volunteers,
               (SELECT COUNT(*) FROM signups s2 WHERE s2.seva_id = seva.id AND s2.status != 'rejected') AS signup_count,
               EXISTS(SELECT 1 FROM signups WHERE signups.seva_id = seva.id AND signups.user_id = ?) AS signed_up
        FROM seva
        ORDER BY seva.date ASC, seva.start_time ASC
        """,
        (user["id"],)
    ).fetchall()
    db.close()
    return jsonify([
        {"id": item["id"], "title": item["title"], "description": item["description"],
         "location": item["location"], "date": str(item["date"]), "start_time": str(item["start_time"])[:5],
         "end_time": str(item["end_time"])[:5], "signed_up": bool(item["signed_up"]),
         "signup_count": int(item["signup_count"] or 0), "max_volunteers": int(item["max_volunteers"] or 0)}
        for item in opportunities
    ])


# ============================================================
# PAATH SLOTS
# ============================================================

@app.route("/paath")
@login_required
def paath():

    user = current_user()

    if user["role"] == "admin":
        return redirect(url_for("admin_paath"))

    db = get_db()

    slots = db.execute(
        """
        SELECT
            paath_slots.*,
            COUNT(
                CASE
                    WHEN paath_signups.status IN ('pending', 'approved')
                    THEN 1
                END
            ) AS booked_count,
            own_signup.status AS own_status
        FROM paath_slots
        LEFT JOIN paath_signups
            ON paath_signups.paath_id = paath_slots.id
        LEFT JOIN paath_signups AS own_signup
            ON own_signup.paath_id = paath_slots.id
            AND own_signup.user_id = ?
        GROUP BY paath_slots.id, own_signup.status
        ORDER BY paath_slots.date ASC, paath_slots.start_time ASC
        """,
        (user["id"],)
    ).fetchall()

    db.close()

    token = h(csrf_token())
    cards = ""

    for slot in slots:
        booked = int(slot["booked_count"] or 0)
        capacity = int(slot["capacity"] or 1)
        own_status = slot["own_status"]
        available = capacity - booked

        if own_status == "approved":
            action = '<span class="success">✓ Sponsorship approved</span>'
        elif own_status == "pending":
            action = '<span class="warning">Request pending approval</span>'
        elif own_status == "rejected":
            action = f'''
                <form method="POST" action="/paath/signup/{slot["id"]}">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <button>Request Again</button>
                </form>
            '''
        elif available <= 0:
            action = '<span class="muted">This paath slot is full</span>'
        else:
            action = f'''
                <form method="POST" action="/paath/signup/{slot["id"]}">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <button>Sponsor This Paath</button>
                </form>
            '''

        cards += f"""
        <div class="card">
            <h2>{h(slot["title"])}</h2>
            <p class="muted">{h(slot["description"])}</p>
            <p>📅 {h(slot["date"])}</p>
            <p>⏰ {h(slot["start_time"])} – {h(slot["end_time"])}</p>
            <p><strong>Sponsorship: ${float(slot["price"]):,.2f}</strong></p>
            <p class="small">{available} of {capacity} slot(s) available</p>
            {action}
        </div>
        """

    if not cards:
        cards = """
        <div class="card">
            <h2>No Paath Slots Yet</h2>
            <p class="muted">The administrator has not published any paath slots.</p>
        </div>
        """

    return layout(
        f"""
        <div class="container">
            <h1>Paath Sponsorship</h1>
            <p class="muted">
                Choose a date and time to sponsor a paath. Your request will be
                reviewed by the administrator.
            </p>
            <div class="grid">{cards}</div>
        </div>
        """,
        "Paath"
    )


@app.route("/paath/signup/<int:paath_id>", methods=["POST"])
@login_required
def paath_signup(paath_id):

    validate_csrf()
    user = current_user()

    if user["role"] == "admin":
        return redirect(url_for("admin_paath"))

    db = get_db()
    slot = db.execute(
        "SELECT * FROM paath_slots WHERE id = ?",
        (paath_id,)
    ).fetchone()

    if not slot:
        db.close()
        abort(404)

    booked = db.execute(
        """
        SELECT COUNT(*)
        FROM paath_signups
        WHERE paath_id = ?
        AND status IN ('pending', 'approved')
        """,
        (paath_id,)
    ).fetchone()[0]

    if booked >= slot["capacity"]:
        db.close()
        abort(400, "This paath slot is already full.")

    try:
        db.execute(
            """
            INSERT INTO paath_signups
            (paath_id, user_id, amount, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (paath_id, user["id"], slot["price"], utc_now())
        )
        db.commit()
    except IntegrityError:
        pass
    finally:
        db.close()

    return redirect(url_for("paath"))


@app.route("/admin/paath")
@paath_admin_required
def admin_paath():

    db = get_db()
    slots = db.execute(
        """
        SELECT
            paath_slots.*,
            COUNT(
                CASE
                    WHEN paath_signups.status IN ('pending', 'approved')
                    THEN 1
                END
            ) AS booked_count
        FROM paath_slots
        LEFT JOIN paath_signups
            ON paath_signups.paath_id = paath_slots.id
        GROUP BY paath_slots.id
        ORDER BY paath_slots.date ASC, paath_slots.start_time ASC
        """
    ).fetchall()

    requests = db.execute(
        """
        SELECT
            paath_signups.*,
            paath_slots.title,
            paath_slots.date,
            paath_slots.start_time,
            users.name AS student_name,
            users.email AS student_email
        FROM paath_signups
        JOIN paath_slots ON paath_slots.id = paath_signups.paath_id
        JOIN users ON users.id = paath_signups.user_id
        ORDER BY paath_signups.id DESC
        """
    ).fetchall()
    db.close()

    token = h(csrf_token())
    slot_rows = ""
    for slot in slots:
        slot_rows += f"""
        <tr>
            <td>{h(slot["title"])}</td>
            <td>{h(slot["date"])}<br>{h(slot["start_time"])} – {h(slot["end_time"])}</td>
            <td>${float(slot["price"]):,.2f}</td>
            <td>{slot["booked_count"]} / {slot["capacity"]}</td>
            <td>
                <form method="POST" action="/admin/paath/delete/{slot["id"]}">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <button class="danger">Delete</button>
                </form>
            </td>
        </tr>
        """

    request_rows = ""
    for item in requests:
        actions = f'<span class="{ "success" if item["status"] == "approved" else "warning" if item["status"] == "pending" else "muted" }">{h(item["status"].title())}</span>'
        if item["status"] == "pending":
            actions = f"""
            <form method="POST" action="/admin/paath/request/{item["id"]}/approved" style="display:inline">
                <input type="hidden" name="csrf_token" value="{token}">
                <button>Approve</button>
            </form>
            <form method="POST" action="/admin/paath/request/{item["id"]}/rejected" style="display:inline">
                <input type="hidden" name="csrf_token" value="{token}">
                <button class="danger">Reject</button>
            </form>
            """

        request_rows += f"""
        <tr>
            <td>{h(item["student_name"])}<br><span class="small">{h(item["student_email"])}</span></td>
            <td>{h(item["title"])}<br>{h(item["date"])} {h(item["start_time"])}</td>
            <td>${float(item["amount"]):,.2f}</td>
            <td>{actions}</td>
        </tr>
        """

    return layout(
        f"""
        <div class="container">
            <h1>Manage Paath</h1>
            <p class="muted">Create time-based sponsorship slots and review requests.</p>

            <div class="card">
                <h2>Create Paath Slot</h2>
                <form method="POST" action="/admin/paath/create">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <label>Title</label>
                    <input name="title" maxlength="150" placeholder="Morning Paath" required>
                    <label>Description</label>
                    <textarea name="description" maxlength="1000" placeholder="Details about this paath" required></textarea>
                    <div class="grid">
                        <div><label>Date</label><input type="date" name="date" required></div>
                        <div><label>Start Time</label><input type="time" name="start_time" required></div>
                        <div><label>End Time</label><input type="time" name="end_time" required></div>
                    </div>
                    <div class="grid">
                        <div><label>Sponsorship Price ($)</label><input type="number" name="price" min="0" step="0.01" required></div>
                        <div><label>Available Slots</label><input type="number" name="capacity" min="1" max="1000" value="1" required></div>
                    </div>
                    <button>Publish Paath Slot</button>
                </form>
            </div>

            <div class="card">
                <h2>Published Slots</h2>
                <div style="overflow-x:auto"><table>
                    <tr><th>Title</th><th>Schedule</th><th>Price</th><th>Booked</th><th>Action</th></tr>
                    {slot_rows or '<tr><td colspan="5">No paath slots published yet.</td></tr>'}
                </table></div>
            </div>

            <div class="card">
                <h2>Sponsorship Requests</h2>
                <div style="overflow-x:auto"><table>
                    <tr><th>Student</th><th>Paath</th><th>Amount</th><th>Status</th></tr>
                    {request_rows or '<tr><td colspan="4">No sponsorship requests yet.</td></tr>'}
                </table></div>
            </div>
        </div>
        """,
        "Manage Paath"
    )


@app.route("/admin/paath/create", methods=["POST"])
@paath_admin_required
def create_paath():

    validate_csrf()
    user = current_user()
    title = clean(request.form.get("title"), 150)
    description = clean(request.form.get("description"), 1000)
    date = clean(request.form.get("date"), 20)
    start_time = clean(request.form.get("start_time"), 20)
    end_time = clean(request.form.get("end_time"), 20)

    try:
        price = float(request.form.get("price", "-1"))
        capacity = int(request.form.get("capacity", "0"))
        datetime.strptime(date, "%Y-%m-%d")
        start = datetime.strptime(start_time, "%H:%M")
        end = datetime.strptime(end_time, "%H:%M")
    except (TypeError, ValueError):
        abort(400, "Enter a valid date, time, price, and capacity.")

    if not title or not description or end <= start or price < 0 or not 1 <= capacity <= 1000:
        abort(400, "Check the paath slot details and try again.")

    db = get_db()
    db.execute(
        """
        INSERT INTO paath_slots
        (title, description, date, start_time, end_time, price, capacity, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (title, description, date, start_time, end_time, price, capacity, user["id"], utc_now())
    )
    db.commit()
    db.close()

    return redirect(url_for("admin_paath"))


@app.route("/admin/paath/request/<int:request_id>/<status>", methods=["POST"])
@paath_admin_required
def update_paath_request(request_id, status):

    validate_csrf()
    if status not in ("approved", "rejected"):
        abort(400)

    db = get_db()
    db.execute(
        "UPDATE paath_signups SET status = ? WHERE id = ?",
        (status, request_id)
    )
    db.commit()
    db.close()
    return redirect(url_for("admin_paath"))


@app.route("/admin/paath/delete/<int:paath_id>", methods=["POST"])
@paath_admin_required
def delete_paath(paath_id):

    validate_csrf()
    db = get_db()
    db.execute("DELETE FROM paath_slots WHERE id = ?", (paath_id,))
    db.commit()
    db.close()
    return redirect(url_for("admin_paath"))


# ============================================================
# SIGN UP FOR SEVA
# ============================================================

@app.route("/signup/<int:seva_id>", methods=["POST"])
@login_required
def signup(seva_id):

    validate_csrf()

    user = current_user()

    if user["role"] == "admin":

        return redirect(
            url_for("admin")
        )

    db = get_db()

    seva_item = db.execute(
        """
        SELECT id, title, max_volunteers,
               (SELECT COUNT(*) FROM signups s2 WHERE s2.seva_id = seva.id AND s2.status != 'rejected') AS signup_count
        FROM seva
        WHERE id = ?
        """,
        (seva_id,)
    ).fetchone()

    if not seva_item:

        db.close()
        abort(404)

    if seva_item["max_volunteers"] and seva_item["signup_count"] >= seva_item["max_volunteers"]:
        db.close()
        abort(400, "This Seva is full.")

    try:

        db.execute(
            """
            INSERT INTO signups
            (
                user_id,
                seva_id,
                status,
                created_at
            )
            VALUES (?, ?, 'pending', ?)
            """,
            (
                user["id"],
                seva_id,
                utc_now()
            )
        )

        db.commit()
        notify_user(user["id"], "Seva signup confirmed", f"You signed up for {seva_item['title']}.", "/dashboard")

    except IntegrityError:
        pass

    db.close()

    return redirect(
        url_for("seva")
    )


# ============================================================
# STUDENT DASHBOARD
# ============================================================

@app.route("/legacy-pantry", methods=["GET", "POST"])
@login_required
def pantry():

    if request.path == "/legacy-pantry":
        return redirect(url_for("pantry_needs"))

    user = current_user()
    db = get_db()
    token = h(csrf_token())

    if request.method == "POST":
        validate_csrf()
        ingredient = clean(request.form.get("ingredient"), 100)
        unit = clean(request.form.get("unit"), 30)
        expiry_date = clean(request.form.get("expiry_date"), 20)

        try:
            quantity = float(request.form.get("quantity", "0"))
            surplus = float(request.form.get("surplus", "0"))
        except (TypeError, ValueError):
            quantity = -1
            surplus = -1

        if not ingredient or not unit or quantity <= 0 or surplus < 0 or surplus > quantity:
            db.close()
            return layout(
                "<div class='container'><div class='card error'>Ingredient, quantity, and a valid surplus amount are required. Surplus cannot exceed the pantry quantity.</div></div>",
                "Pantry"
            ), 400

        now = utc_now()
        db.execute(
            """
            INSERT INTO pantry_items
            (user_id, ingredient, quantity, unit, expiry_date, surplus, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user["id"], ingredient, quantity, unit, expiry_date, surplus, now, now)
        )
        db.commit()
        db.close()
        return redirect(url_for("pantry"))

    items = db.execute(
        """
        SELECT pantry_items.*, COALESCE(SUM(pantry_donations.quantity), 0) AS donated
        FROM pantry_items
        LEFT JOIN pantry_donations
            ON pantry_donations.pantry_item_id = pantry_items.id
            AND pantry_donations.status != 'cancelled'
        WHERE pantry_items.user_id = ?
        GROUP BY pantry_items.id
        ORDER BY pantry_items.id DESC
        """,
        (user["id"],)
    ).fetchall()

    donations = db.execute(
        """
        SELECT pantry_donations.*, pantry_items.ingredient
        FROM pantry_donations
        JOIN pantry_items ON pantry_items.id = pantry_donations.pantry_item_id
        WHERE pantry_donations.donor_id = ?
        ORDER BY pantry_donations.id DESC
        """,
        (user["id"],)
    ).fetchall()
    db.close()

    item_rows = ""
    for item in items:
        available = max(0, float(item["surplus"] or 0) - float(item["donated"] or 0))
        offer = f"<a class='button' href='/pantry/donate/{item['id']}'>Offer surplus</a>" if available > 0 else "-"
        item_rows += f"""
        <tr>
            <td><strong>{h(item["ingredient"])}</strong></td>
            <td>{float(item["quantity"]):g} {h(item["unit"])}</td>
            <td>{float(item["surplus"]):g} {h(item["unit"])}</td>
            <td>{available:g} {h(item["unit"])}</td>
            <td>{h(item["expiry_date"] or "Not set")}</td>
            <td>{offer}</td>
        </tr>
        """

    if not item_rows:
        item_rows = "<tr><td colspan='6'>Your pantry is empty. Add your first ingredient above.</td></tr>"

    donation_rows = ""
    for donation in donations:
        donation_rows += f"""
        <tr>
            <td>{h(donation["ingredient"])}</td>
            <td>{float(donation["quantity"]):g} {h(donation["unit"])}</td>
            <td>{h(donation["gurdwara"])}</td>
            <td><span class="warning">{h(donation["status"].title())}</span></td>
        </tr>
        """

    return layout(
        f"""
        <div class="container">
            <h1>🥣 Pantry Tracker</h1>
            <p class="muted">Keep a live list of ingredients and offer unused surplus to another gurdwara.</p>

            <div class="card">
                <h2>Add pantry ingredient</h2>
                <form method="POST">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <div class="grid">
                        <label>Ingredient<input name="ingredient" required maxlength="100" placeholder="Rice, lentils, flour..."></label>
                        <label>Total quantity<input name="quantity" type="number" min="0.01" step="0.01" required></label>
                        <label>Unit<input name="unit" required maxlength="30" placeholder="kg, bags, cans..."></label>
                        <label>Use by <span class="small">(optional)</span><input name="expiry_date" type="date"></label>
                        <label>Surplus available for donation<input name="surplus" type="number" min="0" step="0.01" value="0" required></label>
                    </div>
                    <button>Add to pantry</button>
                </form>
            </div>

            <div class="card">
                <h2>My pantry</h2>
                <div style="overflow-x:auto"><table>
                    <tr><th>Ingredient</th><th>Quantity</th><th>Surplus</th><th>Available</th><th>Use by</th><th>Action</th></tr>
                    {item_rows}
                </table></div>
            </div>

            <div class="card">
                <h2>Donation offers</h2>
                <div style="overflow-x:auto"><table>
                    <tr><th>Ingredient</th><th>Quantity</th><th>Gurdwara</th><th>Status</th></tr>
                    {donation_rows or "<tr><td colspan='4'>No donation offers yet.</td></tr>"}
                </table></div>
            </div>
        </div>
        """,
        "Pantry Tracker"
    )


@app.route("/pantry/donate/<int:item_id>", methods=["GET", "POST"])
@login_required
def donate_pantry_item(item_id):

    user = current_user()
    db = get_db()
    item = db.execute(
        "SELECT * FROM pantry_items WHERE id = ? AND user_id = ?",
        (item_id, user["id"])
    ).fetchone()

    if not item:
        db.close()
        abort(404)

    donated = db.execute(
        """
        SELECT COALESCE(SUM(quantity), 0) AS total
        FROM pantry_donations
        WHERE pantry_item_id = ? AND status != 'cancelled'
        """,
        (item_id,)
    ).fetchone()["total"]
    available = max(0, float(item["surplus"] or 0) - float(donated or 0))

    if request.method == "POST":
        validate_csrf()
        gurdwara = clean(request.form.get("gurdwara"), 150)
        try:
            quantity = float(request.form.get("quantity", "0"))
        except (TypeError, ValueError):
            quantity = 0

        if not gurdwara or quantity <= 0 or quantity > available:
            db.close()
            return layout(
                "<div class='container'><div class='card error'>Enter a gurdwara and a quantity within the available surplus.</div></div>",
                "Offer Surplus"
            ), 400

        db.execute(
            """
            INSERT INTO pantry_donations
            (pantry_item_id, donor_id, gurdwara, quantity, unit, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, user["id"], gurdwara, quantity, item["unit"], utc_now())
        )
        db.commit()
        db.close()
        return redirect(url_for("pantry"))

    token = h(csrf_token())
    db.close()
    return layout(
        f"""
        <div class="container">
            <div class="card">
                <h1>Offer surplus</h1>
                <p class="muted"><strong>{h(item["ingredient"])}</strong>: {available:g} {h(item["unit"])} available to donate.</p>
                <form method="POST">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <label>Receiving gurdwara<input name="gurdwara" required maxlength="150" placeholder="Gurdwara name and city"></label>
                    <label>Donation quantity<input name="quantity" type="number" min="0.01" max="{available}" step="0.01" required></label>
                    <button>Submit donation offer</button>
                    <a class="button dark-button" href="/pantry">Cancel</a>
                </form>
            </div>
        </div>
        """,
        "Offer Surplus"
    )

@app.route("/pantry")
@login_required
def pantry_needs():

    user = current_user()
    if user["role"] == "admin":
        return redirect(url_for("admin_pantry"))

    db = get_db()
    needs = db.execute(
        """
        SELECT pantry_needs.*,
               COALESCE(SUM(pantry_need_signups.quantity), 0) AS committed,
               own_signup.quantity AS own_quantity
        FROM pantry_needs
        LEFT JOIN pantry_need_signups
            ON pantry_need_signups.need_id = pantry_needs.id
            AND pantry_need_signups.status = 'committed'
        LEFT JOIN pantry_need_signups AS own_signup
            ON own_signup.need_id = pantry_needs.id
            AND own_signup.user_id = ?
        WHERE pantry_needs.status = 'published'
        GROUP BY pantry_needs.id, own_signup.quantity
        ORDER BY pantry_needs.needed_by ASC, pantry_needs.id DESC
        """,
        (user["id"],)
    ).fetchall()
    db.close()

    token = h(csrf_token())
    cards = ""

    for need in needs:
        needed = float(need["quantity_needed"])
        committed = float(need["committed"] or 0)
        remaining = max(0, needed - committed)
        progress_percent = min(100, round((committed / needed) * 100, 1)) if needed > 0 else 0
        unit = canonical_unit(need["unit"])
        display_unit = "lb" if unit == "lb" else ("kg" if unit == "kg" else need["unit"])

        if need["own_quantity"] is not None:
            action = f'<span class="success">✓ Signed up to bring {format_quantity(need["own_quantity"])} {h(display_unit)}</span>'
        elif remaining <= 0:
            action = '<span class="muted">This need is fully covered</span>'
        else:
            if is_mass_unit(unit):
                other_unit = "kg" if unit == "lb" else "lb"
                unit_input = f"""
                <label>Unit
                    <select name="unit">
                        <option value="{h(unit)}">{h(unit)}</option>
                        <option value="{other_unit}">{other_unit}</option>
                    </select>
                </label>
                <p class="small">kg and lb are converted automatically.</p>
                """
            else:
                unit_input = f"""
                <input type="hidden" name="unit" value="{h(unit)}">
                <p class="small">Unit: {h(unit)}</p>
                """

            action = f"""
            <form method="POST" action="/pantry/signup/{need["id"]}">
                <input type="hidden" name="csrf_token" value="{token}">
                <label>Quantity to bring
                    <input name="quantity" type="number" min="0.01" step="0.01" required>
                </label>
                {unit_input}
                <button>Sign up to bring this</button>
            </form>
            """

        cards += f"""
        <div class="card">
            <h2>{h(need["ingredient"])}</h2>
            <p class="muted">{h(need["details"])}</p>
            <p><strong>{format_quantity(remaining)} {h(display_unit)} still needed</strong></p>
            <div class="pantry-progress" aria-label="{format_quantity(committed)} of {format_quantity(needed)} {h(display_unit)} covered">
                <div class="progress-label"><span>{format_quantity(committed)} / {format_quantity(needed)} {h(display_unit)} covered</span><span>{progress_percent}%</span></div>
                <div class="pantry-progress-track" role="progressbar" aria-valuenow="{progress_percent}" aria-valuemin="0" aria-valuemax="100">
                    <div class="pantry-progress-fill" style="width:{progress_percent}%"></div>
                </div>
            </div>
            <p class="small">Bring by: {h(need["needed_by"] or "As soon as possible")}</p>
            {action}
        </div>
        """

    return layout(
        f"""
        <div class="container">
            <h1>🥣 Food Ingredients Needed</h1>
            <p class="muted">Choose an ingredient published by the administrator and sign up to bring it.</p>
            <div class="grid">{cards or '<div class="card"><h2>No ingredient requests yet</h2><p class="muted">The administrator has not published any food ingredient needs.</p></div>'}</div>
        </div>
        """,
        "Food Ingredients Needed"
    )


@app.route("/pantry/signup/<int:need_id>", methods=["POST"])
@login_required
def pantry_signup(need_id):

    validate_csrf()
    user = current_user()
    if user["role"] == "admin":
        return redirect(url_for("admin_pantry"))

    db = get_db()
    need = db.execute(
        "SELECT * FROM pantry_needs WHERE id = ? AND status = 'published'",
        (need_id,)
    ).fetchone()
    if not need:
        db.close()
        abort(404)

    committed = db.execute(
        """
        SELECT COALESCE(SUM(quantity), 0) AS total
        FROM pantry_need_signups
        WHERE need_id = ? AND status = 'committed'
        """,
        (need_id,)
    ).fetchone()["total"]

    remaining = max(0, float(need["quantity_needed"]) - float(committed or 0))

    try:
        quantity = float(request.form.get("quantity", "0"))
    except (TypeError, ValueError):
        quantity = 0

    submitted_unit = canonical_unit(request.form.get("unit", ""))
    requested_unit = canonical_unit(need["unit"])

    if quantity <= 0:
        db.close()
        abort(400, "Enter a positive quantity.")

    converted_quantity = convert_quantity(quantity, submitted_unit, requested_unit)

    if converted_quantity is None:
        db.close()
        abort(400, "The selected unit does not match this ingredient. Use the requested unit or kg/lb for weight-based needs.")

    if converted_quantity > remaining + 1e-9:
        db.close()
        abort(400, "That amount is greater than the remaining need.")

    try:
        db.execute(
            """
            INSERT INTO pantry_need_signups
            (need_id, user_id, quantity, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (need_id, user["id"], converted_quantity, utc_now())
        )
        db.commit()
    except IntegrityError:
        db.close()
        abort(400, "You have already signed up for this ingredient.")
    db.close()
    return redirect(url_for("pantry_needs"))


@app.route("/admin/pantry")
@pantry_admin_required
def admin_pantry():

    db = get_db()
    needs = db.execute(
        """
        SELECT pantry_needs.*,
               COALESCE(SUM(pantry_need_signups.quantity), 0) AS committed
        FROM pantry_needs
        LEFT JOIN pantry_need_signups
            ON pantry_need_signups.need_id = pantry_needs.id
            AND pantry_need_signups.status = 'committed'
        GROUP BY pantry_needs.id
        ORDER BY pantry_needs.status, pantry_needs.needed_by ASC, pantry_needs.id DESC
        """
    ).fetchall()
    signups = db.execute(
        """
        SELECT pantry_need_signups.*, pantry_needs.ingredient, pantry_needs.unit,
               users.name AS student_name, users.email AS student_email
        FROM pantry_need_signups
        JOIN pantry_needs ON pantry_needs.id = pantry_need_signups.need_id
        JOIN users ON users.id = pantry_need_signups.user_id
        ORDER BY pantry_need_signups.id DESC
        """
    ).fetchall()
    db.close()

    token = h(csrf_token())
    need_rows = ""
    for need in needs:
        needed = float(need["quantity_needed"])
        committed = float(need["committed"] or 0)
        remaining = max(0, needed - committed)
        display_unit = canonical_unit(need["unit"])
        action = f"<form method='POST' action='/admin/pantry/archive/{need['id']}'><input type='hidden' name='csrf_token' value='{token}'><button class='danger'>Archive</button></form>" if need["status"] == "published" else "Archived"
        need_rows += f"""
        <tr><td>{h(need["ingredient"])}</td><td><strong>{format_quantity(remaining)} {h(display_unit)} still needed</strong></td><td>{h(need["needed_by"] or "-")}</td><td>{h(need["status"].title())}</td><td>{action}</td></tr>
        """

    signup_rows = "".join(
        f"<tr><td>{h(item['student_name'])}<br><span class='small'>{h(item['student_email'])}</span></td><td>{h(item['ingredient'])}</td><td>{format_quantity(item['quantity'])} {h(canonical_unit(item['unit']))}</td><td>{h(item['status'].title())}</td></tr>"
        for item in signups
    )

    return layout(
        f"""
        <div class="container">
            <h1>Manage Food Ingredient Needs</h1>
            <p class="muted">Publish the ingredients students should bring and review their commitments.</p>
            <div class="card">
                <h2>Publish an ingredient need</h2>
                <form method="POST" action="/admin/pantry/create">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <div class="grid">
                        <label>Ingredient<input name="ingredient" maxlength="100" placeholder="Rice, lentils, flour..." required></label>
                        <label>Quantity needed<input name="quantity_needed" type="number" min="0.01" step="0.01" required></label>
                        <label>Unit
                            <select name="unit" required>
                                <option value="lb">lb</option>
                                <option value="kg">kg</option>
                                <option value="bags">bags</option>
                                <option value="cans">cans</option>
                                <option value="boxes">boxes</option>
                                <option value="items">items</option>
                            </select>
                        </label>
                        <label>Bring by <input name="needed_by" type="date"></label>
                    </div>
                    <p class="small">For weight-based ingredients, choose kg or lb. Smart Seva converts kg and lb automatically.</p>
                    <label>Details<textarea name="details" maxlength="1000" placeholder="Optional notes for students"></textarea></label>
                    <button>Publish ingredient need</button>
                </form>
            </div>
            <div class="card"><h2>Published needs</h2><div style="overflow-x:auto"><table><tr><th>Ingredient</th><th>Remaining</th><th>Bring by</th><th>Status</th><th>Action</th></tr>{need_rows or '<tr><td colspan="5">No ingredient needs published yet.</td></tr>'}</table></div></div>
            <div class="card"><h2>Student signups</h2><div style="overflow-x:auto"><table><tr><th>Student</th><th>Ingredient</th><th>Quantity</th><th>Status</th></tr>{signup_rows or '<tr><td colspan="4">No student signups yet.</td></tr>'}</table></div></div>
        </div>
        """,
        "Manage Pantry Needs"
    )


@app.route("/admin/pantry/create", methods=["POST"])
@pantry_admin_required
def create_pantry_need():

    validate_csrf()
    user = current_user()
    ingredient = clean(request.form.get("ingredient"), 100)
    unit = canonical_unit(request.form.get("unit", ""))
    details = clean(request.form.get("details"), 1000)
    needed_by = clean(request.form.get("needed_by"), 20)
    try:
        quantity_needed = float(request.form.get("quantity_needed", "0"))
    except (TypeError, ValueError):
        quantity_needed = 0

    if not ingredient or not unit or quantity_needed <= 0:
        abort(400, "Enter an ingredient, unit, and positive quantity.")

    # Keep all new weight-based needs in pounds internally.
    if unit == "kg":
        quantity_needed = convert_mass(quantity_needed, "kg", "lb")
        unit = "lb"

    db = get_db()
    db.execute(
        """
        INSERT INTO pantry_needs
        (ingredient, quantity_needed, unit, details, needed_by, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ingredient, quantity_needed, unit, details, needed_by, user["id"], utc_now())
    )
    db.commit()
    db.close()
    return redirect(url_for("admin_pantry"))


@app.route("/admin/pantry/archive/<int:need_id>", methods=["POST"])
@pantry_admin_required
def archive_pantry_need(need_id):

    validate_csrf()
    db = get_db()
    db.execute("UPDATE pantry_needs SET status = 'archived' WHERE id = ?", (need_id,))
    db.commit()
    db.close()
    return redirect(url_for("admin_pantry"))


# ============================================================
# PERSONAL SEVA CALENDAR
# ============================================================

@app.route("/my-calendar")
@login_required
def my_calendar():
    """Show only the signed-in user's non-rejected Seva signups."""
    user = current_user()
    db = get_db()
    events = db.execute(
        """
        SELECT signups.id AS signup_id, signups.status,
               seva.id AS seva_id, seva.title, seva.date,
               seva.start_time, seva.end_time, seva.location
        FROM signups
        JOIN seva ON seva.id = signups.seva_id
        WHERE signups.user_id = ? AND signups.status != 'rejected'
        ORDER BY seva.date ASC, seva.start_time ASC
        """,
        (user["id"],)
    ).fetchall()
    db.close()

    calendar_data = json.dumps([
        {
            "id": int(item["signup_id"]),
            "title": str(item["title"]),
            "date": str(item["date"]),
            "start_time": str(item["start_time"])[:5],
            "end_time": str(item["end_time"])[:5],
            "location": str(item["location"]),
            "status": str(item["status"])
        }
        for item in events
    ], ensure_ascii=False)

    signup_rows = ""
    for item in events:
        signup_rows += f"<tr><td>{h(item['title'])}</td><td>{h(item['date'])}</td><td>{h(format_time_label(item['start_time']))} – {h(format_time_label(item['end_time']))}</td><td>{h(item['location'])}</td><td>{h(str(item['status']).title())}</td></tr>"
    if not signup_rows:
        signup_rows = '<tr><td colspan="5">You have not signed up for any Sevas yet. Browse <a href="/seva">Seva opportunities</a> to get started.</td></tr>'

    return layout(
        f"""
        <div class="container">
            <h1>🗓️ My Seva Calendar</h1>
            <p class="muted">A personal schedule of Seva opportunities you have signed up for. Rejected signups are not shown.</p>
            <div class="card">
                <div class="calendar-toolbar">
                    <button type="button" class="dark-button" onclick="moveMyCalendar(-1)">← Previous</button>
                    <h2 id="my-calendar-title" style="margin:0"></h2>
                    <button type="button" class="dark-button" onclick="moveMyCalendar(1)">Next →</button>
                </div>
                <div class="calendar" id="my-calendar-grid"></div>
                <p class="small muted">Pending signups are awaiting approval. Approved signups are confirmed.</p>
            </div>
            <div class="card">
                <h2>Upcoming and Past Signups</h2>
                <div style="overflow-x:auto">
                    <table>
                        <tr><th>Seva</th><th>Date</th><th>Time</th><th>Location</th><th>Status</th></tr>
                        {signup_rows}
                    </table>
                </div>
            </div>
        </div>
        <script>
        const myCalendarEvents = {calendar_data};
        let myCalendarCursor = new Date();
        myCalendarCursor.setDate(1);
        function renderMyCalendar() {{
            const year = myCalendarCursor.getFullYear();
            const month = myCalendarCursor.getMonth();
            document.getElementById("my-calendar-title").textContent =
                myCalendarCursor.toLocaleDateString(undefined, {{month:"long", year:"numeric"}});
            const grid = document.getElementById("my-calendar-grid");
            grid.innerHTML = "";
            ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"].forEach(function(day) {{
                const header = document.createElement("div");
                header.className = "calendar-head";
                header.textContent = day;
                grid.appendChild(header);
            }});
            const firstDay = new Date(year, month, 1).getDay();
            const days = new Date(year, month + 1, 0).getDate();
            for (let blank = 0; blank < firstDay; blank++) {{
                const cell = document.createElement("div");
                cell.className = "calendar-day empty";
                grid.appendChild(cell);
            }}
            for (let day = 1; day <= days; day++) {{
                const dateKey = year + "-" + String(month + 1).padStart(2,"0") + "-" + String(day).padStart(2,"0");
                const dayEvents = myCalendarEvents.filter(function(item) {{ return item.date.slice(0,10) === dateKey; }});
                const cell = document.createElement("div");
                cell.className = "calendar-day";
                const number = document.createElement("div");
                number.className = "calendar-num";
                number.textContent = day;
                cell.appendChild(number);
                dayEvents.forEach(function(item) {{
                    const event = document.createElement("div");
                    event.className = "calendar-event";
                    event.textContent = item.start_time + " " + item.title + " (" + item.status + ")";
                    event.title = item.title + " • " + item.location;
                    cell.appendChild(event);
                }});
                grid.appendChild(cell);
            }}
        }}
        function moveMyCalendar(delta) {{
            myCalendarCursor.setMonth(myCalendarCursor.getMonth() + delta);
            renderMyCalendar();
        }}
        renderMyCalendar();
        </script>
        """,
        "My Seva Calendar"
    )


# ============================================================
# COMMUNITY IMPACT DASHBOARD
# ============================================================

@app.route("/impact")
@login_required
def community_impact():
    """Show aggregate community metrics without exposing individual records."""
    db = get_db()
    try:
        service_stats = db.execute(
            """
            SELECT
                COALESCE(SUM(signups.hours), 0) AS approved_hours,
                COUNT(*) AS completed_sevas,
                COUNT(DISTINCT signups.user_id) AS volunteers
            FROM signups
            WHERE signups.status = 'approved'
            """
        ).fetchone()
        opportunity_stats = db.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE CAST(date AS date) >= CURRENT_DATE) AS upcoming
            FROM seva
            """
        ).fetchone()
        pantry_stats = db.execute(
            """
            SELECT COUNT(*) AS donation_offers,
                   COUNT(DISTINCT donor_id) AS donors
            FROM pantry_donations
            """
        ).fetchone()
        monthly = db.execute(
            """
            SELECT to_char(date_trunc('month', CAST(seva.date AS date)), 'Mon YYYY') AS month,
                   COALESCE(SUM(signups.hours), 0) AS hours,
                   COUNT(*) AS completed
            FROM signups
            JOIN seva ON seva.id = signups.seva_id
            WHERE signups.status = 'approved'
              AND CAST(seva.date AS date) >= date_trunc('month', CURRENT_DATE) - INTERVAL '5 months'
              AND CAST(seva.date AS date) < date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
            GROUP BY date_trunc('month', CAST(seva.date AS date))
            ORDER BY date_trunc('month', CAST(seva.date AS date))
            """
        ).fetchall()
    finally:
        db.close()

    approved_hours = float(service_stats["approved_hours"] or 0)
    completed_sevas = int(service_stats["completed_sevas"] or 0)
    volunteers = int(service_stats["volunteers"] or 0)
    donation_offers = int(pantry_stats["donation_offers"] or 0)
    donors = int(pantry_stats["donors"] or 0)
    max_hours = max([float(item["hours"] or 0) for item in monthly] + [1.0])

    month_rows = ""
    for item in monthly:
        hours = float(item["hours"] or 0)
        width = min(100, round(hours / max_hours * 100))
        month_rows += f"""
        <div class="impact-month">
            <div class="progress-label"><span>{h(item["month"])}</span><strong>{round(hours, 2)} hours</strong></div>
            <div class="progress"><div class="progress-bar" style="width:{width}%"></div></div>
            <p class="small muted">{int(item["completed"] or 0)} approved Sevas</p>
        </div>
        """
    if not month_rows:
        month_rows = '<p class="muted">Monthly participation will appear here after the first Seva hours are approved.</p>'

    return layout(
        f"""
        <div class="container">
            <h1>🌍 Community Impact</h1>
            <p class="muted">A privacy-friendly snapshot of what our community has accomplished together. Only aggregate totals are shown.</p>
            <div class="grid">
                <div class="card"><div class="small">VERIFIED SERVICE HOURS</div><div class="stat">{round(approved_hours, 2)}</div></div>
                <div class="card"><div class="small">APPROVED SEVA SIGNUPS</div><div class="stat">{completed_sevas}</div></div>
                <div class="card"><div class="small">VOLUNTEERS WHO CONTRIBUTED</div><div class="stat">{volunteers}</div></div>
                <div class="card"><div class="small">UPCOMING SEVA OPPORTUNITIES</div><div class="stat">{int(opportunity_stats["upcoming"] or 0)}</div></div>
                <div class="card"><div class="small">PANTRY DONATION OFFERS</div><div class="stat">{donation_offers}</div><p class="small muted">From {donors} contributing donor(s)</p></div>
                <div class="card"><div class="small">TOTAL SEVA OPPORTUNITIES</div><div class="stat">{int(opportunity_stats["total"] or 0)}</div></div>
            </div>
            <div class="card">
                <h2>📈 Service Over the Last Six Months</h2>
                <p class="muted">Bars compare approved hours month by month. Only verified hours are counted.</p>
                {month_rows}
            </div>
            <div class="card">
                <h2>🤝 Keep the Impact Growing</h2>
                <p>Every hour of service and every pantry contribution helps strengthen the community.</p>
                <a class="button" href="/seva">Find a Seva</a>
                <a class="button dark-button" href="/pantry">Visit Pantry</a>
            </div>
        </div>
        """,
        "Community Impact"
    )


@app.route("/dashboard")
@login_required
def dashboard():

    user = current_user()

    if user["role"] == "admin":

        return redirect(
            url_for("admin")
        )

    db = get_db()

    total_row = db.execute(
        """
        SELECT COALESCE(SUM(hours), 0) AS total

        FROM signups

        WHERE user_id = ?

        AND status = 'approved'
        """,
        (user["id"],)
    ).fetchone()

    total = float(
        total_row["total"] or 0
    )

    signups = db.execute(
        """
        SELECT
            signups.*,

            seva.title,
            seva.description,
            seva.location,
            seva.date,
            seva.start_time,
            seva.end_time

        FROM signups

        JOIN seva
            ON signups.seva_id = seva.id

        WHERE signups.user_id = ?

        ORDER BY signups.id DESC
        """,
        (user["id"],)
    ).fetchall()

    completed_count_row = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM signups
        WHERE user_id = ? AND status = 'approved'
        """,
        (user["id"],)
    ).fetchone()
    completed_count = int(completed_count_row["count"] or 0)

    monthly_row = db.execute(
        """
        SELECT COALESCE(SUM(signups.hours), 0) AS hours
        FROM signups
        JOIN seva ON seva.id = signups.seva_id
        WHERE signups.user_id = ?
          AND signups.status = 'approved'
          AND CAST(seva.date AS date) >= date_trunc('month', CURRENT_DATE)
          AND CAST(seva.date AS date) < date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
        """,
        (user["id"],)
    ).fetchone()
    monthly_hours = float(monthly_row["hours"] or 0)

    next_seva = db.execute(
        """
        SELECT signups.id, signups.status, seva.title, seva.date, seva.start_time,
               seva.end_time, seva.location, signups.check_in, signups.check_out
        FROM signups
        JOIN seva ON seva.id = signups.seva_id
        WHERE signups.user_id = ?
          AND signups.status = 'pending'
          AND CAST(seva.date AS date) >= CURRENT_DATE
        ORDER BY seva.date ASC, seva.start_time ASC
        LIMIT 1
        """,
        (user["id"],)
    ).fetchone()

    badges = milestone_badges(total, completed_count)
    badges_html = " ".join(f"<span class='button' style='display:inline-block;margin:4px'>{h(b)}</span>" for b in badges) or "<span class='muted'>Complete your first Seva to earn a milestone.</span>"

    certificate = db.execute(
        """
        SELECT *
        FROM certificates

        WHERE user_id = ?

        ORDER BY id DESC

        LIMIT 1
        """,
        (user["id"],)
    ).fetchone()

    db.close()

    goal = float(
        user["goal_hours"] or 40
    )

    percentage = min(
        100,
        round(
            total / goal * 100,
            1
        )
    )

    monthly_goal = max(1, min(goal, 10))
    monthly_percentage = min(100, round(monthly_hours / monthly_goal * 100, 1))

    next_seva_html = ""
    if next_seva:
        checkin_active = "active" if next_seva["check_in"] else ""
        checkout_active = "active" if next_seva["check_out"] else ""
        next_seva_html = f"""
        <div class="card">
            <h2>🚀 Your Next Seva</h2>
            <div class="flow-card">
                <h3>{h(next_seva["title"])}</h3>
                <p>📅 {h(next_seva["date"])} • ⏰ {h(format_time_label(next_seva["start_time"]))} – {h(format_time_label(next_seva["end_time"]))}</p>
                <p>📍 {h(next_seva["location"])}</p>
                <div class="status-flow">
                    <div class="status-step active">1. Sign Up</div>
                    <div class="status-step {checkin_active}">2. Check In</div>
                    <div class="status-step {checkout_active}">3. Check Out</div>
                    <div class="status-step">4. Await Approval</div>
                    <div class="status-step">5. Verified</div>
                </div>
            </div>
        </div>
        """

    certificate_button = ""

    if total >= goal:

        if certificate:

            certificate_button = f"""
            <a
                class="button"
                href="/certificate/{h(certificate["verification_code"])}"
            >
                🏆 View Certificate
            </a>
            """

        else:

            token = h(csrf_token())

            certificate_button = f"""
            <form
                method="POST"
                action="/generate-certificate"
            >

                <input
                    type="hidden"
                    name="csrf_token"
                    value="{token}"
                >

                <button>
                    🏆 Generate Certificate
                </button>

            </form>
            """

    else:

        remaining = round(
            goal - total,
            2
        )

        certificate_button = f"""
        <p class="muted">
            You need
            <strong>{remaining}</strong>
            more approved hours to reach your goal.
        </p>
        """

    rows = ""

    token = h(csrf_token())

    for item in signups:

        status = item["status"]

        if status == "approved":

            status_html = """
            <span class="success">
                ✓ Approved
            </span>
            """

        elif status == "rejected":

            status_html = """
            <span style="color:#ff7777">
                ✕ Rejected
            </span>
            """

        else:

            status_html = """
            <span class="warning">
                ⏳ Pending
            </span>
            """

        action = ""

        if status == "pending":

            if not item["check_in"]:

                action = f"""
                <form
                    method="POST"
                    action="/checkin/{item["id"]}"
                >

                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{token}"
                    >

                    <button>
                        Check In
                    </button>

                </form>
                """

            elif not item["check_out"]:

                if seva_end_passed(item["date"], item["end_time"]):
                    action = f"""
                    <span class="success">
                        ✓ Checked In
                    </span>
                    <br><br>
                    <form
                    method="POST"
                    action="/checkout/{item["id"]}"
                >

                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{token}"
                    >

                    <button>
                        Check Out
                    </button>

                    </form>
                    """
                else:
                    action = f"""
                    <span class="success">✓ Checked In</span><br><br>
                    <span class="warning">⏳ Check Out opens after {h(format_time_label(item["end_time"]))}</span>
                    """

            else:

                action = """
                <span class="warning">
                    Waiting for admin verification
                </span>
                """

        rows += f"""
        <tr data-status="{h(status)}">

            <td>{h(item["title"])}</td>

            <td>{h(item["date"])}</td>

            <td>{h(item["location"])}</td>

            <td>
                {round(float(item["hours"] or 0), 2)}
            </td>

            <td>
                {status_html}
            </td>

            <td>
                {action}
            </td>

        </tr>
        """

    if not rows:

        rows = """
        <tr>
            <td colspan="6">
                No seva yet.
            </td>
        </tr>
        """

    return layout(
        f"""
        <div class="container">

            <h1>
                Welcome, {h(user["name"])} 👋
            </h1>

            <p class="muted">
                Your Smart Seva dashboard.
            </p>

            <div class="grid">

                <div class="card">
                    <div class="small">
                        APPROVED HOURS
                    </div>

                    <div class="stat">
                        {round(total, 2)}
                    </div>
                </div>

                <div class="card">
                    <div class="small">
                        GOAL
                    </div>

                    <div class="stat">
                        {round(goal, 2)}
                    </div>
                </div>

                <div class="card">
                    <div class="small">
                        PROGRESS
                    </div>

                    <div class="stat">
                        {percentage}%
                    </div>
                </div>

            </div>

            <div class="card">
                <h2>🗓️ Plan Your Seva</h2>
                <p class="muted">See only the Sevas you have signed up for in a personal calendar.</p>
                <a class="button" href="/my-calendar">Open My Seva Calendar</a>
            </div>

            <div class="card">
                <h2>🌍 Community Impact</h2>
                <p class="muted">See the community's verified service hours, volunteer participation, and pantry donation activity.</p>
                <a class="button" href="/impact">Explore Community Impact</a>
            </div>

            <div class="card">
                <h2>🏅 Milestones</h2>
                {badges_html}
            </div>

            <div class="card">
                <h2>🎯 Seva Goal</h2>
                <div class="progress-label"><span>Overall goal</span><strong>{percentage}%</strong></div>
                <div class="progress"><div class="progress-bar" style="width:{percentage}%"></div></div>
                <p>{round(total,2)} / {round(goal,2)} verified hours</p>
                {certificate_button}
                <div class="flow-card">
                    <div class="progress-label"><span>Monthly momentum</span><strong>{monthly_percentage}%</strong></div>
                    <div class="progress"><div class="progress-bar" style="width:{monthly_percentage}%"></div></div>
                    <p class="small">{round(monthly_hours,2)} approved hours this month • target {round(monthly_goal,2)} hours</p>
                </div>
            </div>

            {next_seva_html}

            <div class="card">
                <h2>📊 My Seva Progress</h2>
                <div class="filter-summary">
                    <span class="filter-chip">Completed: {completed_count}</span>
                    <span class="filter-chip">Verified: {round(total,2)} hours</span>
                    <span class="filter-chip">Goal: {percentage}%</span>
                </div>
            </div>

            <div class="card">

                <h2>
                    📋 My Seva
                </h2>

                <div class="filter-actions" style="margin-bottom:14px">
                    <button type="button" class="dark-button" onclick="filterMySevas('all')">All</button>
                    <button type="button" class="dark-button" onclick="filterMySevas('pending')">Pending</button>
                    <button type="button" class="dark-button" onclick="filterMySevas('approved')">Approved</button>
                    <button type="button" class="dark-button" onclick="filterMySevas('rejected')">Rejected</button>
                </div>

                <div style="overflow-x:auto">

                    <table>

                        <tr>
                            <th>Seva</th>
                            <th>Date</th>
                            <th>Location</th>
                            <th>Hours</th>
                            <th>Status</th>
                            <th>Action</th>
                        </tr>

                        {rows}

                    </table>

                </div>
                <script>
                function filterMySevas(status) {{
                    document.querySelectorAll("tr[data-status]").forEach(function(row) {{
                        row.style.display = (status === "all" || row.dataset.status === status) ? "" : "none";
                    }});
                }}
                </script>

            </div>

        </div>
        """,
        "Dashboard"
    )


# ============================================================
# CHECK IN
# ============================================================

@app.route("/checkin/<int:signup_id>", methods=["POST"])
@login_required
def checkin(signup_id):

    validate_csrf()

    user = current_user()

    db = get_db()

    signup = db.execute(
        """
        SELECT *
        FROM signups

        WHERE id = ?
        AND user_id = ?
        """,
        (
            signup_id,
            user["id"]
        )
    ).fetchone()

    if not signup:

        db.close()

        abort(404)

    if signup["check_in"]:

        db.close()

        return redirect(
            url_for("dashboard")
        )

    db.execute(
        """
        UPDATE signups

        SET check_in = ?

        WHERE id = ?
        """,
        (
            utc_now(),
            signup_id
        )
    )

    db.commit()
    db.close()

    return redirect(
        url_for("dashboard")
    )


# # ============================================================
# CHECK OUT
# ============================================================

@app.route("/checkout/<int:signup_id>", methods=["POST"])
@login_required
def checkout(signup_id):

    validate_csrf()

    user = current_user()

    if user["role"] == "admin":
        return redirect(url_for("admin"))

    db = get_db()

    signup = db.execute(
        """
        SELECT
            signups.*,
            seva.date,
            seva.start_time,
            seva.end_time
        FROM signups
        JOIN seva
            ON signups.seva_id = seva.id
        WHERE signups.id = ?
        AND signups.user_id = ?
        """,
        (signup_id, user["id"])
    ).fetchone()

    if not signup:
        db.close()
        abort(404)

    if not signup["check_in"]:
        db.close()
        return redirect(url_for("dashboard"))

    if signup["check_out"]:
        db.close()
        return redirect(url_for("dashboard"))

    if not seva_end_passed(signup.get("date") if "date" in signup else None, signup["end_time"]):
        db.close()
        return redirect(url_for("dashboard"))

    check_in = as_datetime(
        signup["check_in"]
    )

    check_out = datetime.now(timezone.utc)

    elapsed_seconds = (
        check_out - check_in
    ).total_seconds()

    hours = max(
        0,
        round(elapsed_seconds / 3600, 2)
    )

    # Prevent accidental/unreasonable hour submissions.
    hours = min(hours, 24)

    db.execute(
        """
        UPDATE signups

        SET
            check_out = ?,
            hours = ?

        WHERE id = ?
        """,
        (
            check_out.isoformat(),
            hours,
            signup_id
        )
    )

    db.commit()
    db.close()

    return redirect(
        url_for("dashboard")
    )


# ============================================================
# GENERATE CERTIFICATE
# ============================================================

@app.route(
    "/generate-certificate",
    methods=["POST"]
)
@login_required
def generate_certificate():

    validate_csrf()

    user = current_user()

    if user["role"] == "admin":
        return redirect(url_for("admin"))

    db = get_db()

    total_row = db.execute(
        """
        SELECT COALESCE(SUM(hours), 0) AS total

        FROM signups

        WHERE user_id = ?

        AND status = 'approved'
        """,
        (user["id"],)
    ).fetchone()

    total = float(
        total_row["total"] or 0
    )

    goal = float(
        user["goal_hours"] or 40
    )

    if total < goal:

        db.close()

        abort(
            400,
            "You have not reached your seva goal yet."
        )

    existing = db.execute(
        """
        SELECT *
        FROM certificates

        WHERE user_id = ?

        ORDER BY id DESC

        LIMIT 1
        """,
        (user["id"],)
    ).fetchone()

    if existing:

        db.close()

        return redirect(
            url_for(
                "certificate",
                verification_code=existing[
                    "verification_code"
                ]
            )
        )

    verification_code = (
        "SS-"
        + secrets.token_hex(6).upper()
    )

    db.execute(
        """
        INSERT INTO certificates
        (
            user_id,
            verification_code,
            issued_at,
            hours
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            user["id"],
            verification_code,
            utc_now(),
            total
        )
    )

    db.commit()
    db.close()

    return redirect(
        url_for(
            "certificate",
            verification_code=verification_code
        )
    )


# ============================================================
# CERTIFICATE PDF
# ============================================================

@app.route(
    "/certificate/<verification_code>"
)
@login_required
def certificate(verification_code):

    user = current_user()

    db = get_db()

    certificate_record = db.execute(
        """
        SELECT
            certificates.*,
            users.name,
            users.email

        FROM certificates

        JOIN users
            ON certificates.user_id = users.id

        WHERE certificates.verification_code = ?
        """,
        (verification_code,)
    ).fetchone()

    db.close()

    if not certificate_record:
        abort(404)

    # Students can only see their own certificate.
    if (
        user["role"] != "admin"
        and certificate_record["user_id"] != user["id"]
    ):
        abort(403)

    issued_at = certificate_record["issued_at"]

    try:
        issued_date = as_datetime(
            issued_at
        ).strftime("%B %d, %Y")
    except ValueError:
        issued_date = issued_at

    return layout(
        f"""
        <div class="container">

            <div class="card center">

                <div class="small">
                    SMART SEVA
                </div>

                <h1>
                    🏆 Certificate of Seva
                </h1>

                <p class="muted">
                    This certificate recognizes the
                    community service contribution of
                </p>

                <h2 style="font-size:32px;color:white">
                    {h(certificate_record["name"])}
                </h2>

                <p class="muted">
                    for completing
                </p>

                <div class="stat">
                    {round(float(certificate_record["hours"]), 2)}
                    Hours
                </div>

                <p class="muted">
                    of verified community seva.
                </p>

                <hr
                    style="
                        border:0;
                        border-top:
                        1px solid
                        rgba(255,255,255,.1);
                        margin:30px 0;
                    "
                >

                <p>
                    <strong>
                        Issued:
                    </strong>
                    {h(issued_date)}
                </p>

                <p>
                    <strong>
                        Verification Code:
                    </strong>

                    <code>
                        {h(verification_code)}
                    </code>
                </p>

                <br>

                <a
                    class="button"
                    href="/certificate/{h(verification_code)}/download"
                >
                    Download PDF
                </a>

            </div>

        </div>
        """,
        "Certificate"
    )


# ============================================================
# DOWNLOAD CERTIFICATE PDF
# ============================================================

@app.route(
    "/certificate/<verification_code>/download"
)
@login_required
def download_certificate(verification_code):

    user = current_user()

    db = get_db()

    certificate_record = db.execute(
        """
        SELECT
            certificates.*,
            users.name,
            users.email

        FROM certificates

        JOIN users
            ON certificates.user_id = users.id

        WHERE certificates.verification_code = ?
        """,
        (verification_code,)
    ).fetchone()

    db.close()

    if not certificate_record:
        abort(404)

    if (
        user["role"] != "admin"
        and certificate_record["user_id"] != user["id"]
    ):
        abort(403)

    buffer = BytesIO()

    pdf = canvas.Canvas(
        buffer,
        pagesize=letter
    )

    width, height = letter

    # --------------------------------------------------------
    # Background
    # --------------------------------------------------------

    pdf.setFillColorRGB(
        0.02,
        0.08,
        0.13
    )

    pdf.rect(
        0,
        0,
        width,
        height,
        fill=1,
        stroke=0
    )

    # --------------------------------------------------------
    # Outer border
    # --------------------------------------------------------

    pdf.setStrokeColorRGB(
        0.83,
        0.69,
        0.22
    )

    pdf.setLineWidth(5)

    pdf.rect(
        35,
        35,
        width - 70,
        height - 70,
        fill=0,
        stroke=1
    )

    pdf.setLineWidth(1)

    pdf.rect(
        50,
        50,
        width - 100,
        height - 100,
        fill=0,
        stroke=1
    )

    # --------------------------------------------------------
    # Title
    # --------------------------------------------------------

    pdf.setFillColorRGB(
        0.96,
        0.80,
        0.36
    )

    pdf.setFont(
        "Helvetica-Bold",
        30
    )

    pdf.drawCentredString(
        width / 2,
        height - 120,
        "SMART SEVA"
    )

    pdf.setFont(
        "Helvetica-Bold",
        24
    )

    pdf.drawCentredString(
        width / 2,
        height - 165,
        "CERTIFICATE OF SEVA"
    )

    # --------------------------------------------------------
    # Body
    # --------------------------------------------------------

    pdf.setFillColorRGB(
        0.9,
        0.93,
        0.95
    )

    pdf.setFont(
        "Helvetica",
        14
    )

    pdf.drawCentredString(
        width / 2,
        height - 220,
        "This certificate recognizes the community"
    )

    pdf.drawCentredString(
        width / 2,
        height - 245,
        "service contribution of"
    )

    # --------------------------------------------------------
    # Student name
    # --------------------------------------------------------

    pdf.setFillColorRGB(
        0.96,
        0.80,
        0.36
    )

    pdf.setFont(
        "Helvetica-Bold",
        26
    )

    pdf.drawCentredString(
        width / 2,
        height - 300,
        certificate_record["name"]
    )

    # --------------------------------------------------------
    # Hours
    # --------------------------------------------------------

    pdf.setFillColorRGB(
        0.9,
        0.93,
        0.95
    )

    pdf.setFont(
        "Helvetica",
        15
    )

    pdf.drawCentredString(
        width / 2,
        height - 350,
        "for completing"
    )

    pdf.setFillColorRGB(
        0.96,
        0.80,
        0.36
    )

    pdf.setFont(
        "Helvetica-Bold",
        28
    )

    pdf.drawCentredString(
        width / 2,
        height - 390,
        f'{round(float(certificate_record["hours"]), 2)} Hours'
    )

    pdf.setFillColorRGB(
        0.9,
        0.93,
        0.95
    )

    pdf.setFont(
        "Helvetica",
        15
    )

    pdf.drawCentredString(
        width / 2,
        height - 425,
        "of verified community seva."
    )

    # --------------------------------------------------------
    # Verification
    # --------------------------------------------------------

    try:
        issued_date = as_datetime(
            certificate_record["issued_at"]
        ).strftime("%B %d, %Y")
    except ValueError:
        issued_date = certificate_record["issued_at"]

    pdf.setFont(
        "Helvetica",
        11
    )

    pdf.drawCentredString(
        width / 2,
        125,
        f"Issued: {issued_date}"
    )

    pdf.drawCentredString(
        width / 2,
        100,
        "Verification Code: "
        + certificate_record["verification_code"]
    )

    pdf.setFillColorRGB(
        0.96,
        0.80,
        0.36
    )

    pdf.setFont(
        "Helvetica-Bold",
        16
    )

    pdf.drawCentredString(
        width / 2,
        75,
        "ੴ • Seva • Sangat • Chardi Kala"
    )

    pdf.save()

    buffer.seek(0)

    safe_name = clean(
        certificate_record["name"],
        80
    ).replace(" ", "_")

    return send_file(
        buffer,
        as_attachment=True,
        download_name=(
            f"Smart_Seva_Certificate_{safe_name}.pdf"
        ),
        mimetype="application/pdf"
    )


# ============================================================
# PUBLIC CERTIFICATE VERIFICATION
# ============================================================

@app.route(
    "/verify/<verification_code>"
)
def verify_certificate(verification_code):

    db = get_db()

    certificate_record = db.execute(
        """
        SELECT
            certificates.*,
            users.name

        FROM certificates

        JOIN users
            ON certificates.user_id = users.id

        WHERE certificates.verification_code = ?
        """,
        (verification_code,)
    ).fetchone()

    db.close()

    if not certificate_record:

        return layout(
            """
            <div class="container">

                <div class="card center">

                    <h1>
                        Certificate Not Found
                    </h1>

                    <p class="muted">
                        The verification code is not valid.
                    </p>

                </div>

            </div>
            """,
            "Certificate Verification"
        )

    try:
        issued_date = as_datetime(
            certificate_record["issued_at"]
        ).strftime("%B %d, %Y")
    except ValueError:
        issued_date = certificate_record["issued_at"]

    return layout(
        f"""
        <div class="container">

            <div class="card center">

                <div class="stat">
                    ✓
                </div>

                <h1>
                    Certificate Verified
                </h1>

                <p class="muted">
                    This Smart Seva certificate is valid.
                </p>

                <h2 style="color:white">
                    {h(certificate_record["name"])}
                </h2>

                <p>
                    Verified Seva Hours
                </p>

                <div class="stat">
                    {round(float(certificate_record["hours"]), 2)}
                </div>

                <p class="muted">
                    Issued {h(issued_date)}
                </p>

                <p>
                    Verification Code:
                    <code>
                        {h(verification_code)}
                    </code>
                </p>

            </div>

        </div>
        """,
        "Verify Certificate"
    )


# ============================================================
# SPECIAL EVENTS
# ============================================================

@app.route("/events")
@login_required
def events():
    db = get_db()
    rows = db.execute(
        """
        SELECT *
        FROM public.special_events
        WHERE status = 'published'
        ORDER BY date ASC, start_time ASC, id ASC
        """
    ).fetchall()
    db.close()

    cards = ""
    for event in rows:
        time_text = ""
        if event["start_time"]:
            time_text = format_time_label(event["start_time"])
            if event["end_time"]:
                time_text += " – " + format_time_label(event["end_time"])

        cards += f"""
        <div class="card">
            <div class="small">SPECIAL EVENT</div>
            <h2>{h(event["title"])}</h2>
            <p class="muted">{h(event["description"] or "")}</p>
            <p>📅 {h(event["date"])}</p>
            {f'<p>⏰ {h(time_text)}</p>' if time_text else ""}
            {f'<p>📍 {h(event["location"])}</p>' if event["location"] else ""}
        </div>
        """

    return layout(
        f"""
        <div class="container">
            <h1>🎉 Special Events</h1>
            <p class="muted">Stay updated on Hola Mahalla, Vaisakhi, and other special community gatherings.</p>
            <div class="grid">
                {cards or '<div class="card empty-state"><h2>No Special Events Yet</h2><p class="muted">Check back soon for upcoming community events.</p></div>'}
            </div>
        </div>
        """,
        "Special Events"
    )


@app.route("/admin/events")
@events_admin_required
def admin_events():
    db = get_db()
    rows = db.execute(
        """
        SELECT *
        FROM public.special_events
        ORDER BY date ASC, start_time ASC, id ASC
        """
    ).fetchall()
    db.close()

    token = h(csrf_token())
    cards = ""
    for event in rows:
        status_class = "success" if event["status"] == "published" else "warning"
        cards += f"""
        <div class="card">
            <h2>{h(event["title"])}</h2>
            <p class="muted">{h(event["description"] or "")}</p>
            <p>📅 {h(event["date"])}{f' • ⏰ {h(format_time_label(event["start_time"]))}' if event["start_time"] else ""}</p>
            {f'<p>📍 {h(event["location"])}</p>' if event["location"] else ""}
            <p><span class="{status_class}">{h(event["status"].title())}</span></p>
            <form method="POST" action="/admin/events/delete/{event["id"]}" onsubmit="return confirm('Delete this special event?');">
                <input type="hidden" name="csrf_token" value="{token}">
                <button class="danger">Delete</button>
            </form>
        </div>
        """

    return layout(
        f"""
        <div class="container">
            <h1>Manage Special Events</h1>
            <p class="muted">Add and maintain upcoming community events.</p>
            <div class="card">
                <h2>➕ Add Special Event</h2>
                <form method="POST" action="/admin/events/create">
                    <input type="hidden" name="csrf_token" value="{token}">
                    <label>Event Name<input name="title" maxlength="150" placeholder="Hola Mahalla" required></label>
                    <label>Description<textarea name="description" maxlength="1500" placeholder="Describe the event..." required></textarea>
                    <div class="grid">
                        <label>Date<input type="date" name="date" required></label>
                        <label>Start Time<input type="time" name="start_time"></label>
                        <label>End Time<input type="time" name="end_time"></label>
                        <label>Location<input name="location" maxlength="250" placeholder="Gurdwara / Community Center"></label>
                    </div>
                    <label>Status
                        <select name="status">
                            <option value="published">Published</option>
                            <option value="draft">Draft</option>
                        </select>
                    </label>
                    <button>Save Event</button>
                </form>
            </div>
            <div class="grid">
                {cards or '<div class="card"><h2>No events created yet.</h2></div>'}
            </div>
        </div>
        """,
        "Manage Special Events"
    )


@app.route("/admin/events/create", methods=["POST"])
@events_admin_required
def create_event():
    validate_csrf()

    title = clean(request.form.get("title"), 150)
    description = clean(request.form.get("description"), 1500)
    date = clean(request.form.get("date"), 20)
    start_time = clean(request.form.get("start_time"), 20)
    end_time = clean(request.form.get("end_time"), 20)
    location = clean(request.form.get("location"), 250)
    status = clean(request.form.get("status", "published"), 20)

    if not title or not description or not date:
        abort(400, "Event name, description, and date are required.")
    if status not in ("published", "draft"):
        abort(400, "Invalid event status.")

    try:
        datetime.strptime(date, "%Y-%m-%d")
        if start_time:
            datetime.strptime(start_time, "%H:%M")
        if end_time:
            datetime.strptime(end_time, "%H:%M")
        if start_time and end_time and end_time <= start_time:
            abort(400, "End time must be after start time.")
    except ValueError:
        abort(400, "Enter a valid event date or time.")

    db = get_db()
    db.execute(
        """
        INSERT INTO public.special_events
        (title, description, date, start_time, end_time, location, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (title, description, date, start_time or None, end_time or None, location or None, status)
    )
    db.commit()
    db.close()
    return redirect(url_for("admin_events"))


@app.route("/admin/events/delete/<int:event_id>", methods=["POST"])
@events_admin_required
def delete_event(event_id):
    validate_csrf()
    db = get_db()
    db.execute("DELETE FROM public.special_events WHERE id = ?", (event_id,))
    db.commit()
    db.close()
    return redirect(url_for("admin_events"))


# ============================================================
# ADMIN MANAGE SEVA
# ============================================================

@app.route("/admin/seva")
@seva_admin_required
def admin_seva():

    db = get_db()

    opportunities = db.execute(
        """
        SELECT seva.*, users.name AS creator_name,
               (SELECT COUNT(*) FROM signups WHERE signups.seva_id = seva.id) AS signup_count
        FROM seva
        LEFT JOIN users ON seva.created_by = users.id
        ORDER BY seva.date ASC, seva.start_time ASC
        """
    ).fetchall()

    pending_signups = db.execute(
        """
        SELECT signups.*, users.name AS student_name, users.email AS student_email,
               seva.title AS seva_title, seva.date AS seva_date
        FROM signups
        JOIN users ON signups.user_id = users.id
        JOIN seva ON signups.seva_id = seva.id
        WHERE signups.status = 'pending' AND signups.check_out IS NOT NULL
        ORDER BY signups.id DESC
        """
    ).fetchall()

    db.close()
    token = h(csrf_token())

    seva_rows = "".join(
        f"<tr><td>{h(item['title'])}</td><td>{h(item['date'])}</td><td>{h(item['location'])}</td><td>{item['signup_count']}</td><td>{item['max_volunteers'] or 'Unlimited'}</td><td><form method='POST' action='/admin/delete-seva/{item['id']}'><input type='hidden' name='csrf_token' value='{token}'><button class='danger'>Delete</button></form></td></tr>"
        for item in opportunities
    )

    pending_rows = "".join(
        f"<tr><td>{h(item['student_name'])}<br><span class='small'>{h(item['student_email'])}</span></td><td>{h(item['seva_title'])}</td><td>{h(item['seva_date'])}</td><td>{round(float(item['hours'] or 0), 2)}</td><td><form method='POST' action='/admin/approve/{item['id']}' style='display:inline'><input type='hidden' name='csrf_token' value='{token}'><button>Approve</button></form> <form method='POST' action='/admin/reject/{item['id']}' style='display:inline'><input type='hidden' name='csrf_token' value='{token}'><button class='danger'>Reject</button></form></td></tr>"
        for item in pending_signups
    )

    return layout(
        f"""
        <div class='container'>
            <h1>Manage Seva</h1>
            <p class='muted'>Create seva opportunities and review completed service hours.</p>
            <div class='card'>
                <h2>➕ Create Seva Opportunity</h2>
                <form method='POST' action='/admin/create-seva'>
                    <input type='hidden' name='csrf_token' value='{token}'>
                    <label>Title<input name='title' maxlength='150' placeholder='Community Kitchen' required></label>
                    <label>Description<textarea name='description' maxlength='1000' placeholder='Describe the seva opportunity...' required></textarea></label>
                    <label>Location<input name='location' maxlength='250' placeholder='Gurdwara / Community Center' required></label>
                    <div class='grid'>
                        <label>Date<input type='date' name='date' required></label>
                        <label>Start Time<input type='time' name='start_time' required></label>
                        <label>End Time<input type='time' name='end_time' required></label>
                        <label>Volunteer Spots<input type='number' name='max_volunteers' min='0' value='0'><span class='small'>0 = unlimited</span></label>
                    </div>
                    <button>Create Seva</button>
                </form>
            </div>
            <div class='card'><h2>📋 Seva Opportunities</h2><div style='overflow-x:auto'><table><tr><th>Title</th><th>Date</th><th>Location</th><th>Signups</th><th>Capacity</th><th>Action</th></tr>{seva_rows or '<tr><td colspan="5">No seva opportunities created yet.</td></tr>'}</table></div></div>
            <div class='card'><h2>⏳ Hours Awaiting Approval</h2><div style='overflow-x:auto'><table><tr><th>Student</th><th>Seva</th><th>Date</th><th>Hours</th><th>Action</th></tr>{pending_rows or '<tr><td colspan="5">No completed seva is waiting for approval.</td></tr>'}</table></div></div>
        </div>
        """,
        "Manage Seva"
    )


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route("/admin")
@admin_required
def admin():

    db = get_db()

    # --------------------------------------------------------
    # Statistics
    # --------------------------------------------------------

    student_count = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM users
        WHERE role = 'student'
        """
    ).fetchone()["count"]

    seva_count = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM seva
        """
    ).fetchone()["count"]

    pending_count = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM signups
        WHERE status = 'pending'
        AND check_out IS NOT NULL
        """
    ).fetchone()["count"]

    approved_hours = db.execute(
        """
        SELECT COALESCE(SUM(hours), 0) AS total
        FROM signups
        WHERE status = 'approved'
        """
    ).fetchone()["total"]

    # --------------------------------------------------------
    # Seva opportunities
    # --------------------------------------------------------

    opportunities = db.execute(
        """
        SELECT
            seva.*,
            users.name AS creator_name,

            (
                SELECT COUNT(*)
                FROM signups
                WHERE signups.seva_id = seva.id
            ) AS signup_count

        FROM seva

        LEFT JOIN users
            ON seva.created_by = users.id

        ORDER BY
            seva.date ASC,
            seva.start_time ASC
        """
    ).fetchall()

    # --------------------------------------------------------
    # Pending check-outs
    # --------------------------------------------------------

    pending_signups = db.execute(
        """
        SELECT
            signups.*,

            users.name AS student_name,
            users.email AS student_email,

            seva.title AS seva_title,
            seva.date AS seva_date,
            seva.location AS seva_location

        FROM signups

        JOIN users
            ON signups.user_id = users.id

        JOIN seva
            ON signups.seva_id = seva.id

        WHERE signups.status = 'pending'

        AND signups.check_out IS NOT NULL

        ORDER BY signups.id DESC
        """
    ).fetchall()

    # --------------------------------------------------------
    # All students
    # --------------------------------------------------------

    students = db.execute(
        """
        SELECT
            users.id,
            users.name,
            users.email,
            users.goal_hours,
            users.created_at,

            COALESCE(
                (
                    SELECT SUM(signups.hours)
                    FROM signups
                    WHERE signups.user_id = users.id
                    AND signups.status = 'approved'
                ),
                0
            ) AS total_hours

        FROM users

        WHERE users.role = 'student'

        ORDER BY users.name
        """
    ).fetchall()

    db.close()

    token = h(csrf_token())

    # --------------------------------------------------------
    # Opportunity HTML
    # --------------------------------------------------------

    seva_rows = ""

    for item in opportunities:

        seva_rows += f"""
        <tr>

            <td>
                {h(item["title"])}
            </td>

            <td>
                {h(item["date"])}
            </td>

            <td>
                {h(item["location"])}
            </td>

            <td>
                {item["signup_count"]}
                /
                {item["max_volunteers"] if item["max_volunteers"] else "∞"}
                <div class="capacity-bar"><div class="capacity-fill" style="width:{min(100, round(item["signup_count"] / item["max_volunteers"] * 100)) if item["max_volunteers"] else 0}%"></div></div>
            </td>

            <td>

                <form
                    method="POST"
                    action="/admin/delete-seva/{item["id"]}"
                    onsubmit="return confirm(
                        'Delete this seva opportunity?'
                    );"
                >

                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{token}"
                    >

                    <button class="danger">
                        Delete
                    </button>

                </form>

            </td>

        </tr>
        """

    if not seva_rows:

        seva_rows = """
        <tr>
            <td colspan="5">
                No seva opportunities created yet.
            </td>
        </tr>
        """

    # --------------------------------------------------------
    # Pending approval HTML
    # --------------------------------------------------------

    pending_rows = ""

    for item in pending_signups:

        pending_rows += f"""
        <tr>

            <td>
                {h(item["student_name"])}
                <br>
                <span class="small">
                    {h(item["student_email"])}
                </span>
            </td>

            <td>
                {h(item["seva_title"])}
            </td>

            <td>
                {h(item["seva_date"])}
            </td>

            <td>
                {round(float(item["hours"] or 0), 2)}
            </td>

            <td>

                <form
                    method="POST"
                    action="/admin/approve/{item["id"]}"
                    style="display:inline"
                >

                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{token}"
                    >

                    <button>
                        Approve
                    </button>

                </form>

                <form
                    method="POST"
                    action="/admin/reject/{item["id"]}"
                    style="display:inline"
                >

                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{token}"
                    >

                    <button class="danger">
                        Reject
                    </button>

                </form>

            </td>

        </tr>
        """

    if not pending_rows:

        pending_rows = """
        <tr>
            <td colspan="5">
                No completed seva is waiting for approval.
            </td>
        </tr>
        """

    # --------------------------------------------------------
    # Student rows
    # --------------------------------------------------------

    student_rows = ""

    for student in students:

        total = float(
            student["total_hours"] or 0
        )

        goal = float(
            student["goal_hours"] or 0
        )

        if goal > 0:

            progress = min(
                100,
                round(
                    total / goal * 100,
                    1
                )
            )

        else:

            progress = 0

        student_rows += f"""
        <tr>

            <td>
                {h(student["name"])}
            </td>

            <td>
                {h(student["email"])}
            </td>

            <td>
                {round(total, 2)}
                /
                {round(goal, 2)}
            </td>

            <td>
                {progress}%
            </td>

        </tr>
        """

    if not student_rows:

        student_rows = """
        <tr>
            <td colspan="4">
                No students have registered yet.
            </td>
        </tr>
        """

    return layout(
        f"""
        <div class="container">

            <h1>
                🛡 Admin Dashboard
            </h1>

            <p class="muted">
                Manage seva opportunities and verify
                student service hours.
            </p>

            <!-- =========================================
                 STATISTICS
                 ========================================= -->

            <div class="grid">

                <div class="card">

                    <div class="small">
                        STUDENTS
                    </div>

                    <div class="stat">
                        {student_count}
                    </div>

                </div>

                <div class="card">

                    <div class="small">
                        SEVA OPPORTUNITIES
                    </div>

                    <div class="stat">
                        {seva_count}
                    </div>

                </div>

                <div class="card">

                    <div class="small">
                        PENDING APPROVALS
                    </div>

                    <div class="stat">
                        {pending_count}
                    </div>

                </div>

                <div class="card">

                    <div class="small">
                        APPROVED HOURS
                    </div>

                    <div class="stat">
                        {round(float(approved_hours or 0), 2)}
                    </div>

                </div>

            </div>


            <!-- =========================================
                 CREATE SEVA
                 ========================================= -->

            <div class="card">

                <h2>
                    ➕ Create Seva Opportunity
                </h2>

                <form
                    method="POST"
                    action="/admin/create-seva"
                >

                    <input
                        type="hidden"
                        name="csrf_token"
                        value="{token}"
                    >

                    <label>
                        Title
                    </label>

                    <input
                        name="title"
                        maxlength="150"
                        placeholder="Community Kitchen"
                        required
                    >

                    <label>
                        Description
                    </label>

                    <textarea
                        name="description"
                        maxlength="1000"
                        placeholder="Describe the seva opportunity..."
                        required
                    ></textarea>

                    <label>
                        Location
                    </label>

                    <input
                        name="location"
                        maxlength="250"
                        placeholder="Gurdwara / Community Center"
                        required
                    >

                    <div class="grid">

                        <div>

                            <label>
                                Date
                            </label>

                            <input
                                type="date"
                                name="date"
                                required
                            >

                        </div>

                        <div>

                            <label>
                                Start Time
                            </label>

                            <input
                                type="time"
                                name="start_time"
                                required
                            >

                        </div>

                        <div>

                            <label>
                                End Time
                            </label>

                            <input
                                type="time"
                                name="end_time"
                                required
                            >

                        </div>

                    </div>

                    <button>
                        Create Seva
                    </button>

                </form>

            </div>


            <!-- =========================================
                 SEVA MANAGEMENT
                 ========================================= -->

            <div class="card">

                <h2>
                    📋 Seva Opportunities
                </h2>

                <div style="overflow-x:auto">

                    <table>

                        <tr>

                            <th>
                                Title
                            </th>

                            <th>
                                Date
                            </th>

                            <th>
                                Location
                            </th>

                            <th>
                                Capacity
                            </th>

                            <th>
                                Action
                            </th>

                        </tr>

                        {seva_rows}

                    </table>

                </div>

            </div>


            <!-- =========================================
                 APPROVALS
                 ========================================= -->

            <div class="card">

                <h2>
                    ⏳ Hours Awaiting Approval
                </h2>

                <div style="overflow-x:auto">

                    <table>

                        <tr>

                            <th>
                                Student
                            </th>

                            <th>
                                Seva
                            </th>

                            <th>
                                Date
                            </th>

                            <th>
                                Hours
                            </th>

                            <th>
                                Action
                            </th>

                        </tr>

                        {pending_rows}

                    </table>

                </div>

            </div>


            <!-- =========================================
                 STUDENTS
                 ========================================= -->

            <div class="card">

                <h2>
                    👥 Students
                </h2>

                <div style="overflow-x:auto">

                    <table>

                        <tr>

                            <th>
                                Name
                            </th>

                            <th>
                                Email
                            </th>

                            <th>
                                Hours
                            </th>

                            <th>
                                Progress
                            </th>

                        </tr>

                        {student_rows}

                    </table>

                </div>

            </div>

        </div>
        """,
        "Admin"
    )


# ============================================================
# ADMIN CREATE SEVA
# ============================================================

@app.route(
    "/admin/create-seva",
    methods=["POST"]
)
@seva_admin_required
def create_seva():

    validate_csrf()

    user = current_user()

    title = clean(
        request.form.get("title"),
        150
    )

    description = clean(
        request.form.get("description"),
        1000
    )

    location = clean(
        request.form.get("location"),
        250
    )

    date = clean(
        request.form.get("date"),
        20
    )

    start_time = clean(
        request.form.get("start_time"),
        20
    )

    end_time = clean(
        request.form.get("end_time"),
        20
    )

    try:
        max_volunteers = max(0, int(request.form.get("max_volunteers") or 0))
    except ValueError:
        abort(400, "Capacity must be a whole number.")

    if not all(
        [
            title,
            description,
            location,
            date,
            start_time,
            end_time
        ]
    ):

        abort(
            400,
            "All seva fields are required."
        )

    # Validate date/time format.
    try:

        datetime.strptime(
            date,
            "%Y-%m-%d"
        )

        start = datetime.strptime(
            start_time,
            "%H:%M"
        )

        end = datetime.strptime(
            end_time,
            "%H:%M"
        )

        if end <= start:

            abort(
                400,
                "End time must be after start time."
            )

    except ValueError:

        abort(
            400,
            "Invalid date or time."
        )

    db = get_db()

    db.execute(
        """
        INSERT INTO seva
        (
            title,
            description,
            location,
            date,
            start_time,
            end_time,
            max_volunteers,
            created_by,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            description,
            location,
            date,
            start_time,
            end_time,
            max_volunteers,
            user["id"],
            utc_now()
        )
    )

    db.commit()
    db.close()

    return redirect(
        url_for("admin")
    )


# ============================================================
# ADMIN DELETE SEVA
# ============================================================

@app.route(
    "/admin/delete-seva/<int:seva_id>",
    methods=["POST"]
)
@seva_admin_required
def delete_seva(seva_id):

    validate_csrf()

    db = get_db()

    db.execute(
        """
        DELETE FROM seva
        WHERE id = ?
        """,
        (seva_id,)
    )

    db.commit()
    db.close()

    return redirect(
        url_for("admin")
    )


# ============================================================
# ADMIN APPROVE HOURS
# ============================================================

@app.route(
    "/admin/approve/<int:signup_id>",
    methods=["POST"]
)
@seva_admin_required
def approve_signup(signup_id):

    validate_csrf()

    db = get_db()

    signup = db.execute(
        """
        SELECT *
        FROM signups
        WHERE id = ?
        """,
        (signup_id,)
    ).fetchone()

    if not signup:

        db.close()

        abort(404)

    if not signup["check_out"]:

        db.close()

        abort(
            400,
            "Student has not checked out yet."
        )

    db.execute(
        """
        UPDATE signups

        SET status = 'approved'

        WHERE id = ?
        """,
        (signup_id,)
    )

    db.commit()
    notify_user(signup["user_id"], "Seva hours approved", "Your completed Seva hours have been approved.", "/dashboard")
    db.close()

    return redirect(
        url_for("admin")
    )


# ============================================================
# ADMIN REJECT HOURS
# ============================================================

@app.route(
    "/admin/reject/<int:signup_id>",
    methods=["POST"]
)
@seva_admin_required
def reject_signup(signup_id):

    validate_csrf()

    db = get_db()

    signup = db.execute(
        """
        SELECT id, user_id
        FROM signups
        WHERE id = ?
        """,
        (signup_id,)
    ).fetchone()

    if not signup:

        db.close()

        abort(404)

    db.execute(
        """
        UPDATE signups

        SET status = 'rejected'

        WHERE id = ?
        """,
        (signup_id,)
    )

    db.commit()
    notify_user(signup["user_id"], "Seva hours rejected", "Your completed Seva hours were not approved. Please contact the administrator if you need clarification.", "/dashboard")
    db.close()

    return redirect(
        url_for("admin")
    )


# ============================================================
# NOTIFICATIONS / PROFILE
# ============================================================

@app.route("/notifications")
@login_required
def notifications():
    user = current_user()
    db = get_db()
    rows = db.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 100", (user["id"],)).fetchall()
    db.execute("UPDATE notifications SET is_read = TRUE WHERE user_id = ?", (user["id"],))
    db.commit(); db.close()
    cards = "".join(f"<div class='card'><h2>{h(r['title'])}</h2><p>{h(r['message'])}</p><p class='small'>{h(r['created_at'])}</p>{f"<a class='button' href='{h(r['link'])}'>Open</a>" if r['link'] else ''}</div>" for r in rows)
    return layout(f"<div class='container'><h1>🔔 Notifications</h1>{cards or '<div class="card"><p class="muted">No notifications yet.</p></div>'}</div>", "Notifications")


@app.route("/profile")
@login_required
def profile():
    user = current_user()
    db = get_db()
    stats = db.execute("SELECT COUNT(*) FILTER (WHERE status='approved') AS completed, COALESCE(SUM(hours) FILTER (WHERE status='approved'),0) AS hours FROM signups WHERE user_id = ?", (user["id"],)).fetchone()
    db.close()
    badges = milestone_badges(float(stats["hours"] or 0), int(stats["completed"] or 0))
    badges_html = " ".join(f"<span class='button' style='display:inline-block;margin:4px'>{h(b)}</span>" for b in badges) or "<p class='muted'>Your first milestone will appear after your first approved Seva.</p>"
    return layout(f"""<div class='container'><h1>👤 My Profile</h1><div class='grid'><div class='card'><h2>{h(user['name'])}</h2><p>{h(user['email'])}</p></div><div class='card'><h2>Verified Hours</h2><div class='stat'>{round(float(stats['hours'] or 0),2)}</div></div><div class='card'><h2>Completed Sevas</h2><div class='stat'>{int(stats['completed'] or 0)}</div></div></div><div class='card'><h2>🏅 Milestones</h2>{badges_html}</div></div>""", "Profile")


# ============================================================
# ERROR PAGES
# ============================================================

@app.errorhandler(400)
def bad_request(error):

    return layout(
        f"""
        <div class="container">

            <div class="card center">

                <h1>
                    400
                </h1>

                <h2>
                    Bad Request
                </h2>

                <p class="muted">
                    {h(error.description)}
                </p>

                <a
                    class="button"
                    href="/"
                >
                    Return Home
                </a>

            </div>

        </div>
        """,
        "Bad Request"
    ), 400


@app.errorhandler(403)
def forbidden(error):

    return layout(
        """
        <div class="container">

            <div class="card center">

                <h1>
                    403
                </h1>

                <h2>
                    Access Denied
                </h2>

                <p class="muted">
                    You don't have permission to view this page.
                </p>

                <a
                    class="button"
                    href="/"
                >
                    Return Home
                </a>

            </div>

        </div>
        """,
        "Access Denied"
    ), 403


@app.errorhandler(404)
def not_found(error):

    return layout(
        """
        <div class="container">

            <div class="card center">

                <h1>
                    404
                </h1>

                <h2>
                    Page Not Found
                </h2>

                <p class="muted">
                    The page you're looking for doesn't exist.
                </p>

                <a
                    class="button"
                    href="/"
                >
                    Return Home
                </a>

            </div>

        </div>
        """,
        "Page Not Found"
    ), 404

# ============================================================
# START APPLICATION
# ============================================================

if __name__ == "__main__":

    print()
    print("=" * 60)
    print("SMART SEVA — SUPABASE")
    print("=" * 60)
    print("Open your browser at:")
    print("http://127.0.0.1:5000")
    print()
    print("Authentication and data are stored in Supabase.")
    print("Set SMART_SEVA_ADMIN_EMAIL to an existing Supabase Auth email to make it an admin.")
    print("=" * 60)
    print()

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=os.environ.get("FLASK_DEBUG", "0") == "1"
    )