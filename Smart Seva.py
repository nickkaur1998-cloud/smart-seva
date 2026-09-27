from flask import Flask, request, redirect, url_for, session, render_template_string, send_file # type: ignore
import sqlite3
import hashlib
import secrets
import io
import os
from datetime import datetime

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

DB = "smart_seva.db"


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'student',
            goal REAL DEFAULT 40
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS seva (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            location TEXT NOT NULL,
            date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            spots INTEGER NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            seva_id INTEGER NOT NULL,
            check_in TEXT,
            check_out TEXT,
            hours REAL DEFAULT 0,
            verified INTEGER DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS certificates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            certificate_id TEXT UNIQUE NOT NULL,
            hours REAL NOT NULL,
            issued_at TEXT NOT NULL
        )
    """)

    # Create admin
    admin = conn.execute(
        "SELECT * FROM users WHERE email=?",
        ("admin@smartseva.local",)
    ).fetchone()

    if not admin:
        conn.execute(
            """
            INSERT INTO users
            (name,email,password,role,goal)
            VALUES (?,?,?,?,?)
            """,
            (
                "Gurdwara Administrator",
                "admin@smartseva.local",
                hash_password("admin123"),
                "admin",
                0
            )
        )

    # Sample seva
    count = conn.execute(
        "SELECT COUNT(*) FROM seva"
    ).fetchone()[0]

    if count == 0:

        sample = [
            (
                "Langar Serving",
                "Help serve food to the sangat.",
                "Langar Hall",
                "Saturday",
                "11:00 AM",
                "1:00 PM",
                10
            ),
            (
                "Cleaning Seva",
                "Help clean and organize the Gurdwara.",
                "Main Hall",
                "Saturday",
                "2:00 PM",
                "3:00 PM",
                8
            ),
            (
                "Jora Ghar",
                "Help organize and maintain the shoe area.",
                "Jora Ghar",
                "Sunday",
                "10:00 AM",
                "11:00 AM",
                5
            ),
            (
                "Punjabi Class Helper",
                "Help younger students during Punjabi class.",
                "Classroom",
                "Sunday",
                "12:00 PM",
                "1:30 PM",
                4
            ),
            (
                "Kirtan Event Setup",
                "Help set up equipment and seating for Kirtan.",
                "Darbar Sahib",
                "Sunday",
                "9:00 AM",
                "11:00 AM",
                6
            )
        ]

        conn.executemany(
            """
            INSERT INTO seva
            (title,description,location,date,start_time,end_time,spots)
            VALUES (?,?,?,?,?,?,?)
            """,
            sample
        )

    conn.commit()
    conn.close()


# ============================================================
# SECURITY
# ============================================================

def hash_password(password):
    return hashlib.sha256(
        password.encode()
    ).hexdigest()


def current_user():
    if "user_id" not in session:
        return None

    conn = get_db()

    user = conn.execute(
        "SELECT * FROM users WHERE id=?",
        (session["user_id"],)
    ).fetchone()

    conn.close()

    return user


def calculate_hours(user_id):

    conn = get_db()

    result = conn.execute(
        """
        SELECT COALESCE(SUM(hours),0)
        FROM signups
        WHERE user_id=?
        AND verified=1
        """,
        (user_id,)
    ).fetchone()[0]

    conn.close()

    return round(result, 2)


# ============================================================
# WEBSITE DESIGN
# ============================================================

STYLE = """

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family: Arial, Helvetica, sans-serif;
    background: #f7f4ed;
    color: #292929;
}

nav {
    background: #741d1d;
    color: white;
    padding: 18px 7%;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.logo {
    font-size: 24px;
    font-weight: bold;
}

nav a {
    color: white;
    text-decoration: none;
    margin-left: 22px;
    font-weight: bold;
}

.container {
    width: 90%;
    max-width: 1100px;
    margin: auto;
}

.hero {
    text-align: center;
    padding: 90px 20px;
}

.hero-icon {
    font-size: 70px;
}

.hero h1 {
    font-size: 65px;
    margin: 15px 0 5px;
    color: #741d1d;
}

.hero p {
    font-size: 20px;
    color: #666;
}

.btn {
    display: inline-block;
    padding: 13px 22px;
    border-radius: 8px;
    border: none;
    text-decoration: none;
    font-weight: bold;
    cursor: pointer;
    margin: 5px;
}

.btn-primary {
    background: #741d1d;
    color: white;
}

.btn-gold {
    background: #d4a72c;
    color: #111;
}

.btn-green {
    background: #198754;
    color: white;
}

.btn-red {
    background: #c0392b;
    color: white;
}

.btn-gray {
    background: #ddd;
    color: #222;
}

.grid {
    display: grid;
    grid-template-columns: repeat(auto-fit,minmax(250px,1fr));
    gap: 20px;
}

.card {
    background: white;
    padding: 25px;
    border-radius: 15px;
    box-shadow: 0 5px 20px rgba(0,0,0,.07);
    margin-bottom: 20px;
}

.card h2 {
    color: #741d1d;
}

.feature {
    text-align: center;
}

.feature-icon {
    font-size: 40px;
}

.form-card {
    max-width: 480px;
    margin: 60px auto;
}

input, textarea {
    width: 100%;
    padding: 13px;
    margin: 7px 0 18px;
    border: 1px solid #ccc;
    border-radius: 8px;
    font-size: 16px;
}

textarea {
    min-height: 100px;
}

label {
    font-weight: bold;
}

.progress {
    width: 100%;
    height: 30px;
    background: #ddd;
    border-radius: 20px;
    overflow: hidden;
}

.progress-bar {
    height: 100%;
    background: #d4a72c;
    text-align: center;
    padding-top: 6px;
    font-weight: bold;
}

.big-number {
    font-size: 48px;
    color: #741d1d;
    font-weight: bold;
}

.alert {
    background: #fff3cd;
    padding: 15px;
    border-radius: 8px;
    margin: 20px 0;
}

.success {
    background: #d1e7dd;
}

.danger {
    background: #f8d7da;
}

.badge {
    padding: 6px 10px;
    border-radius: 15px;
    font-size: 13px;
}

.verified {
    background: #d1e7dd;
    color: #146c43;
}

.pending {
    background: #fff3cd;
    color: #664d03;
}

table {
    width: 100%;
    border-collapse: collapse;
}

th, td {
    padding: 13px;
    border-bottom: 1px solid #ddd;
    text-align: left;
}

footer {
    text-align: center;
    padding: 50px;
    color: #777;
}

.certificate {
    background: white;
    border: 8px solid #d4a72c;
    padding: 60px;
    text-align: center;
    max-width: 800px;
    margin: 50px auto;
}

.certificate h1 {
    color: #741d1d;
    font-size: 45px;
}

.certificate .name {
    font-size: 32px;
    font-weight: bold;
    margin: 30px;
}

@media(max-width:600px) {

    .hero h1 {
        font-size: 42px;
    }

    nav {
        padding: 15px;
    }

    nav a {
        margin-left: 8px;
        font-size: 13px;
    }

    .certificate {
        padding: 30px;
    }

}

</style>

"""


# ============================================================
# NAVBAR
# ============================================================

def page(title, content):

    user = current_user()

    navbar = """

    <nav>

        <div class="logo">
            🙏 Smart Seva
        </div>

        <div>
    """

    if user:

        if user["role"] == "admin":

            navbar += """
                <a href="/admin">Admin</a>
            """

        else:

            navbar += """
                <a href="/dashboard">Dashboard</a>
                <a href="/seva">Find Seva</a>
            """

        navbar += """
            <a href="/logout">Logout</a>
        """

    else:

        navbar += """
            <a href="/login">Login</a>
            <a href="/register">Register</a>
        """

    navbar += """
        </div>

    </nav>
    """

    return f"""
    <!DOCTYPE html>

    <html>

    <head>

        <title>{title} | Smart Seva</title>

        <meta name="viewport"
              content="width=device-width, initial-scale=1">

        {STYLE}

    </head>

    <body>

        {navbar}

        <main class="container">

            {content}

        </main>

        <footer>
            Smart Seva • Serve. Track. Achieve. 🙏
        </footer>

    </body>

    </html>
    """


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    content = """

    <section class="hero">

        <div class="hero-icon">
            🙏
        </div>

        <h1>
            Smart Seva
        </h1>

        <p>
            Serve. Track. Achieve.
        </p>

        <p>
            Find seva opportunities, track verified
            community service hours, and earn
            a digital certificate.
        </p>

        <br>

        <a href="/register"
           class="btn btn-primary">
            Get Started
        </a>

        <a href="/login"
           class="btn btn-gold">
            Login
        </a>

    </section>


    <div class="grid">

        <div class="card feature">

            <div class="feature-icon">
                🙏
            </div>

            <h2>
                Find Seva
            </h2>

            <p>
                Discover volunteer opportunities
                at the Gurdwara.
            </p>

        </div>


        <div class="card feature">

            <div class="feature-icon">
                ⏱️
            </div>

            <h2>
                Track Hours
            </h2>

            <p>
                Check in and out and build your
                verified service record.
            </p>

        </div>


        <div class="card feature">

            <div class="feature-icon">
                🏆
            </div>

            <h2>
                Get Certified
            </h2>

            <p>
                Reach your goal and receive a
                verifiable service certificate.
            </p>

        </div>

    </div>

    """

    return page("Home", content)


# ============================================================
# REGISTER
# ============================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        if not name or not email or not password:

            return page(
                "Register",
                """
                <div class="alert">
                    Please fill in every field.
                </div>
                """
            )

        conn = get_db()

        try:

            conn.execute(
                """
                INSERT INTO users
                (name,email,password,role,goal)
                VALUES (?,?,?,?,?)
                """,
                (
                    name,
                    email,
                    hash_password(password),
                    "student",
                    40
                )
            )

            conn.commit()

        except sqlite3.IntegrityError:

            conn.close()

            return page(
                "Register",
                """
                <div class="alert danger">
                    An account with that email already exists.
                </div>

                <a href="/register"
                   class="btn btn-primary">
                    Try Again
                </a>
                """
            )

        conn.close()

        return redirect("/login")

    content = """

    <div class="card form-card">

        <h1>
            Create Your Account
        </h1>

        <p>
            Start tracking your seva.
        </p>

        <form method="POST">

            <label>
                Full Name
            </label>

            <input
                name="name"
                placeholder="Your name"
                required
            >

            <label>
                Email
            </label>

            <input
                type="email"
                name="email"
                placeholder="you@example.com"
                required
            >

            <label>
                Password
            </label>

            <input
                type="password"
                name="password"
                placeholder="Create a password"
                required
            >

            <button class="btn btn-primary">
                Create Account
            </button>

        </form>

    </div>

    """

    return page("Register", content)


# ============================================================
# LOGIN
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"].strip().lower()
        password = request.form["password"]

        conn = get_db()

        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE email=?
            AND password=?
            """,
            (
                email,
                hash_password(password)
            )
        ).fetchone()

        conn.close()

        if user:

            session["user_id"] = user["id"]

            if user["role"] == "admin":
                return redirect("/admin")

            return redirect("/dashboard")

        return page(
            "Login",
            """
            <div class="alert danger">
                Incorrect email or password.
            </div>

            <a href="/login"
               class="btn btn-primary">
                Try Again
            </a>
            """
        )

    content = """

    <div class="card form-card">

        <h1>
            Login
        </h1>

        <form method="POST">

            <label>
                Email
            </label>

            <input
                type="email"
                name="email"
                required
            >

            <label>
                Password
            </label>

            <input
                type="password"
                name="password"
                required
            >

            <button class="btn btn-primary">
                Login
            </button>

        </form>

        <hr>

        <p>
            <strong>Demo admin:</strong><br>
            admin@smartseva.local<br>
            admin123
        </p>

    </div>

    """

    return page("Login", content)


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect("/")


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/dashboard")
def dashboard():

    user = current_user()

    if not user:
        return redirect("/login")

    hours = calculate_hours(user["id"])

    goal = user["goal"]

    if goal > 0:
        progress = min(100, (hours / goal) * 100)
    else:
        progress = 0

    conn = get_db()

    signups = conn.execute(
        """
        SELECT
            signups.*,
            seva.title,
            seva.location,
            seva.date,
            seva.start_time,
            seva.end_time
        FROM signups
        JOIN seva
        ON signups.seva_id=seva.id
        WHERE signups.user_id=?
        ORDER BY signups.id DESC
        """,
        (user["id"],)
    ).fetchall()

    certificate = conn.execute(
        """
        SELECT *
        FROM certificates
        WHERE user_id=?
        """,
        (user["id"],)
    ).fetchone()

    conn.close()

    certificate_button = ""

    if certificate:

        certificate_button = f"""
        <a href="/certificate/{certificate['certificate_id']}"
           class="btn btn-gold">
            🏆 View Certificate
        </a>
        """

    content = f"""

    <div style="margin-top:50px">

        <h1>
            Welcome, {user['name']} 👋
        </h1>

        <p>
            Your seva journey starts here.
        </p>

    </div>


    <div class="card">

        <h2>
            🎯 My Service Goal
        </h2>

        <div class="big-number">
            {hours}
            <span style="font-size:20px">
                / {goal} hours
            </span>
        </div>

        <br>

        <div class="progress">

            <div class="progress-bar"
                 style="width:{progress}%">

                {round(progress)}%

            </div>

        </div>

        <br>

        <form method="POST"
              action="/set-goal">

            <label>
                Change Goal
            </label>

            <input
                type="number"
                name="goal"
                value="{goal}"
                min="1"
                step="1"
                style="max-width:200px"
            >

            <button class="btn btn-primary">
                Update Goal
            </button>

        </form>

    </div>
    """

    if hours >= goal:

        content += f"""

        <div class="card success">

            <h2>
                🎉 Goal Complete!
            </h2>

            <p>
                You have completed {hours} verified
                hours of seva.
            </p>

            {certificate_button}

        </div>

        """

    else:

        remaining = round(goal - hours, 2)

        content += f"""

        <div class="card">

            <h2>
                {remaining} hours remaining
            </h2>

            <a href="/seva"
               class="btn btn-primary">
                Find Seva
            </a>

        </div>

        """

    content += """

    <h2>
        My Seva
    </h2>
    """

    if not signups:

        content += """

        <div class="card">

            <p>
                You haven't signed up for seva yet.
            </p>

            <a href="/seva"
               class="btn btn-primary">
                Find Seva
            </a>

        </div>

        """

    for item in signups:

        if item["verified"]:

            status = """
            <span class="badge verified">
                ✓ VERIFIED
            </span>
            """

        elif item["check_out"]:

            status = """
            <span class="badge pending">
                Waiting for verification
            </span>
            """

        elif item["check_in"]:

            status = """
            <span class="badge"
                  style="background:#cfe2ff">
                Checked In
            </span>
            """

        else:

            status = """
            <span class="badge"
                  style="background:#eee">
                Not Started
            </span>
            """

        buttons = ""

        if not item["check_in"]:

            buttons += f"""

            <form method="POST"
                  action="/check-in/{item['id']}"
                  style="display:inline">

                <button class="btn btn-green">
                    Check In
                </button>

            </form>

            """

        elif not item["check_out"]:

            buttons += f"""

            <form method="POST"
                  action="/check-out/{item['id']}"
                  style="display:inline">

                <button class="btn btn-red">
                    Check Out
                </button>

            </form>

            """

        content += f"""

        <div class="card">

            <h2>
                {item['title']}
            </h2>

            <p>
                📍 {item['location']}
            </p>

            <p>
                📅 {item['date']}
            </p>

            <p>
                ⏰ {item['start_time']}
                -
                {item['end_time']}
            </p>

            <p>
                {status}
            </p>

            <div>
                {buttons}
            </div>

            <p>
                <strong>
                    Recorded hours:
                    {item['hours']}
                </strong>
            </p>

        </div>

        """

    return page("Dashboard", content)


# ============================================================
# SET GOAL
# ============================================================

@app.route("/set-goal", methods=["POST"])
def set_goal():

    user = current_user()

    if not user:
        return redirect("/login")

    try:
        goal = float(request.form["goal"])

        if goal <= 0:
            raise ValueError

    except ValueError:

        return redirect("/dashboard")

    conn = get_db()

    conn.execute(
        "UPDATE users SET goal=? WHERE id=?",
        (goal, user["id"])
    )

    conn.commit()
    conn.close()

    return redirect("/dashboard")


# ============================================================
# SEVA PAGE
# ============================================================

@app.route("/seva")
def seva():

    user = current_user()

    if not user:
        return redirect("/login")

    conn = get_db()

    opportunities = conn.execute(
        """
        SELECT
            seva.*,
            COUNT(signups.id) AS filled
        FROM seva
        LEFT JOIN signups
        ON seva.id=signups.seva_id
        GROUP BY seva.id
        ORDER BY seva.id DESC
        """
    ).fetchall()

    conn.close()

    content = """

    <div style="margin-top:50px">

        <h1>
            Find Seva 🙏
        </h1>

        <p>
            Choose an opportunity that works for you.
        </p>

    </div>

    <div class="grid">
    """

    for item in opportunities:

        spots_left = item["spots"] - item["filled"]

        if spots_left > 0:

            button = f"""

            <form method="POST"
                  action="/signup/{item['id']}">

                <button class="btn btn-primary">
                    Sign Up
                </button>

            </form>

            """

        else:

            button = """

            <button class="btn btn-gray"
                    disabled>
                Full
            </button>

            """

        content += f"""

        <div class="card">

            <h2>
                {item['title']}
            </h2>

            <p>
                {item['description']}
            </p>

            <hr>

            <p>
                📍 {item['location']}
            </p>

            <p>
                📅 {item['date']}
            </p>

            <p>
                ⏰ {item['start_time']}
                -
                {item['end_time']}
            </p>

            <p>
                👥 {item['filled']} /
                {item['spots']} volunteers
            </p>

            <p>
                <strong>
                    {spots_left} spots remaining
                </strong>
            </p>

            {button}

        </div>

        """

    content += "</div>"

    return page("Find Seva", content)


# ============================================================
# SIGN UP
# ============================================================

@app.route("/signup/<int:seva_id>", methods=["POST"])
def signup(seva_id):

    user = current_user()

    if not user:
        return redirect("/login")

    conn = get_db()

    existing = conn.execute(
        """
        SELECT *
        FROM signups
        WHERE user_id=?
        AND seva_id=?
        """,
        (user["id"], seva_id)
    ).fetchone()

    if existing:

        conn.close()

        return redirect("/seva")

    seva_item = conn.execute(
        "SELECT * FROM seva WHERE id=?",
        (seva_id,)
    ).fetchone()

    if not seva_item:

        conn.close()

        return redirect("/seva")

    filled = conn.execute(
        """
        SELECT COUNT(*)
        FROM signups
        WHERE seva_id=?
        """,
        (seva_id,)
    ).fetchone()[0]

    if filled >= seva_item["spots"]:

        conn.close()

        return redirect("/seva")

    conn.execute(
        """
        INSERT INTO signups
        (user_id,seva_id)
        VALUES (?,?)
        """,
        (user["id"], seva_id)
    )

    conn.commit()
    conn.close()

    return redirect("/dashboard")


# ============================================================
# CHECK IN
# ============================================================

@app.route("/check-in/<int:signup_id>", methods=["POST"])
def check_in(signup_id):

    user = current_user()

    if not user:
        return redirect("/login")

    conn = get_db()

    signup_item = conn.execute(
        """
        SELECT *
        FROM signups
        WHERE id=?
        AND user_id=?
        """,
        (signup_id, user["id"])
    ).fetchone()

    if signup_item and not signup_item["check_in"]:

        now = datetime.now().isoformat()

        conn.execute(
            """
            UPDATE signups
            SET check_in=?
            WHERE id=?
            """,
            (now, signup_id)
        )

        conn.commit()

    conn.close()

    return redirect("/dashboard")


# ============================================================
# CHECK OUT
# ============================================================

@app.route("/check-out/<int:signup_id>", methods=["POST"])
def check_out(signup_id):

    user = current_user()

    if not user:
        return redirect("/login")

    conn = get_db()

    signup_item = conn.execute(
        """
        SELECT *
        FROM signups
        WHERE id=?
        AND user_id=?
        """,
        (signup_id, user["id"])
    ).fetchone()

    if signup_item:

        if signup_item["check_in"] and not signup_item["check_out"]:

            start = datetime.fromisoformat(
                signup_item["check_in"]
            )

            end = datetime.now()

            hours = round(
                (end - start).total_seconds() / 3600,
                2
            )

            conn.execute(
                """
                UPDATE signups

                SET check_out=?,
                    hours=?

                WHERE id=?
                """,
                (
                    end.isoformat(),
                    hours,
                    signup_id
                )
            )

            conn.commit()

    conn.close()

    return redirect("/dashboard")


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route("/admin")
def admin():

    user = current_user()

    if not user or user["role"] != "admin":
        return redirect("/")

    conn = get_db()

    pending = conn.execute(
        """
        SELECT
            signups.*,
            users.name,
            users.email,
            seva.title
        FROM signups

        JOIN users
        ON signups.user_id=users.id

        JOIN seva
        ON signups.seva_id=seva.id

        WHERE signups.check_out IS NOT NULL
        AND signups.verified=0

        ORDER BY signups.id DESC
        """
    ).fetchall()

    students = conn.execute(
        """
        SELECT *
        FROM users
        WHERE role='student'
        ORDER BY name
        """
    ).fetchall()

    conn.close()

    content = """

    <div style="margin-top:50px">

        <h1>
            Gurdwara Admin Dashboard
        </h1>

        <p>
            Manage seva and verify student service hours.
        </p>

    </div>


    <div class="card">

        <h2>
            Create Seva Opportunity
        </h2>

        <form method="POST"
              action="/admin/create-seva">

            <label>
                Seva Name
            </label>

            <input
                name="title"
                placeholder="Langar Serving"
                required
            >

            <label>
                Description
            </label>

            <textarea
                name="description"
                required
            ></textarea>

            <label>
                Location
            </label>

            <input
                name="location"
                placeholder="Langar Hall"
                required
            >

            <label>
                Date
            </label>

            <input
                name="date"
                placeholder="Saturday"
                required
            >

            <label>
                Start Time
            </label>

            <input
                name="start_time"
                placeholder="11:00 AM"
                required
            >

            <label>
                End Time
            </label>

            <input
                name="end_time"
                placeholder="1:00 PM"
                required
            >

            <label>
                Volunteer Spots
            </label>

            <input
                type="number"
                name="spots"
                min="1"
                required
            >

            <button class="btn btn-primary">
                Create Seva
            </button>

        </form>

    </div>


    <h2>
        Pending Verification
    </h2>
    """

    if not pending:

        content += """

        <div class="card success">

            ✓ No sessions are waiting for verification.

        </div>

        """

    else:

        for item in pending:

            content += f"""

            <div class="card">

                <h2>
                    {item['name']}
                </h2>

                <p>
                    <strong>
                        {item['title']}
                    </strong>
                </p>

                <p>
                    Service time:
                    {item['hours']} hours
                </p>

                <form method="POST"
                      action="/admin/verify/{item['id']}">

                    <button class="btn btn-green">
                        ✓ Verify Hours
                    </button>

                </form>

            </div>

            """

    content += """

    <h2>
        Students
    </h2>

    <div class="card">

        <table>

            <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Goal</th>
                <th>Verified Hours</th>
            </tr>
    """

    for student in students:

        hours = calculate_hours(
            student["id"]
        )

        content += f"""

            <tr>

                <td>
                    {student['name']}
                </td>

                <td>
                    {student['email']}
                </td>

                <td>
                    {student['goal']}
                </td>

                <td>
                    {hours}
                </td>

            </tr>

        """

    content += """

        </table>

    </div>

    """

    return page("Admin", content)


# ============================================================
# CREATE SEVA FROM ADMIN
# ============================================================

@app.route("/admin/create-seva", methods=["POST"])
def create_seva():

    user = current_user()

    if not user or user["role"] != "admin":
        return redirect("/")

    conn = get_db()

    conn.execute(
        """
        INSERT INTO seva
        (
            title,
            description,
            location,
            date,
            start_time,
            end_time,
            spots
        )

        VALUES (?,?,?,?,?,?,?)
        """,
        (
            request.form["title"],
            request.form["description"],
            request.form["location"],
            request.form["date"],
            request.form["start_time"],
            request.form["end_time"],
            int(request.form["spots"])
        )
    )

    conn.commit()
    conn.close()

    return redirect("/admin")


# ============================================================
# VERIFY HOURS
# ============================================================

@app.route("/admin/verify/<int:signup_id>", methods=["POST"])
def verify_hours(signup_id):

    user = current_user()

    if not user or user["role"] != "admin":
        return redirect("/")

    conn = get_db()

    signup_item = conn.execute(
        "SELECT * FROM signups WHERE id=?",
        (signup_id,)
    ).fetchone()

    if signup_item:

        conn.execute(
            """
            UPDATE signups
            SET verified=1
            WHERE id=?
            """,
            (signup_id,)
        )

        conn.commit()

        # Check if student has reached their goal
        student = conn.execute(
            """
            SELECT *
            FROM users
            WHERE id=?
            """,
            (signup_item["user_id"],)
        ).fetchone()

        conn.close()

        if student:

            hours = calculate_hours(
                student["id"]
            )

            if hours >= student["goal"]:

                create_certificate(
                    student["id"],
                    hours
                )

        return redirect("/admin")

    conn.close()

    return redirect("/admin")


# ============================================================
# CERTIFICATE CREATION
# ============================================================

def create_certificate(user_id, hours):

    conn = get_db()

    existing = conn.execute(
        """
        SELECT *
        FROM certificates
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    if existing:

        conn.close()

        return existing["certificate_id"]

    certificate_id = (
        "SS-"
        + datetime.now().strftime("%Y")
        + "-"
        + secrets.token_hex(4).upper()
    )

    issued = datetime.now().strftime(
        "%B %d, %Y"
    )

    conn.execute(
        """
        INSERT INTO certificates
        (user_id,certificate_id,hours,issued_at)
        VALUES (?,?,?,?)
        """,
        (
            user_id,
            certificate_id,
            hours,
            issued
        )
    )

    conn.commit()
    conn.close()

    return certificate_id


# ============================================================
# CERTIFICATE PAGE
# ============================================================

@app.route("/certificate/<certificate_id>")
def certificate(certificate_id):

    conn = get_db()

    cert = conn.execute(
        """
        SELECT
            certificates.*,
            users.name
        FROM certificates

        JOIN users
        ON certificates.user_id=users.id

        WHERE certificates.certificate_id=?
        """,
        (certificate_id,)
    ).fetchone()

    conn.close()

    if not cert:

        return page(
            "Certificate",
            """
            <div class="card">

                <h1>
                    Certificate Not Found
                </h1>

                <p>
                    This certificate could not be verified.
                </p>

            </div>
            """
        )

    content = f"""

    <div class="certificate">

        <div style="font-size:60px">
            🙏
        </div>

        <h1>
            Certificate of Seva
        </h1>

        <p>
            This certificate recognizes
        </p>

        <div class="name">
            {cert['name']}
        </div>

        <p>
            for completing
        </p>

        <h2>
            {cert['hours']:.2f} Verified Hours
        </h2>

        <p>
            of community seva through Smart Seva.
        </p>

        <br>

        <p>
            <strong>
                Certificate ID
            </strong>
        </p>

        <p>
            {cert['certificate_id']}
        </p>

        <p>
            Issued: {cert['issued_at']}
        </p>

        <br>

        <div class="card">

            <strong>
                ✓ VERIFIABLE CERTIFICATE
            </strong>

            <p>
                Certificate ID:
                {cert['certificate_id']}
            </p>

            <a
                href="/certificate/{cert['certificate_id']}/pdf"
                class="btn btn-primary"
            >
                Download PDF
            </a>

        </div>

    </div>

    """

    return page(
        "Certificate",
        content
    )


# ============================================================
# PDF CERTIFICATE
# ============================================================

@app.route("/certificate/<certificate_id>/pdf")
def certificate_pdf(certificate_id):

    conn = get_db()

    cert = conn.execute(
        """
        SELECT
            certificates.*,
            users.name
        FROM certificates

        JOIN users
        ON certificates.user_id=users.id

        WHERE certificates.certificate_id=?
        """,
        (certificate_id,)
    ).fetchone()

    conn.close()

    if not cert:
        return "Certificate not found", 404

    # Try ReportLab
    try:

        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.colors import HexColor

        buffer = io.BytesIO()

        pdf = canvas.Canvas(
            buffer,
            pagesize=letter
        )

        width, height = letter

        # Background
        pdf.setFillColor(
            HexColor("#fffdf5")
        )

        pdf.rect(
            0,
            0,
            width,
            height,
            fill=True,
            stroke=False
        )

        # Gold border
        pdf.setStrokeColor(
            HexColor("#d4a72c")
        )

        pdf.setLineWidth(5)

        pdf.rect(
            40,
            40,
            width - 80,
            height - 80,
            fill=False
        )

        # Title
        pdf.setFillColor(
            HexColor("#741d1d")
        )

        pdf.setFont(
            "Helvetica-Bold",
            32
        )

        pdf.drawCentredString(
            width / 2,
            height - 130,
            "CERTIFICATE OF SEVA"
        )

        pdf.setFillColor(
            HexColor("#222222")
        )

        pdf.setFont(
            "Helvetica",
            15
        )

        pdf.drawCentredString(
            width / 2,
            height - 170,
            "Presented in recognition of community service"
        )

        pdf.setFont(
            "Helvetica-Bold",
            27
        )

        pdf.drawCentredString(
            width / 2,
            height - 240,
            cert["name"]
        )

        pdf.setFont(
            "Helvetica",
            16
        )

        pdf.drawCentredString(
            width / 2,
            height - 285,
            f"has completed {cert['hours']:.2f} verified hours of seva"
        )

        pdf.setFont(
            "Helvetica",
            11
        )

        pdf.drawCentredString(
            width / 2,
            height - 330,
            "Smart Seva Community Service Program"
        )

        pdf.drawString(
            70,
            80,
            "Certificate ID: "
            + cert["certificate_id"]
        )

        pdf.drawRightString(
            width - 70,
            80,
            cert["issued_at"]
        )

        pdf.save()

        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name="smart_seva_certificate.pdf",
            mimetype="application/pdf"
        )

    except ImportError:

        return """
        ReportLab is not installed.

        Run:

        pip install reportlab

        then try again.
        """


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    init_db()

    print("")
    print("======================================")
    print("🙏 SMART SEVA IS RUNNING")
    print("======================================")
    print("")
    print("Open this in your browser:")
    print("http://127.0.0.1:5000")
    print("")
    print("ADMIN LOGIN")
    print("Email: admin@smartseva.local")
    print("Password: admin123")
    print("")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )


