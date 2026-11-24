import copy
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from functools import wraps

import requests
from urllib.parse import quote_plus

from flask import Flask, request, session, redirect, url_for, render_template, flash, Response, g, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import state_repo
import db


def _load_dotenv(path=".env"):
    """Zero-dependency local-dev loader, mirroring config.py at the repo root.
    Never overrides a real env var, so Vercel's injected values always win."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB, generous for a CV PDF
# Forms have no CSRF tokens; Lax keeps the session cookie off cross-site POSTs.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

ALLOWED_EMAIL = os.environ.get("ALLOWED_EMAIL", "ashertettehabotsi@gmail.com").strip().lower()
COMPANY_TYPES = ["fintech", "consultancy", "isp", "bank", "general_startup"]
STATUSES = ["Pending", "Approved", "Review", "Skipped", "Sent", "Followup-Sent", "Failed", "Rejected"]
RESET_TOKEN_MAX_AGE = 3600  # 1 hour

# Mirrors the pipeline: outreach_pipeline.DAILY_SEND_LIMIT and the two workflows' cron.
DAILY_SEND_LIMIT = 15
SCRAPE_WORKFLOW = "scrape-pipeline.yml"
SEND_WORKFLOW = "send-pipeline.yml"
SEND_HOUR_UTC, SEND_MINUTE = 7, 23
SEND_WEEKDAYS = {0, 1, 2, 3, 4}  # Mon-Fri, as in send-pipeline.yml's "23 7 * * 1-5"
SCRAPE_EVERY_HOURS, SCRAPE_MINUTE = 6, 17
SEARCHES_PER_RUN = 2  # outreach_pipeline.QUERIES_PER_RUN

DEFAULT_RECIPIENT = "The Recruiting and Technology Team"
DEFAULT_ADDRESS = "Accra, Ghana"

# The shipped searches, shown until you save your own. outreach_pipeline.py
# reads this same file, so what you see here is what the scrape runs.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_queries_default.txt"),
          encoding="utf-8") as _f:
    DEFAULT_QUERIES_TEXT = _f.read()

# What an invited user sees before saving searches of their own: instructions
# only. The owner's searches (and roles) are never a member's starting point.
MEMBER_QUERIES_TEMPLATE = """# One search per line: "search | how many results (1-25) | role".
# Describe the companies you want: industry, location, what they do. The role
# (a short name from your Roles page) is the starting guess for what they find.
# Example:
#   fintech startups in Lagos with in-house engineering teams | 10 | frontend
# Lines starting with # are ignored. Nothing is searched until you add a line.
"""


def default_queries_text():
    return MEMBER_QUERIES_TEMPLATE if state_repo.current_tenant() else DEFAULT_QUERIES_TEXT

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --- Auth ----------------------------------------------------------------

def serializer():
    return URLSafeTimedSerializer(app.secret_key, salt="password-reset")


def is_owner():
    """The owner: their own login, their data at the state repo root. Sessions
    from before invited users existed carry no role and are the owner's."""
    return bool(session.get("authed")) and session.get("role", "owner") == "owner"


@app.before_request
def bind_user():
    """
    Point every storage call in this request at the signed-in user's data.
    Reset first, on every request, so nothing carries over between requests on
    the same worker. A member removed by the owner is signed out here, on
    their very next request.
    """
    state_repo.set_tenant("")
    g.member = None
    if not session.get("authed") or session.get("role", "owner") == "owner":
        return None
    member = db.get_member(session.get("email", ""))
    if not member or member["tenant"] != session.get("tenant"):
        session.clear()
        flash("Your access to this dashboard has ended.", "error")
        return redirect(url_for("login"))
    state_repo.set_tenant(member["tenant"])
    g.member = member
    return None


@app.teardown_request
def unbind_user(exc=None):
    state_repo.set_tenant("")


def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_owner():
            flash("Only the dashboard's owner can open that page.", "error")
            return redirect(url_for("overview"))
        return view(*args, **kwargs)
    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            # Keep where you were going (e.g. a Fix link from the review
            # email), so login lands there instead of on the overview.
            nxt = request.full_path if request.method == "GET" else ""
            return redirect(url_for("login", next=nxt.rstrip("?")) if nxt else url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def send_email(to_addr, subject, body):
    sender = os.environ["SENDER_EMAIL"]
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=20) as smtp:
        smtp.ehlo()
        # PLAIN only: smtp.login() tries each mechanism, and after Gmail rejects
        # a bad password it hangs up, hiding the 535 behind "Connection
        # unexpectedly closed".
        smtp.user, smtp.password = sender, password
        smtp.auth("PLAIN", smtp.auth_plain)
        smtp.sendmail(sender, to_addr, msg.as_string())


@app.route("/setup", methods=["GET", "POST"])
def setup():
    auth = db.load_auth()
    if auth.get("password_hash"):
        return redirect(url_for("login"))

    if request.method == "POST":
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw) < 10:
            flash("Password must be at least 10 characters.", "error")
        elif pw != pw2:
            flash("Passwords don't match.", "error")
        else:
            db.save_auth(generate_password_hash(pw), datetime.now(timezone.utc))
            flash("Password set. Log in below.", "success")
            return redirect(url_for("login"))
    return render_template("setup.html", email=ALLOWED_EMAIL)


@app.route("/login", methods=["GET", "POST"])
def login():
    auth = db.load_auth()
    if not auth.get("password_hash"):
        return redirect(url_for("setup"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if email == ALLOWED_EMAIL and check_password_hash(auth["password_hash"], pw):
            session.clear()
            session.update({"authed": True, "role": "owner", "email": email, "tenant": ""})
            return safe_next("overview")
        member = db.get_member(email) if email != ALLOWED_EMAIL else None
        if member and check_password_hash(member["password_hash"], pw):
            session.clear()
            session.update({"authed": True, "role": "member", "email": email, "tenant": member["tenant"]})
            return safe_next("overview")
        flash("Incorrect email or password.", "error")
    return render_template("login.html", email="")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        # Gmail is only used for this email, so it's optional to configure;
        # say so plainly rather than crashing on the missing variable.
        if not os.environ.get("SENDER_EMAIL") or not os.environ.get("GMAIL_APP_PASSWORD"):
            flash("Password reset isn't set up: add SENDER_EMAIL and GMAIL_APP_PASSWORD to the "
                  "dashboard's environment variables, then try again.", "error")
            return redirect(url_for("forgot"))
        email = request.form.get("email", "").strip().lower()
        current = current_hash_for(email)
        if current:
            token = serializer().dumps({"e": email, "h": current[:16]})
            link = request.url_root.rstrip("/") + url_for("reset_password", token=token)
            try:
                send_email(
                    email,
                    "Reset your Outbound password",
                    f"Reset your password here (expires in 1 hour):\n\n{link}\n\n"
                    f"If you didn't request this, ignore this email.",
                )
            except smtplib.SMTPAuthenticationError:
                flash("Couldn't send the reset email: Gmail rejected the login. Update GMAIL_APP_PASSWORD "
                      "in the dashboard's environment with a new app password.", "error")
                return redirect(url_for("forgot"))
            except (smtplib.SMTPException, OSError):
                flash("Couldn't reach Gmail to send the reset email. Try again in a minute.", "error")
                return redirect(url_for("forgot"))
        flash("If that email has an account, a reset link was just sent.", "success")
        return redirect(url_for("login"))
    return render_template("forgot.html")


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    auth = db.load_auth()
    try:
        payload = serializer().loads(token, max_age=RESET_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        flash("That reset link is invalid or has expired.", "error")
        return redirect(url_for("forgot"))

    email = payload.get("e") or ALLOWED_EMAIL  # links from before invited users existed
    current = current_hash_for(email)
    if not current or payload.get("h") != current[:16]:
        flash("That reset link has already been used.", "error")
        return redirect(url_for("forgot"))

    if request.method == "POST":
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if len(pw) < 10:
            flash("Password must be at least 10 characters.", "error")
        elif pw != pw2:
            flash("Passwords don't match.", "error")
        else:
            if email == ALLOWED_EMAIL:
                db.save_auth(generate_password_hash(pw), datetime.now(timezone.utc))
            else:
                db.set_member_password(email, generate_password_hash(pw), datetime.now(timezone.utc).isoformat())
            flash("Password updated. Log in below.", "success")
            return redirect(url_for("login"))
    return render_template("reset.html")


def current_hash_for(email):
    """The password hash for the owner or a member, or None for anyone else."""
    if email == ALLOWED_EMAIL:
        return db.load_auth().get("password_hash")
    member = db.get_member(email)
    return member["password_hash"] if member else None


# --- Sign-up (invited users only) ---------------------------------------------

def tenant_id_for(email):
    """A folder name for a new member: readable, unique, path-safe."""
    import hashlib
    local = re.sub(r"[^a-z0-9]+", "-", email.split("@")[0].lower()).strip("-")[:24] or "user"
    return f"{local}-{hashlib.sha1(email.encode()).hexdigest()[:6]}"


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("authed"):
        return redirect(url_for("overview"))
    form = request.form if request.method == "POST" else {"email": request.args.get("email", "")}
    if request.method == "POST":
        email = form.get("email", "").strip().lower()
        pw, pw2 = form.get("password", ""), form.get("password2", "")
        if email == ALLOWED_EMAIL:
            return redirect(url_for("login"))
        if not EMAIL_RE.match(email) or not db.is_invited(email):
            flash("That email hasn't been invited. Ask whoever runs this dashboard to add you.", "error")
        elif db.get_member(email):
            flash("You already have an account. Log in below.", "success")
            return redirect(url_for("login"))
        elif len(pw) < 10:
            flash("Password must be at least 10 characters.", "error")
        elif pw != pw2:
            flash("Passwords don't match.", "error")
        else:
            tenant = tenant_id_for(email)
            db.create_member(email, tenant, generate_password_hash(pw), datetime.now(timezone.utc).isoformat())
            session.clear()
            session.update({"authed": True, "role": "member", "email": email, "tenant": tenant})
            # Someone whose access was removed and who has been invited back
            # gets the same workspace (the folder is derived from their email),
            # with everything they had. Being invited again is the owner's say-so
            # to resume, so lift the pause that removal put on their pipeline.
            state_repo.set_tenant(tenant)
            prof = state_repo.get_profile()
            if prof.get("paused"):
                prof.pop("paused")
                state_repo.save_profile(prof, "Invited back; pipeline resumed")
                flash("Welcome back! Your workspace is as you left it, and your pipeline is running again.",
                      "success")
                return redirect(url_for("overview"))
            flash("Welcome! Start by filling in your profile: it's what your applications are signed with.",
                  "success")
            return redirect(url_for("profile"))
    return render_template("signup.html", form=form)


# --- Queue helpers ------------------------------------------------------------

def is_yes(value):
    return str(value or "").strip().lower() == "yes"


def has_email(c):
    return bool(c.get("email")) and c["email"] != "no_email_found"


def current_tracks():
    """(tracks, saved), read once per request: every page needs it for the buckets."""
    if "tracks" not in g:
        g.tracks = state_repo.get_tracks()
    return g.tracks


def role_missing(c):
    """The row's role was removed (or never existed), so it can't be sent as-is."""
    return bool(c.get("track")) and c["track"] not in current_tracks()[0]


def checkin_due(c, today=None):
    """Mirrors outreach_pipeline.checkin_due: said "not now", and the check-back date has come."""
    today = today or utcnow().strftime("%Y-%m-%d")
    return (c.get("outcome") == "later" and not c.get("checkback_status")
            and bool(c.get("checkback_date")) and c["checkback_date"] <= today)


def bucket_of(c):
    """Which page a row belongs on, in terms of what (if anything) it needs from you."""
    s = c["status"]
    if s in ("Sent", "Followup-Sent") and checkin_due(c):
        return "attention"
    if s in ("Review", "Failed") or (s == "Skipped" and not has_email(c)):
        return "attention"
    if s in ("Pending", "Approved") and (is_yes(c.get("role_review")) or role_missing(c)):
        return "attention"
    if s in ("Pending", "Approved") and has_email(c):
        return "upcoming"
    if s == "Pending":
        return "scraping"
    if s in ("Sent", "Followup-Sent"):
        return "sent"
    if s == "Rejected":
        return "rejected"
    return "other"


def attention_reason(c):
    if c["status"] in ("Sent", "Followup-Sent"):
        return "checkback"
    if c["status"] == "Review":
        return "name"
    if c["status"] == "Failed":
        return "failed"
    if c["status"] == "Skipped":
        return "email"
    return "role"


def load_data():
    """Every logged-in page needs the queue (for the nav counts), so load it once here."""
    companies = state_repo.load_queue()
    for c in companies:
        c["slug"] = state_repo.slugify(c["company_name"])
        c["bucket"] = bucket_of(c)
        c["host"] = state_repo.host_of(c["website"])
    nav = {
        "attention": sum(1 for c in companies if c["bucket"] == "attention"),
        "upcoming": sum(1 for c in companies if c["bucket"] == "upcoming"),
        "sent_open": sum(
            1 for c in companies
            if c["bucket"] == "sent" and not is_yes(c["replied"]) and not is_yes(c["bounced"])
        ),
        "total": len(companies),
    }
    # The command palette (in every page's shell) searches these client-side.
    g.palette = [{"n": c["company_name"], "s": c["slug"], "st": c["status"]} for c in companies]
    return companies, nav


def member_paused():
    """True when the signed-in member's pipeline has been paused by the owner (read once per request)."""
    if not getattr(g, "member", None):
        return False
    if "member_paused" not in g:
        g.member_paused = bool(safe_call(state_repo.get_profile, fallback={}).get("paused"))
    return g.member_paused


def set_member_paused(tenant, paused, why):
    """Pause or resume a member's pipeline. Their sign-in and data are untouched; tenants.py skips paused users."""
    state_repo.set_tenant(tenant)
    try:
        prof = state_repo.get_profile()
        if bool(prof.get("paused")) == paused:
            return False
        if paused:
            prof["paused"] = True
        else:
            prof.pop("paused", None)
        state_repo.save_profile(prof, why)
        return True
    finally:
        state_repo.set_tenant("")


@app.context_processor
def shell_context():
    now = utcnow()
    return {
        "is_member": bool(getattr(g, "member", None)),
        "member_paused": member_paused(),
        "is_owner_user": is_owner(),
        "palette": getattr(g, "palette", []),
        "shell_next_send": next_send_run(now),
        "shell_next_scrape": next_scrape_run(now),
    }


def wants_json():
    """The triage page saves in place with fetch(); plain form posts still redirect."""
    return request.headers.get("Accept", "").startswith("application/json")


def respond(ok, message, default_endpoint, **extra):
    if wants_json():
        return jsonify(ok=ok, message=message, **extra), (200 if ok else 400)
    flash(message, "success" if ok else "error")
    return safe_next(default_endpoint)


# Labels that never identify the company in a hostname ("careers.acme.com.gh").
COMMON_TLDS = {"com", "net", "org", "io", "co", "gh", "africa", "tech", "ng", "uk", "ke", "za",
               "biz", "info", "app", "dev", "ai", "edu", "gov"}
SUBDOMAIN_NOISE = {"careers", "jobs", "about", "en", "blog", "app", "portal", "web"}


def email_domain(host):
    parts = [p for p in (host or "").split(".") if p]
    if len(parts) > 2 and parts[0] in SUBDOMAIN_NOISE:
        parts = parts[1:]
    return ".".join(parts)


def name_from_host(host):
    parts = [p for p in (host or "").split(".") if p]
    while len(parts) > 1 and parts[-1] in COMMON_TLDS:
        parts.pop()
    if not parts:
        return ""
    words = [w for w in re.split(r"[-_]+", parts[-1]) if w]
    return " ".join(w if any(ch.isdigit() for ch in w) else w.capitalize() for w in words)


def triage_item(c, tracks=()):
    """Everything the Needs you page shows for one row, including suggested fixes."""
    reason = attention_reason(c)
    host = c["host"]
    domain = email_domain(host)
    current = c["email"] if has_email(c) else ""
    guess = name_from_host(host)
    return {
        "slug": c["slug"],
        "name": c["company_name"],
        "website": c["website"],
        "host": host,
        "email": current,
        "reason": reason,
        "type": c["company_type"],
        "track": c["track"] if c["track"] in tracks else (tracks[0] if tracks else ""),
        "tracks": list(tracks),
        "email_note": c.get("email_note", ""),
        "role_reason": (f"its role '{c['track']}' was removed" if role_missing(c)
                        else c.get("role_reason", "")),
        "added": c["date_added"],
        "name_suggestion": guess if guess and guess.lower() != c["company_name"].strip().lower() else "",
        "email_suggestions": [e for e in (f"{p}@{domain}" for p in ("careers", "hr", "jobs", "info")) if e != current] if domain else [],
        "google": "https://www.google.com/search?q=" + quote_plus(f'"{domain or c["company_name"]}" email careers OR hr OR contact'),
        "linkedin": "https://www.linkedin.com/search/results/companies/?keywords=" + quote_plus(guess or c["company_name"]),
    }


def safe_next(default_endpoint):
    """Redirect back to the page a form came from, but only to a path on this site."""
    nxt = request.form.get("next") or request.args.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return redirect(nxt)
    return redirect(url_for(default_endpoint))


def clean_email(value):
    value = (value or "").strip()
    if value and not EMAIL_RE.match(value):
        raise ValueError(f"'{value}' doesn't look like an email address.")
    return value


def clean_website(value):
    value = (value or "").strip()
    if value and not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


# --- Time helpers ---------------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc)


def next_scrape_run(now):
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slot = start_of_day + timedelta(hours=(now.hour // SCRAPE_EVERY_HOURS) * SCRAPE_EVERY_HOURS,
                                    minutes=SCRAPE_MINUTE)
    return slot if slot > now else slot + timedelta(hours=SCRAPE_EVERY_HOURS)


def next_send_run(now):
    run = now.replace(hour=SEND_HOUR_UTC, minute=SEND_MINUTE, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    while run.weekday() not in SEND_WEEKDAYS:
        run += timedelta(days=1)
    return run


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    value = value.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.fromisoformat(value) if fmt is None else datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@app.template_filter("when")
def when_filter(value):
    """'Today 09:00', 'Tomorrow 00:00', or 'Mon 3 Oct 09:00' (all UTC = Accra time)."""
    dt = _parse_dt(value)
    if not dt:
        return ""
    now = utcnow()
    days = (dt.date() - now.date()).days
    hhmm = dt.strftime("%H:%M")
    if days == 0:
        return f"Today {hhmm}"
    if days == 1:
        return f"Tomorrow {hhmm}"
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')} {hhmm}"


@app.template_filter("ago")
def ago_filter(value):
    dt = _parse_dt(value)
    if not dt:
        return ""
    secs = int((utcnow() - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    days = secs // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


@app.template_filter("day")
def day_filter(value):
    return (value or "").split(" ")[0]


@app.template_filter("longdate")
def longdate_filter(value):
    """'2026-11-23...' -> '23 Nov 2026'; anything unparseable is shown as is."""
    try:
        return datetime.strptime((value or "")[:10], "%Y-%m-%d").strftime("%-d %b %Y")
    except ValueError:
        return value or ""


@app.template_filter("commit_label")
def commit_label_filter(message):
    """CI commits end in their own timestamp; the feed already shows 'x ago'."""
    return re.sub(r":\s*\d{4}-\d{2}-\d{2}T[\d:]+Z?$", "", message or "")


def safe_call(fn, *args, fallback=None):
    """Pipeline status and history are nice-to-have; never let them break a page."""
    try:
        return fn(*args)
    except Exception:
        return fallback


# --- Error handling ------------------------------------------------------------

@app.errorhandler(state_repo.ConflictError)
def handle_conflict(_e):
    message = "The pipeline changed the queue at the same moment. Nothing was saved, please try again."
    if wants_json():
        return jsonify(ok=False, message=message), 409
    flash(message, "error")
    return redirect(request.referrer if (request.referrer or "").startswith(request.host_url) else url_for("overview"))


@app.errorhandler(requests.RequestException)
def handle_github_error(e):
    message = (
        "Couldn't reach GitHub to read or save the queue. This is usually temporary; "
        "if it keeps happening, check that GITHUB_TOKEN is valid and has access to the companion repo."
    )
    if wants_json():
        return jsonify(ok=False, message=message), 502
    return render_template("error.html", message=message, detail=str(e)), 502


# --- Pages ------------------------------------------------------------------------

def sent_series(log, days=14):
    """First emails successfully sent per day, oldest first, for the overview chart."""
    per_day = {}
    for e in log:
        if e.get("status") == "SUCCESS":
            day = (e.get("date") or "")[:10]
            per_day[day] = per_day.get(day, 0) + 1
    today = utcnow().date()
    series = []
    for offset in range(days - 1, -1, -1):
        d = today - timedelta(days=offset)
        series.append({"label": f"{d.day} {d.strftime('%b')}", "count": per_day.get(d.isoformat(), 0)})
    return series


@app.route("/")
@login_required
def overview():
    companies, nav = load_data()
    now = utcnow()
    upcoming = [c for c in companies if c["bucket"] == "upcoming"]
    sent_rows = [c for c in companies if c["bucket"] == "sent"]
    replied = sum(1 for c in sent_rows if is_yes(c["replied"]))
    funnel = [
        ("Found by the scraper or added", len(companies)),
        ("Had a usable email", sum(1 for c in companies if has_email(c))),
        ("Emailed", len(sent_rows)),
        ("Replied", replied),
    ]
    series = sent_series(safe_call(state_repo.load_log, fallback=[]) or [])
    return render_template(
        "overview.html", active="overview", nav=nav, steps=setup_steps(),
        upcoming_count=len(upcoming),
        batch=min(len(upcoming), DAILY_SEND_LIMIT),
        rollover=max(0, len(upcoming) - DAILY_SEND_LIMIT),
        reply_rate=round(100 * replied / len(sent_rows)) if sent_rows else None,
        replied=replied, sent_count=len(sent_rows),
        funnel=funnel, series=series,
        series_total=sum(p["count"] for p in series),
        series_max=max([p["count"] for p in series] + [4]),
        next_send=next_send_run(now), next_scrape=next_scrape_run(now),
        scrape_run=safe_call(state_repo.latest_run, SCRAPE_WORKFLOW),
        send_run=safe_call(state_repo.latest_run, SEND_WORKFLOW),
        activity=safe_call(state_repo.recent_commits, 7, fallback=[]),
    )


@app.route("/attention")
@login_required
def attention():
    companies, nav = load_data()
    order = {"checkback": 0, "role": 1, "email": 2, "name": 3, "failed": 4}
    tracks = track_keys()
    items = sorted(
        (triage_item(c, tracks) for c in companies if c["bucket"] == "attention"),
        key=lambda i: (order[i["reason"]], i["name"].lower()),
    )
    return render_template("attention.html", active="attention", nav=nav, items=items)


@app.route("/attention/bulk", methods=["POST"])
@login_required
def attention_bulk():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    slugs = set(data.get("slugs") or [])
    if action not in ("drop", "retry", "accept_role") or not slugs:
        return jsonify(ok=False, message="Nothing selected."), 400

    known = set(current_tracks()[0])

    def fn(rows):
        done = []
        for r in rows:
            slug = state_repo.slugify(r["company_name"])
            if slug not in slugs:
                continue
            if action == "accept_role":
                # Keep the pre-selected role. Only rows waiting on a role, and
                # only if that role still exists (a removed one must be picked).
                if is_yes(r.get("role_review")) and r["track"] in known:
                    r["role_review"] = ""
                    r["role_reason"] = "best guess accepted by you"
                    done.append(slug)
                continue
            if action == "drop" and r["status"] in ("Sent", "Followup-Sent"):
                # A due check-in: "drop" means skip the check-in, not reject
                # a company you've already emailed.
                r["checkback_status"] = "skipped"
            elif action == "drop":
                r["status"] = "Rejected"
                done.append(slug)
            elif r["status"] == "Failed":
                r["status"] = "Approved"
                done.append(slug)
        return done

    verb = {"drop": "Drop", "retry": "Retry", "accept_role": "Accept best-guess role for"}[action]
    done = state_repo.mutate_queue(fn, f"{verb} {len(slugs)} leads via dashboard")
    if action == "accept_role":
        skipped = len(slugs) - len(done)
        message = (f"Kept the best-guess role for {len(done)}. They rejoin the queue."
                   + (f" {skipped} skipped: only companies waiting on a role whose guess still exists can be accepted."
                      if skipped else ""))
    elif action == "drop":
        message = f"Dropped {len(done)}. They won't be emailed."
    else:
        message = f"{len(done)} will be retried at the next send." + ("" if len(done) == len(slugs) else " Only failed sends can be retried.")
    return jsonify(ok=True, done=done, message=message)


@app.route("/upcoming")
@login_required
def upcoming():
    companies, nav = load_data()
    # outreach_pipeline.run_send_emails takes Approved rows in queue order, after
    # auto-approve has promoted every Pending row with an email, so queue order
    # among these rows is the send order.
    rows = [c for c in companies if c["bucket"] == "upcoming"]
    scraping = [c for c in companies if c["bucket"] == "scraping"]
    # Mirror run_send_emails: rows on a switched-off role, or one with no CV,
    # are held and skipped before the daily cap, so they don't take a slot.
    tracks, _ = current_tracks()
    hold = {}
    for key in {c["track"] or "devops" for c in rows}:
        t = tracks.get(key)
        if t is None:
            hold[key] = "unknown role"
        elif not t.get("active", True):
            hold[key] = "role is off"
        elif safe_call(state_repo.cv_size, t["cv_path"]) is None:
            hold[key] = "role has no CV"
    pos = 0
    for c in rows:
        c["held"] = hold.get(c["track"] or "devops")
        if not c["held"]:
            pos += 1
            c["send_pos"] = pos
    return render_template(
        "upcoming.html", active="upcoming", nav=nav, rows=rows, scraping=scraping,
        limit=DAILY_SEND_LIMIT, next_send=next_send_run(utcnow()),
        next_scrape=next_scrape_run(utcnow()),
    )


@app.route("/sent")
@login_required
def sent():
    companies, nav = load_data()
    show = request.args.get("show", "open")
    all_sent = [c for c in companies if c["bucket"] == "sent"]
    groups = {
        "open": [c for c in all_sent if not is_yes(c["replied"]) and not is_yes(c["bounced"])],
        "replied": [c for c in all_sent if is_yes(c["replied"])],
        "bounced": [c for c in all_sent if is_yes(c["bounced"])],
        "all": all_sent,
    }
    if show not in groups:
        show = "open"
    rows = sorted(groups[show], key=lambda c: c["date_sent"], reverse=True)
    return render_template(
        "sent.html", active="sent", nav=nav, rows=rows, show=show,
        group_counts={k: len(v) for k, v in groups.items()},
    )


@app.route("/companies")
@login_required
def companies_list():
    companies, nav = load_data()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    counts = {}
    for c in companies:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    rows = companies
    if status:
        rows = [c for c in rows if c["status"] == status]
    if q:
        needle = q.lower()
        rows = [
            c for c in rows
            if needle in c["company_name"].lower() or needle in c["email"].lower() or needle in c["host"]
        ]
    rows = sorted(rows, key=lambda c: c["company_name"].lower())
    return render_template(
        "companies.html", active="companies", nav=nav, rows=rows, q=q, status=status,
        status_counts=[(s, counts[s]) for s in STATUSES if counts.get(s)],
    )


@app.route("/companies/new", methods=["GET", "POST"])
@login_required
def company_new():
    _, nav = load_data()
    tracks = track_keys()
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        try:
            name = form.get("company_name", "").strip()
            if not name:
                raise ValueError("Company name is required.")
            email = clean_email(form.get("email"))
            website = clean_website(form.get("website"))
            if not email and not website:
                raise ValueError("Give at least a website (the scraper will find an email) or an email.")
            row = {
                "company_name": name,
                "website": website,
                "email": email,
                "company_type": form.get("company_type") if form.get("company_type") in COMPANY_TYPES else "general_startup",
                "track": form.get("track") if form.get("track") in tracks else tracks[0],
                "role_reason": "set by you",
                "recipient_name": form.get("recipient_name", "").strip() or DEFAULT_RECIPIENT,
                "company_address": form.get("company_address", "").strip() or DEFAULT_ADDRESS,
                "status": "Pending",
                "date_added": utcnow().strftime("%Y-%m-%d"),
                "lead_reviewed": "yes",
            }
            state_repo.add_company(row, f"Add {name} via dashboard")
        except ValueError as e:
            flash(str(e), "error")
        else:
            if email:
                flash(f"{name} added. It goes out with the next send.", "success")
            else:
                flash(f"{name} added. The next scrape will look for an email.", "success")
            return redirect(url_for("company_detail", slug=state_repo.slugify(name)))
    return render_template(
        "company_new.html", active="companies", nav=nav, form=form,
        company_types=COMPANY_TYPES, tracks=tracks,
    )


@app.route("/companies/<slug>", methods=["GET", "POST"])
@login_required
def company_detail(slug):
    companies, nav = load_data()
    company = next((c for c in companies if c["slug"] == slug), None)
    if company is None:
        flash("That company isn't in the queue any more.", "error")
        return redirect(url_for("companies_list"))
    tracks = track_keys()

    if request.method == "POST":
        f = request.form
        try:
            name = f.get("company_name", "").strip()
            if not name:
                raise ValueError("Company name is required.")
            email = f.get("email", "").strip()
            if email != "no_email_found":
                email = clean_email(email)
            status = f.get("status")
            if status not in STATUSES:
                raise ValueError("Pick a valid status.")
            changes = {
                "company_name": name,
                "website": clean_website(f.get("website")),
                "email": email,
                "company_type": f.get("company_type") if f.get("company_type") in COMPANY_TYPES else company["company_type"],
                "track": f.get("track") if f.get("track") in tracks else company["track"],
                "recipient_name": f.get("recipient_name", "").strip() or DEFAULT_RECIPIENT,
                "company_address": f.get("company_address", "").strip(),
                "status": status,
                "replied": "yes" if f.get("replied") else "",
                "bounced": "yes" if f.get("bounced") else "",
                "lead_reviewed": "yes",
            }
            if changes["track"] != company["track"] or is_yes(company.get("role_review")):
                # Saving the page with a role chosen is your decision.
                changes["role_reason"] = "set by you"
            changes["role_review"] = ""
            state_repo.update_company(slug, changes, f"Edit {name} via dashboard")
        except (ValueError, KeyError) as e:
            flash(str(e) if isinstance(e, ValueError) else "That company isn't in the queue any more.", "error")
            return redirect(url_for("company_detail", slug=slug))
        flash("Saved.", "success")
        return redirect(url_for("company_detail", slug=state_repo.slugify(name)))

    return render_template(
        "company_detail.html", active="companies", nav=nav, c=company,
        referral_attach=REFERRAL_ATTACH,
        outcomes=OUTCOMES, checkback_after=CHECKBACK_AFTER, checkin_is_due=checkin_due(company),
        checkin_preview=(checkin_preview(current_tracks()[0][company["track"]], company["company_name"],
                                         month_of(company.get("outcome_date", "")))
                         if company.get("outcome") == "later" and company["track"] in current_tracks()[0] else None),
        today=utcnow().strftime("%Y-%m-%d"),
        referral_preview=(referral_preview(current_tracks()[0][company["track"]], company["company_name"],
                                           company.get("referral_by", ""), company.get("referral_attach") or "both")
                          if company.get("referral_status") == "draft" and company["track"] in current_tracks()[0]
                          else None),
        statuses=STATUSES, company_types=COMPANY_TYPES, tracks=tracks,
        reason=attention_reason(company) if company["bucket"] == "attention" else None,
    )


@app.route("/companies/<slug>/delete", methods=["POST"])
@login_required
def company_delete(slug):
    try:
        row = state_repo.delete_company(slug, f"Delete {slug} via dashboard")
    except KeyError:
        flash("That company isn't in the queue any more.", "error")
    else:
        flash(f"Deleted {row['company_name']}.", "success")
    return redirect(url_for("companies_list"))


# --- Referrals ------------------------------------------------------------------
# A company answers "please send your CV to recruitment@...". From its page you
# fill in that address, who pointed you there, the role and what to attach,
# preview the exact email, then send now or with the next batch. The pipeline
# (run_send_referrals) does the sending. One row per company throughout.

REFERRAL_ATTACH = [
    ("both", "CV + cover letter",
     "Recommended. Right whenever they ask for an application, or for \u201cyour CV and any relevant documents\u201d."),
    ("cv", "CV only",
     "When they ask for just your CV, or say a short email is preferred."),
    ("letter", "Cover letter only",
     "Rarely right: a recruiter can't assess you without your CV. Only if they already have it."),
]
REFERRAL_ATTACH_LINE = {  # mirrors applicant.REFERRAL_ATTACHMENTS
    "both": "My CV and cover letter are attached.",
    "cv": "My CV is attached.",
    "letter": "My cover letter is attached, and I am happy to send my CV as well.",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def referral_preview(role, company, referred_by="", attach="both"):
    """
    The referral email exactly as applicant.build_referral_body writes it,
    minus the signature (your name and phone live in the pipeline's .env).
    tests/test_referrals.py keeps the two in step.
    """
    who = (referred_by or "").strip()
    opener = (f"{who} from your team kindly suggested I send my application to this address."
              if who else "I was asked to send my application to this address.")
    highlights = "\n".join(f"- {h}" for h in role["highlights"][:3])
    subject = f"Application: {role['role_title']} \u2013 <your name>"
    body = (
        f"Dear {company} Recruitment Team,\n\n"
        f"{opener}\n\n"
        f"My name is <your name>. I am {role['credentials']}. I would like to be considered "
        f"for {role['interest_area']} roles at {company}.\n\n"
        f"Some highlights of my work:\n"
        f"{highlights}\n\n"
        f"{REFERRAL_ATTACH_LINE.get(attach, REFERRAL_ATTACH_LINE['both'])} "
        f"Thank you for your time and consideration.\n\n"
    )
    return {"subject": subject, "body": body}


@app.route("/companies/<slug>/referral", methods=["POST"])
@login_required
def company_referral(slug):
    tracks, _ = current_tracks()
    f = request.form
    action = f.get("action", "preview")
    back = url_for("company_detail", slug=slug) + "#referral"

    if action == "cancel":
        def cancel(rows):
            for r in rows:
                if state_repo.slugify(r["company_name"]) == slug and r.get("referral_status") in ("draft", "queued"):
                    r["referral_status"] = ""
                    return True
            return False
        flash("Not sent. Your details are kept below to edit or send later." if state_repo.mutate_queue(cancel, f"Cancel referral for {slug}")
              else "Too late to cancel: it has already gone out.", "success")
        return redirect(back)

    if action in ("queue", "send_now"):
        # Confirming the draft you previewed: nothing in the form changes it.
        def confirm(rows):
            for r in rows:
                if state_repo.slugify(r["company_name"]) == slug and r.get("referral_status") == "draft":
                    r["referral_status"] = "queued"
                    return r
            return None
        row = state_repo.mutate_queue(confirm, f"Queue referral for {slug}")
        if row is None:
            flash("Preview the email first.", "error")
            return redirect(back)
        if action == "send_now":
            try:
                state_repo.dispatch_workflow(SEND_WORKFLOW, {"mode": "referrals"})
            except state_repo.TokenPermissionError:
                flash(f"Queued, but GitHub refused to start a send now, so it goes out with the next send "
                      f"({next_send_run(utcnow()):%a %H:%M}). Give your GITHUB_TOKEN Actions: Read and write to fix.", "error")
            else:
                flash(f"Sending to {row['referral_email']} now. It takes a couple of minutes; "
                      f"refresh to see it marked sent.", "success")
        else:
            flash(f"Queued: goes to {row['referral_email']} with the next send ({next_send_run(utcnow()):%a %d %b, %H:%M}).",
                  "success")
        return redirect(back)

    # action == "preview": validate and save as a draft (never sent as-is).
    email = (f.get("referral_email") or "").strip()
    who = (f.get("referral_by") or "").strip()[:60]
    track = f.get("track", "")
    attach = f.get("referral_attach", "both")
    errors = []
    if not EMAIL_RE.match(email):
        errors.append("Enter the address they asked you to send to.")
    if track not in tracks:
        errors.append("Pick one of your roles.")
    if attach not in REFERRAL_ATTACH_LINE:
        errors.append("Choose what to attach.")
    elif attach in ("both", "cv") and track in tracks and safe_call(state_repo.cv_size, tracks[track]["cv_path"]) is None:
        errors.append(f"The {track} role has no CV yet. Upload it on its Roles page, or attach the cover letter only.")
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(back)

    def draft(rows):
        for r in rows:
            if state_repo.slugify(r["company_name"]) != slug:
                continue
            r.update({"referral_email": email, "referral_by": who, "referral_attach": attach,
                      "referral_status": "draft", "referral_note": "", "track": track})
            # Their answer was the referral: count it as a reply, so no
            # automatic follow-up chases them.
            if not is_yes(r.get("replied")):
                r["replied"] = "yes"
                r["reply_note"] = f"replied with a referral to {email}"
            return True
        return False
    if not state_repo.mutate_queue(draft, f"Draft referral for {slug}"):
        flash("That company isn't in the queue any more.", "error")
        return redirect(url_for("companies_list"))
    return redirect(back)


# --- Their reply: outcome and check-ins -------------------------------------------
# Record what a company's reply said. "Not hiring now" schedules a check-in:
# on that date the company comes back to Needs you with a short email
# drafted, and nothing is sent until you read it and press Send.

OUTCOMES = [
    ("interview", "Interview / next steps", "They want to talk. Reply yourself; nothing automatic is sent."),
    ("later", "Not hiring now: check back later",
     "They're interested but not ready. You'll get a drafted check-in on the date you choose."),
    ("declined", "Not interested", "Closed. Nothing more is sent to them."),
]
CHECKBACK_AFTER = [("1", "1 month"), ("3", "3 months"), ("6", "6 months")]


def checkin_preview(role, company, replied_month=""):
    """
    The check-in exactly as applicant.build_checkin_body writes it, minus the
    signature. tests/test_checkins.py keeps the two in step.
    """
    when = f"In {replied_month} you" if replied_month else "You"
    highlights = "\n".join(f"- {h}" for h in role["highlights"][:2])
    return {
        "subject": f"Checking in: {role['role_title']} \u2013 <your name>",
        "body": (
            f"Dear {company} team,\n\n"
            f"{when} kindly replied that {company} wasn't ready to recruit yet, and that you "
            f"might be in touch when you were. I wanted to check in in case that has changed: "
            f"I am still very interested in {role['interest_area']} roles at {company}.\n\n"
            f"As a reminder, I am {role['credentials']}. Some recent highlights:\n"
            f"{highlights}\n\n"
            f"My current CV is attached. If now isn't the right time either, that's no problem at all; "
            f"I'd be glad to hear from you whenever you're ready.\n\n"
            f"Thank you, and best wishes with everything at {company}.\n\n"
        ),
    }


def month_of(date_str):
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%B %Y")
    except (ValueError, TypeError):
        return ""


def add_months(day, months):
    """Same day N months on, clamped to the month's end (31 Jan + 1 -> 28/29 Feb)."""
    m = day.month - 1 + months
    y, m = day.year + m // 12, m % 12 + 1
    import calendar
    return day.replace(year=y, month=m, day=min(day.day, calendar.monthrange(y, m)[1]))


@app.route("/companies/<slug>/reply", methods=["POST"])
@login_required
def company_reply(slug):
    f = request.form
    action = f.get("action", "save")
    back = url_for("company_detail", slug=slug) + "#reply"
    today = utcnow().date()

    def with_row(fn, message):
        def run(rows):
            for r in rows:
                if state_repo.slugify(r["company_name"]) == slug:
                    return fn(r)
            return None
        return state_repo.mutate_queue(run, message)

    if action == "save":
        outcome = f.get("outcome", "")
        if outcome not in dict((k, 1) for k, _, _ in OUTCOMES) and outcome != "":
            flash("Pick what they said.", "error")
            return redirect(back)
        checkback = ""
        if outcome == "later":
            if f.get("checkback_in") in dict(CHECKBACK_AFTER):
                checkback = add_months(today, int(f["checkback_in"])).isoformat()
            else:
                try:
                    picked = datetime.strptime(f.get("checkback_date", ""), "%Y-%m-%d").date()
                except ValueError:
                    flash("Choose when to check back.", "error")
                    return redirect(back)
                if picked <= today:
                    flash("Pick a date after today.", "error")
                    return redirect(back)
                checkback = picked.isoformat()

        def save(r):
            r.update({"outcome": outcome, "outcome_date": today.isoformat() if outcome else "",
                      "checkback_date": checkback, "checkback_status": "", "checkback_note": "",
                      "checkback_notified": ""})
            if outcome and not is_yes(r.get("replied")):
                r["replied"] = "yes"
                r["reply_note"] = r.get("reply_note") or "marked by you"
            return r
        if with_row(save, f"Record reply outcome for {slug}") is None:
            flash("That company isn't in the queue any more.", "error")
            return redirect(url_for("companies_list"))
        msg = {"interview": "Recorded: interview / next steps. Good luck!",
               "declined": "Recorded: not interested. Nothing more goes to them.",
               "": "Cleared."}.get(outcome)
        if outcome == "later":
            msg = (f"Recorded. On {datetime.fromisoformat(checkback):%a %d %b %Y} they'll come back to "
                   f"Needs you with a check-in drafted for you to read and send.")
        flash(msg, "success")
        return redirect(back)

    if action == "snooze":
        def snooze(r):
            if r.get("outcome") != "later":
                return None
            r["checkback_date"] = add_months(today, 1).isoformat()
            r["checkback_notified"] = ""
            return r
        r = with_row(snooze, f"Snooze check-in for {slug}")
        flash(f"Pushed back to {datetime.fromisoformat(r['checkback_date']):%d %b %Y}." if r else "Nothing to snooze.",
              "success" if r else "error")
        return redirect(back)

    if action == "skip":
        with_row(lambda r: r.update({"checkback_status": "skipped"}) or r, f"Skip check-in for {slug}")
        flash("Check-in skipped. Nothing will be sent.", "success")
        return redirect(back)

    if action == "cancel":
        ok = with_row(lambda r: (r.update({"checkback_status": ""}) or r)
                      if r.get("checkback_status") == "queued" else None, f"Cancel check-in for {slug}")
        flash("Check-in cancelled; it's back to waiting for you." if ok else "Too late: it has already gone out.",
              "success" if ok else "error")
        return redirect(back)

    if action == "send_now":
        tracks, _ = current_tracks()

        def queue_it(r):
            if not checkin_due(r):
                return None
            if r["track"] not in tracks or safe_call(state_repo.cv_size, tracks[r["track"]]["cv_path"]) is None:
                return "no_cv"
            r["checkback_status"] = "queued"
            return r
        r = with_row(queue_it, f"Send check-in to {slug}")
        if r == "no_cv":
            flash("This company's role has no CV to attach. Upload it on the Roles page first.", "error")
        elif r is None:
            flash("There's no check-in due for this company.", "error")
        else:
            try:
                state_repo.dispatch_workflow(SEND_WORKFLOW, {"mode": "referrals"})
            except state_repo.TokenPermissionError:
                flash(f"Queued, but GitHub refused to start a send now, so it goes out with the next send "
                      f"({next_send_run(utcnow()):%a %H:%M}).", "error")
            else:
                flash(f"Sending the check-in to {r['email']} now. Refresh in a couple of minutes.", "success")
        return redirect(back)

    flash("Unknown action.", "error")
    return redirect(back)


@app.route("/companies/<slug>/resolve", methods=["POST"])
@login_required
def company_resolve(slug):
    """The one-step fix on the Needs you page: supply what was missing and requeue it."""
    reason = request.form.get("reason")
    name = request.form.get("company_name", "").strip()
    try:
        email = clean_email(request.form.get("email"))
        changes = {"lead_reviewed": "yes"}
        if reason == "role":
            track = request.form.get("track", "")
            if track not in track_keys():
                raise ValueError("Pick one of your roles.")
            row = state_repo.update_company(
                slug, {"track": track, "role_review": "", "role_reason": "set by you", "lead_reviewed": "yes"},
                f"Set role of {slug} to {track} via dashboard")
            if has_email(row):
                return respond(True, f"{row['company_name']} will be pitched as {track} at the next send.", "attention")
            return respond(True, f"{row['company_name']} set to {track}. The next scrape will look for its email.", "attention")
        if reason == "name":
            if not name:
                raise ValueError("Enter the company's real name.")
            changes["company_name"] = name
        elif reason == "email" and not email:
            raise ValueError("Enter an email address for this company.")
        if email:
            changes["email"] = email
        if reason == "failed":
            changes["status"] = "Approved"
            message = "Retry send for {}"
        else:
            changes["status"] = "Pending"
            message = "Resolve {} via dashboard"
        row = state_repo.update_company(slug, changes, message.format(name or slug))
    except ValueError as e:
        return respond(False, str(e), "attention")
    except KeyError:
        return respond(False, "That company isn't in the queue any more.", "attention")
    if reason == "failed":
        msg = f"{row['company_name']} will be retried at the next send."
    elif has_email(row):
        msg = f"{row['company_name']} is fixed and goes out with the next send."
    else:
        msg = f"{row['company_name']} is fixed. The next scrape will look for its email."
    return respond(True, msg, "attention")


@app.route("/companies/<slug>/status", methods=["POST"])
@login_required
def company_status(slug):
    status = request.form.get("status")
    if status not in ("Rejected", "Pending", "Approved"):
        return respond(False, "That status change isn't allowed from here.", "companies_list")
    try:
        row = state_repo.update_company(slug, {"status": status}, f"Set {slug} to {status}")
    except KeyError:
        return respond(False, "That company isn't in the queue any more.", "companies_list")
    labels = {"Rejected": "won't be emailed", "Pending": "is back in the queue", "Approved": "goes out with the next send"}
    return respond(True, f"{row['company_name']} {labels[status]}.", "companies_list")


@app.route("/companies/<slug>/flag", methods=["POST"])
@login_required
def company_flag(slug):
    flag = request.form.get("flag")
    if flag not in ("replied", "bounced"):
        flash("Unknown flag.", "error")
        return safe_next("sent")
    value = "yes" if request.form.get("value") == "yes" else ""
    try:
        row = state_repo.update_company(slug, {flag: value}, f"Mark {slug} {flag}={value or 'no'}")
    except KeyError:
        flash("That company isn't in the queue any more.", "error")
    else:
        if value:
            flash(f"{row['company_name']} marked {flag}. No more follow-ups.", "success")
        else:
            flash(f"Cleared {flag} on {row['company_name']}.", "success")
    return safe_next("sent")


@app.route("/activity")
@login_required
def activity():
    _, nav = load_data()
    log = list(reversed(safe_call(state_repo.load_log, fallback=[]) or []))[:100]
    commits = safe_call(state_repo.recent_commits, 40, fallback=[])
    return render_template("activity.html", active="activity", nav=nav, log=log, commits=commits)


# --- Settings ------------------------------------------------------------------------

def parse_queries(text, tracks):
    """Validate search_queries.txt the same way outreach_pipeline.load_search_queries reads it."""
    queries = []
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        query = parts[0]
        limit = parts[1] if len(parts) > 1 else ""
        track = parts[2].lower() if len(parts) > 2 else ""
        if len(parts) > 3:
            raise ValueError(f"Line {n} has too many | separators (search | number | role).")
        if not query:
            raise ValueError(f"Line {n} has a limit but no search text.")
        if limit:
            if not limit.isdigit() or not 1 <= int(limit) <= 25:
                raise ValueError(f"Line {n}: the number after | must be between 1 and 25.")
        if track and track not in tracks:
            raise ValueError(f"Line {n}: no role called '{track}'. Roles: {', '.join(tracks)}.")
        queries.append(query)
    if not queries:
        raise ValueError("Add at least one search.")
    return queries


@app.route("/settings")
@login_required
def settings():
    _, nav = load_data()
    queries_text = state_repo.get_queries_text()
    return render_template(
        "settings.html", active="settings", nav=nav, tracks=track_keys(),
        queries_text=queries_text if queries_text is not None else default_queries_text(),
        searches_per_run=SEARCHES_PER_RUN,
        queries_count=sum(1 for l in (queries_text if queries_text is not None else default_queries_text()).splitlines()
                          if l.strip() and not l.strip().startswith("#")),
        queries_saved=queries_text is not None,
        scrape_run=safe_call(state_repo.latest_run, SCRAPE_WORKFLOW),
        send_run=safe_call(state_repo.latest_run, SEND_WORKFLOW),
        scrape_url=state_repo.actions_url(SCRAPE_WORKFLOW),
        send_url=state_repo.actions_url(SEND_WORKFLOW),
        next_send=next_send_run(utcnow()), next_scrape=next_scrape_run(utcnow()),
        pipeline_repo=state_repo.PIPELINE_REPO,
    )


@app.route("/settings/queries", methods=["POST"])
@login_required
def settings_queries():
    text = request.form.get("queries", "").replace("\r\n", "\n").strip() + "\n"
    try:
        queries = parse_queries(text, track_keys())
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("settings") + "#queries")
    state_repo.save_queries_text(text, "Update search queries via dashboard")
    flash(f"Saved {len(queries)} search{'es' if len(queries) != 1 else ''}. The next scrape uses them.", "success")
    return redirect(url_for("settings") + "#queries")


@app.route("/settings/run/<which>", methods=["POST"])
@login_required
def settings_run(which):
    workflow = {"scrape": SCRAPE_WORKFLOW, "send": SEND_WORKFLOW}.get(which)
    if not workflow:
        flash("Unknown pipeline.", "error")
        return redirect(url_for("settings"))
    if member_paused():
        flash("Your pipeline is paused by the dashboard's owner, so nothing will run. Ask them to resume it.",
              "error")
        return redirect(url_for("settings"))
    try:
        state_repo.dispatch_workflow(workflow)
    except state_repo.TokenPermissionError:
        flash(
            f"GitHub refused to start the run. Edit your GITHUB_TOKEN to also give it "
            f"Actions: Read and write on {state_repo.PIPELINE_REPO}.", "error",
        )
    else:
        label = "Scrape" if which == "scrape" else "Send"
        flash(f"{label} started. It takes a few minutes; refresh this page to see the result.", "success")
    return redirect(url_for("settings"))


# --- Roles (tracks) ------------------------------------------------------------
# A role is one pitch: its own CV, letterhead title, email highlights and cover
# letter. Stored in tracks.json in the companion repo; the pipeline reads the
# same file (applicant.load_tracks), so what's saved here is what gets sent.

# Mirrors applicant.TRACK_KEY_RE.
TRACK_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_FIELD = 1200

# (field, label, hint) for the one-line pitch fields, in form order.
ROLE_TEXT_FIELDS = [
    ("role_title", "Role title", "Used as “the ___ position at <company>”. e.g. Full-Stack Engineer"),
    ("headline", "Letterhead title", "Printed under your name on the cover letter."),
    ("subject_area", "Email subject area", "Subject becomes “___ Application - <your name>”."),
    ("interest_area", "Interest area", "“I am writing to express my strong interest in ___ opportunities”."),
    ("credentials", "Who you are", "“My name is <you>. I am ___.”"),
    ("closing_skills", "Closing skills", "e.g. backend engineering skills, product focus, and adaptability"),
    ("followup_expertise", "Follow-up talking points", "“how my experience with ___ can support your team’s goals”."),
]
LETTER_BULLETS = 3


def track_keys():
    tracks, _ = current_tracks()
    return list(tracks)


def _clean(value, label, required=True):
    v = (value or "").strip()
    if required and not v:
        raise ValueError(f"{label} is required.")
    if len(v) > MAX_FIELD:
        raise ValueError(f"{label} is too long ({len(v)} characters, max {MAX_FIELD}).")
    return v


def role_from_form(form, existing=None):
    """Build a track dict from the role form. Raises ValueError with a user-facing message."""
    role = dict(existing or {})
    for field, label, _ in ROLE_TEXT_FIELDS:
        role[field] = _clean(form.get(field), label)

    highlights = [h.strip().lstrip("-• ").strip() for h in form.get("highlights", "").splitlines()]
    highlights = [h for h in highlights if h]
    if not 2 <= len(highlights) <= 5:
        raise ValueError("Email highlights: write 2 to 5, one per line.")
    role["highlights"] = [_clean(h, "A highlight") for h in highlights]

    raw_kw = re.split(r"[,\n]", form.get("keywords", ""))
    keywords = []
    for kw in raw_kw:
        kw = kw.strip().lower()
        if kw and kw not in keywords:
            if len(kw) > 40:
                raise ValueError(f"Keyword '{kw[:40]}...' is too long (40 characters max).")
            keywords.append(kw)
    if len(keywords) > 60:
        raise ValueError("Keep it to 60 keywords or fewer; the most specific ones do the work.")
    role["keywords"] = keywords

    intro = _clean(form.get("letter_intro"), "Letter intro", required=False)
    lead_in = _clean(form.get("letter_lead_in"), "Letter lead-in", required=False)
    closing = _clean(form.get("letter_closing"), "Letter closing", required=False)
    bullets = []
    for i in range(1, LETTER_BULLETS + 1):
        title = _clean(form.get(f"bullet_title_{i}"), f"Bullet {i} title", required=False)
        text = _clean(form.get(f"bullet_text_{i}"), f"Bullet {i} text", required=False)
        if bool(title) != bool(text):
            raise ValueError(f"Letter bullet {i} needs both a title and text (or neither).")
        if title:
            bullets.append({"title": title, "text": text})

    if not (intro or lead_in or closing or bullets):
        # Only a role that already uses the sector-specific templates
        # (the original devops role) may keep an empty letter.
        if existing is not None and existing.get("letter") is None:
            role["letter"] = None
        else:
            raise ValueError("Write the cover letter: an intro, at least 2 bullets, and a closing.")
    else:
        if not intro or not closing or len(bullets) < 2:
            raise ValueError("The cover letter needs an intro, at least 2 bullets, and a closing.")
        role["letter"] = {"intro": intro, "lead_in": lead_in, "bullets": bullets, "closing": closing}
    return role


def role_preview(role, company="Acme Ltd"):
    """
    The cold email exactly as applicant.build_outreach_body writes it, minus the
    signature (your name and phone live in the pipeline's .env, not here).
    tests/test_role_preview.py keeps the two in step.
    """
    highlights = "\n".join(f"- {h}" for h in role["highlights"])
    subject = f"{role['subject_area']} Application - <your name>"
    body = (
        f"Dear Recruiting Team at {company},\n\n"
        f"I hope this email finds you well.\n\n"
        f"My name is <your name>. I am {role['credentials']}.\n\n"
        f"I am writing to express my strong interest in {role['interest_area']} "
        f"opportunities at {company}. To share details on how my background aligns "
        f"with your work, I have attached my CV and a tailored cover letter.\n\n"
        f"Some highlights of my work include:\n"
        f"{highlights}\n\n"
        f"I would welcome the opportunity to speak briefly with a member of your team or "
        f"your engineering leads about how my skills and experience can "
        f"add value to {company}.\n\n"
        f"Thank you for your time and consideration.\n\n"
    )
    letter = None
    if role.get("letter"):
        def fill(t):
            return (t or "").replace("{company}", company).replace("{role}", role["role_title"])
        L = role["letter"]
        letter = {
            "intro": fill(L["intro"]), "lead_in": fill(L.get("lead_in")),
            "bullets": [{"title": fill(b["title"]), "text": fill(b["text"])} for b in L["bullets"]],
            "closing": fill(L["closing"]),
        }
    return {"subject": subject, "body": body, "letter": letter, "company": company}


def _load_role(key):
    tracks, saved = current_tracks()
    return tracks.get(key), tracks, saved


@app.route("/roles")
@login_required
def roles():
    companies, nav = load_data()
    tracks, saved = current_tracks()
    rows = []
    for key, t in tracks.items():
        size = safe_call(state_repo.cv_size, t["cv_path"])
        queued = sum(1 for c in companies if c["bucket"] == "upcoming" and (c["track"] or "devops") == key)
        sent_n = sum(1 for c in companies if c["bucket"] == "sent" and (c["track"] or "devops") == key)
        rows.append({
            "key": key, "t": t, "has_cv": size is not None,
            "cv_kb": max(0.1, round(size / 1024, 1)) if size is not None else None,
            "queued": queued, "sent": sent_n,
            "live": bool(t.get("active", True)) and size is not None,
        })
    orphans = {}
    for c in companies:
        if role_missing(c):
            orphans[c["track"]] = orphans.get(c["track"], 0) + 1
    return render_template("roles.html", active="roles", nav=nav, rows=rows, saved=saved,
                           orphans=sorted(orphans.items()), keys=list(tracks))


@app.route("/roles/new", methods=["GET", "POST"])
@login_required
def role_new():
    _, nav = load_data()
    form = request.form if request.method == "POST" else {}
    if request.method == "POST":
        try:
            key = form.get("key", "").strip().lower()
            if not TRACK_KEY_RE.match(key):
                raise ValueError("Short name: lowercase letters, digits, - or _ (e.g. fullstack, platform).")
            role = role_from_form(form)
            # A new role starts switched off: nothing goes out under it until it
            # has a CV and you've turned it on from its page.
            role.update({"active": False, "cv_path": f"cv_{key}.pdf"})

            def add(tracks):
                if key in tracks:
                    raise ValueError(f"There's already a role called '{key}'.")
                tracks[key] = role
            state_repo.mutate_tracks(add, f"Add role '{key}' via dashboard")
        except ValueError as e:
            flash(str(e), "error")
        else:
            flash(f"Role '{key}' added, switched off. Upload its CV, check the preview, then turn it on.", "success")
            return redirect(url_for("role_detail", key=key))
    return render_template(
        "role_form.html", active="roles", nav=nav, form=form, role=None, key=None,
        text_fields=ROLE_TEXT_FIELDS, n_bullets=LETTER_BULLETS,
    )


@app.route("/roles/<key>", methods=["GET", "POST"])
@login_required
def role_detail(key):
    companies, nav = load_data()
    role, _, saved = _load_role(key)
    if role is None:
        flash("No role by that name.", "error")
        return redirect(url_for("roles"))

    if request.method == "POST":
        try:
            def edit(tracks):
                if key not in tracks:
                    raise ValueError("That role was removed while you were editing.")
                tracks[key] = role_from_form(request.form, tracks[key])
            state_repo.mutate_tracks(edit, f"Edit role '{key}' via dashboard")
        except ValueError as e:
            flash(str(e), "error")
            # Re-render with what was typed rather than losing it.
            return render_template(
                "role_form.html", active="roles", nav=nav, form=request.form, role=role, key=key,
                text_fields=ROLE_TEXT_FIELDS, n_bullets=LETTER_BULLETS,
            )
        flash("Role saved. The next send uses it.", "success")
        return redirect(url_for("role_detail", key=key))

    size = safe_call(state_repo.cv_size, role["cv_path"])
    # Pre-fill the form from the stored role.
    form = {f: role.get(f, "") for f, _, _ in ROLE_TEXT_FIELDS}
    form["highlights"] = "\n".join(role.get("highlights", []))
    form["keywords"] = ", ".join(role.get("keywords", []))
    L = role.get("letter") or {}
    form.update({"letter_intro": L.get("intro", ""), "letter_lead_in": L.get("lead_in", ""),
                 "letter_closing": L.get("closing", "")})
    for i, b in enumerate(L.get("bullets", [])[:LETTER_BULLETS], start=1):
        form[f"bullet_title_{i}"], form[f"bullet_text_{i}"] = b["title"], b["text"]
    return render_template(
        "role_form.html", active="roles", nav=nav, form=form, role=role, key=key,
        text_fields=ROLE_TEXT_FIELDS, n_bullets=LETTER_BULLETS,
        has_cv=size is not None, cv_kb=max(0.1, round(size / 1024, 1)) if size is not None else None,
        preview=role_preview(role),
        other_roles=[k for k in current_tracks()[0] if k != key],
        builtin_differs=builtin_pitch_differs(key, role),
        queued=sum(1 for c in companies if c["bucket"] == "upcoming" and (c["track"] or "devops") == key),
    )


@app.route("/roles/<key>/active", methods=["POST"])
@login_required
def role_active(key):
    role, _, _ = _load_role(key)
    if role is None:
        flash("No role by that name.", "error")
        return redirect(url_for("roles"))
    turn_on = request.form.get("active") == "on"
    if turn_on and state_repo.cv_size(role["cv_path"]) is None:
        flash("Upload this role's CV first. A role can't send without its own CV.", "error")
        return redirect(url_for("role_detail", key=key))

    def toggle(tracks):
        tracks[key]["active"] = turn_on
    state_repo.mutate_tracks(toggle, f"Turn role '{key}' {'on' if turn_on else 'off'} via dashboard")
    flash(f"'{key}' is {'on: its queued companies go out with the next send' if turn_on else 'off: its companies wait in the queue'}.", "success")
    return redirect(url_for("role_detail", key=key))


# What "reset to built-in pitch" replaces: the words, not the role's CV,
# on/off state or keywords (which decide targeting, and you may have tuned).
PITCH_FIELDS = [f for f, _, _ in ROLE_TEXT_FIELDS] + ["highlights", "letter"]


def builtin_pitch_differs(key, role):
    if state_repo.current_tenant():
        return False  # the built-in pitches are the owner's: never offered to anyone else
    d = state_repo.DEFAULT_TRACKS.get(key)
    return bool(d) and any(role.get(f) != d.get(f) for f in PITCH_FIELDS)


@app.route("/roles/<key>/reset", methods=["POST"])
@login_required
def role_reset(key):
    if state_repo.current_tenant() or key not in state_repo.DEFAULT_TRACKS:
        flash("Only built-in roles have a pitch to reset to.", "error")
        return redirect(url_for("role_detail", key=key))

    def reset(ts):
        if key not in ts:
            raise ValueError("That role no longer exists.")
        for f in PITCH_FIELDS:
            ts[key][f] = copy.deepcopy(state_repo.DEFAULT_TRACKS[key][f])
    try:
        state_repo.mutate_tracks(reset, f"Reset role '{key}' to the built-in pitch via dashboard")
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("roles"))
    flash("Pitch reset to the built-in version. Your CV, keywords and on/off setting are unchanged.", "success")
    return redirect(url_for("role_detail", key=key) + "#preview")


def _move_companies(from_key, to_key, message):
    """Retag every row on from_key (any status) to to_key. Returns (moved, of_which_sent)."""
    def fn(rows):
        moved = sent = 0
        for r in rows:
            if r["track"] == from_key:
                r["track"] = to_key
                r["role_reason"] = f"moved from {from_key}"
                moved += 1
                sent += r["status"] in ("Sent", "Followup-Sent")
        return moved, sent
    return state_repo.mutate_queue(fn, message)


def _retag_queries(from_key, to_key):
    """Point any search tagged '| from_key' at to_key instead. Returns lines changed."""
    text = state_repo.get_queries_text()
    if text is None:
        return 0
    changed, out = 0, []
    for line in text.splitlines():
        parts = line.split("|")
        if not line.strip().startswith("#") and len(parts) == 3 and parts[2].strip().lower() == from_key:
            parts[2] = f" {to_key}"
            line = "|".join(parts)
            changed += 1
        out.append(line)
    if changed:
        state_repo.save_queries_text("\n".join(out) + "\n", f"Retag searches from {from_key} to {to_key}")
    return changed


@app.route("/roles/<key>/delete", methods=["POST"])
@login_required
def role_delete(key):
    tracks, _ = current_tracks()
    to_key = request.form.get("move_to", "")
    if key not in tracks:
        flash("No role by that name.", "error")
        return redirect(url_for("roles"))
    if len(tracks) < 2:
        flash("You can't remove your only role.", "error")
        return redirect(url_for("role_detail", key=key))
    if to_key not in tracks or to_key == key:
        flash("Pick another role to move this role's companies to.", "error")
        return redirect(url_for("role_detail", key=key) + "#remove")

    # Companies and searches first: if this stops halfway, the role still
    # exists and nothing is orphaned; retrying finishes the job.
    moved, sent = _move_companies(key, to_key, f"Move companies from role '{key}' to '{to_key}'")
    queries = _retag_queries(key, to_key)
    merge_kw = request.form.get("merge_keywords") == "on"

    def remove(ts):
        if key in ts:
            if merge_kw and to_key in ts:
                kws = ts[to_key].setdefault("keywords", [])
                kws.extend(k for k in ts[key].get("keywords", []) if k not in kws)
            del ts[key]
    state_repo.mutate_tracks(remove, f"Remove role '{key}' via dashboard")

    parts = [f"Removed '{key}'. Moved {moved} compan{'y' if moved == 1 else 'ies'} to '{to_key}'"]
    if sent:
        parts.append(f"({sent} already emailed; their follow-up now uses the {to_key} pitch and CV)")
    if queries:
        parts.append(f"and {queries} search{'es' if queries != 1 else ''}")
    if merge_kw:
        parts.append(f"and added its keywords to '{to_key}'")
    flash(" ".join(parts) + ".", "success")
    return redirect(url_for("role_detail", key=to_key))


@app.route("/roles/move-orphans", methods=["POST"])
@login_required
def roles_move_orphans():
    tracks, _ = current_tracks()
    from_key, to_key = request.form.get("from", ""), request.form.get("move_to", "")
    if from_key in tracks or to_key not in tracks:
        flash("Pick one of your roles to move them to.", "error")
        return redirect(url_for("roles"))
    moved, _ = _move_companies(from_key, to_key, f"Move companies from removed role '{from_key}' to '{to_key}'")
    queries = _retag_queries(from_key, to_key)
    extra = f" and {queries} search{'es' if queries != 1 else ''}" if queries else ""
    flash(f"Moved {moved} compan{'y' if moved == 1 else 'ies'}{extra} from '{from_key}' to '{to_key}'.", "success")
    return redirect(url_for("roles"))


@app.route("/roles/<key>/cv", methods=["POST"])
@login_required
def role_cv_upload(key):
    role, _, _ = _load_role(key)
    if role is None:
        flash("No role by that name.", "error")
        return redirect(url_for("roles"))
    f = request.files.get("cv")
    if not f or not f.filename:
        flash("Choose a PDF file first.", "error")
    elif not f.filename.lower().endswith(".pdf") or f.mimetype != "application/pdf":
        flash("That doesn't look like a PDF.", "error")
    else:
        data = f.read()
        if not data.startswith(b"%PDF-"):
            flash("That doesn't look like a valid PDF file.", "error")
        else:
            state_repo.save_cv(role["cv_path"], data, f"Update '{key}' CV via dashboard")
            if role.get("active", True):
                flash("CV updated. The next send attaches this version.", "success")
            else:
                flash("CV uploaded. Check the preview below, then turn the role on.", "success")
    return redirect(url_for("role_detail", key=key) + "#cv")


@app.route("/roles/<key>/cv")
@login_required
def role_cv_view(key):
    role, _, _ = _load_role(key)
    data = state_repo.get_cv(role["cv_path"])[0] if role else None
    if data is None:
        flash("No CV uploaded for this role yet.", "error")
        return redirect(url_for("roles"))
    return Response(
        data, mimetype="application/pdf",
        headers={"Content-Disposition": f"inline; filename=cv_{key}.pdf"},
    )

# --- Invited users: profile and keys -----------------------------------------------
# A member's applications go out under their own name, from their own Gmail,
# with their own Exa/Apify keys: all set here. Keys are write-only: the page
# shows whether each is saved, never its value.

PROFILE_FIELDS = [
    # (field, label, required, hint)
    ("full_name", "Full name", True, "As it should appear on your emails and cover letters."),
    ("headline", "Headline", False, "Under your name on the cover letter, e.g. Frontend Engineer. Each role can override it."),
    ("email", "Gmail address you send from", True, "Applications are sent from here, and replies are read from its inbox."),
    ("phone", "Phone", False, "With country code, e.g. +233 20 000 0000."),
    ("location", "Your location", False, "e.g. Accra, Ghana. Shown in your signature; also the default for companies found."),
    ("linkedin", "LinkedIn URL", False, ""),
    ("portfolio", "GitHub, Behance or portfolio URL", False, ""),
]
SECRET_LABELS = {
    "gmail_app_password": ("Gmail app password", "16 letters from myaccount.google.com/apppasswords (needs 2-Step Verification)."),
    "exa_api_key": ("Exa API key", "From dashboard.exa.ai: finds companies for your searches."),
    "apify_api_token": ("Apify API token", "From console.apify.com, Settings → API: finds their email addresses."),
}
URL_RE = re.compile(r"^https?://[^\s\"'<>]+$", re.I)


def setup_steps():
    """What an invited user still has to do before anything can go out, in order."""
    if not state_repo.current_tenant():
        return []
    profile = state_repo.get_profile()
    keys = set(profile.get("keys_set", []))
    tracks, _ = current_tracks()
    ready_role = any(t.get("active", True) and t.get("letter") and
                     safe_call(state_repo.cv_size, t["cv_path"]) is not None for t in tracks.values())
    queries = state_repo.get_queries_text()
    has_search = bool(queries and any(l.strip() and not l.strip().startswith("#") for l in queries.splitlines()))
    return [
        ("Fill in your profile", bool(profile.get("full_name") and profile.get("email")), url_for("profile")),
        ("Save your Gmail app password", "gmail_app_password" in keys, url_for("profile") + "#keys"),
        ("Add a role with its cover letter and CV, then turn it on", ready_role, url_for("roles")),
        ("Write your searches", has_search, url_for("settings") + "#queries"),
        ("Save your Exa and Apify keys", {"exa_api_key", "apify_api_token"} <= keys, url_for("profile") + "#keys"),
    ]


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if not state_repo.current_tenant():
        flash("Your own details are set in applicant.py and the repo's secrets.", "success")
        return redirect(url_for("settings"))
    _, nav = load_data()
    current = state_repo.get_profile()
    import secretbox
    can_store_keys = secretbox.available()

    if request.method == "POST" and request.form.get("action") == "test_gmail":
        try:
            secrets = state_repo.get_secrets()
        except secretbox.SecretsUnavailable:
            secrets = {}
        if not current.get("email") or not secrets.get("gmail_app_password"):
            flash("Save your Gmail address and app password first.", "error")
        else:
            try:
                with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as smtp:
                    smtp.ehlo()
                    smtp.user, smtp.password = current["email"], secrets["gmail_app_password"]
                    smtp.auth("PLAIN", smtp.auth_plain)
                flash("Gmail accepted your app password. You're set to send.", "success")
            except smtplib.SMTPAuthenticationError:
                flash("Gmail rejected that address and app password. Make a new app password and save it again.",
                      "error")
            except (smtplib.SMTPException, OSError):
                flash("Couldn't reach Gmail just now. Try again in a minute.", "error")
        return redirect(url_for("profile") + "#keys")

    if request.method == "POST":
        f = request.form
        new = dict(current)
        errors = []
        for field, label, required, _ in PROFILE_FIELDS:
            value = (f.get(field) or "").strip()[:200]
            if required and not value:
                errors.append(f"{label} is required.")
            if field == "email" and value and not EMAIL_RE.match(value):
                errors.append("Enter a valid email address.")
            if field in ("linkedin", "portfolio") and value and not URL_RE.match(value):
                errors.append(f"{label} must start with https://")
            new[field] = value
        changes = {}
        for field in SECRET_LABELS:
            value = (f.get(field) or "").strip()
            if f.get(f"remove_{field}"):
                changes[field] = None
            elif value:
                if field == "gmail_app_password":
                    value = value.replace(" ", "")
                    if not re.fullmatch(r"[A-Za-z]{16}", value):
                        errors.append("A Gmail app password is 16 letters (spaces are fine).")
                        continue
                elif len(value) > 300 or re.search(r"\s", value):
                    errors.append(f"That {SECRET_LABELS[field][0]} doesn't look right.")
                    continue
                changes[field] = value
        if changes and not can_store_keys:
            errors.append("Keys can't be saved yet: the dashboard's owner needs to set SECRETS_KEY.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("profile.html", active="profile", nav=nav, p={**current, **dict(f)},
                                   fields=PROFILE_FIELDS, secret_labels=SECRET_LABELS,
                                   keys_set=set(current.get("keys_set", [])), can_store_keys=can_store_keys,
                                   steps=setup_steps())
        if changes:
            new["keys_set"] = state_repo.update_secrets(changes, "Update keys via dashboard")
        state_repo.save_profile(new, "Update profile via dashboard")
        flash("Profile saved." + (" Keys updated." if changes else ""), "success")
        return redirect(url_for("profile"))

    if not current.get("email") and g.member:
        current = {**current, "email": g.member["email"]}
    return render_template("profile.html", active="profile", nav=nav, p=current, fields=PROFILE_FIELDS,
                           secret_labels=SECRET_LABELS, keys_set=set(current.get("keys_set", [])),
                           can_store_keys=can_store_keys, steps=setup_steps())


def invite_send_failure(e):
    """A short, non-secret reason an invite email failed, for the owner to act on."""
    if isinstance(e, KeyError):
        return f"{e.args[0]} isn't set in the dashboard's environment"
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "Gmail rejected GMAIL_APP_PASSWORD; make a new app password and update it in Vercel"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "Gmail refused that address"
    if isinstance(e, (TimeoutError, OSError)) and not isinstance(e, smtplib.SMTPException):
        return f"couldn't reach Gmail: {type(e).__name__}"
    return f"Gmail error: {type(e).__name__}"


# --- People (owner only) ---------------------------------------------------------------

@app.route("/people", methods=["GET", "POST"])
@login_required
@owner_required
def people():
    _, nav = load_data()
    import secretbox
    if request.method == "POST":
        action = request.form.get("action")
        email = (request.form.get("email") or "").strip().lower()
        if action == "invite":
            if not EMAIL_RE.match(email):
                flash("Enter a valid email address.", "error")
            elif email == ALLOWED_EMAIL:
                flash("That's you.", "error")
            else:
                db.add_invite(email, datetime.now(timezone.utc).isoformat())
                link = url_for("signup", email=email, _external=True)
                try:
                    send_email(email, "You're invited to Outbound",
                               f"You've been invited to use Outbound, a job-application pipeline that finds "
                               f"companies, writes tailored emails and cover letters, and follows up for you.\n\n"
                               f"Create your account here:\n{link}\n\n"
                               f"You'll add your own CV, Gmail app password and API keys after signing up.")
                    flash(f"Invited {email}. They've been emailed a sign-up link.", "success")
                except (KeyError, smtplib.SMTPException, OSError) as e:
                    reason = invite_send_failure(e)
                    app.logger.warning("Invite email to %s failed: %r", email, e)
                    flash(f"Invited {email}, but the invite email couldn't be sent ({reason}). "
                          f"Send them this link: {link}", "success")
        elif action in ("pause", "resume"):
            member = db.get_member(email)
            if not member:
                flash(f"{email} hasn't signed up yet, so there's no pipeline to {action}.", "error")
            elif action == "pause":
                set_member_paused(member["tenant"], True, "Pipeline paused by owner")
                flash(f"Paused {email}'s pipeline: nothing is searched or sent for them. They can still sign in "
                      f"and see their data.", "success")
            else:
                set_member_paused(member["tenant"], False, "Pipeline resumed by owner")
                flash(f"Resumed {email}'s pipeline. It runs again on the next schedule.", "success")
        elif action == "remove":
            # The rarely needed last resort: revoke the login itself. Data is kept and
            # the pipeline paused; inviting them again restores both.
            tenant = db.remove_access(email)
            if tenant:
                set_member_paused(tenant, True, "Access removed by owner; pipeline paused")
                flash(f"Revoked {email}'s sign-in and paused their pipeline. Their data is kept in "
                      f"tenants/{tenant}/; invite them again to restore everything.", "success")
            else:
                flash(f"Cancelled the invite for {email}.", "success")
        return redirect(url_for("people"))
    invites = safe_call(db.list_invites, fallback=[])
    for i in invites:
        i["paused"] = False
        if i["tenant"]:
            state_repo.set_tenant(i["tenant"])
            try:
                i["paused"] = bool(safe_call(state_repo.get_profile, fallback={}).get("paused"))
            finally:
                state_repo.set_tenant("")
    return render_template("people.html", active="people", nav=nav, invites=invites,
                           keys_ready=secretbox.available())


if __name__ == "__main__":
    app.run(debug=True)
