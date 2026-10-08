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
TRACKS = ["devops", "software", "sysadmin"]
STATUSES = ["Pending", "Approved", "Review", "Skipped", "Sent", "Followup-Sent", "Failed", "Rejected"]
RESET_TOKEN_MAX_AGE = 3600  # 1 hour

# Mirrors the pipeline: outreach_pipeline.DAILY_SEND_LIMIT and the two workflows' cron.
DAILY_SEND_LIMIT = 15
SCRAPE_WORKFLOW = "scrape-pipeline.yml"
SEND_WORKFLOW = "send-pipeline.yml"
SEND_HOUR_UTC = 9
SCRAPE_EVERY_HOURS = 6

DEFAULT_RECIPIENT = "The Recruiting and Technology Team"
DEFAULT_ADDRESS = "Accra, Ghana"

# Must match DEFAULT_SEARCH_QUERIES in outreach_pipeline.py: what the scrape
# workflow uses until search_queries.txt exists in the companion repo.
DEFAULT_QUERIES_TEXT = """# One search per line. Optional "| N" sets how many results (1-25, default 10).
# Lines starting with # are ignored.
cloud and devops engineering companies Accra Ghana | 10
fintech and payments companies hiring engineers Ghana | 10
"""

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --- Auth ----------------------------------------------------------------

def serializer():
    return URLSafeTimedSerializer(app.secret_key, salt="password-reset")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login"))
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
            session["authed"] = True
            return redirect(url_for("overview"))
        flash("Incorrect email or password.", "error")
    return render_template("login.html", email=ALLOWED_EMAIL)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        auth = db.load_auth()
        if email == ALLOWED_EMAIL and auth.get("password_hash"):
            token = serializer().dumps({"h": auth["password_hash"][:16]})
            link = request.url_root.rstrip("/") + url_for("reset_password", token=token)
            try:
                send_email(
                    ALLOWED_EMAIL,
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

    if not auth.get("password_hash") or payload.get("h") != auth["password_hash"][:16]:
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
            db.save_auth(generate_password_hash(pw), datetime.now(timezone.utc))
            flash("Password updated. Log in below.", "success")
            return redirect(url_for("login"))
    return render_template("reset.html")


# --- Queue helpers ------------------------------------------------------------

def is_yes(value):
    return str(value or "").strip().lower() == "yes"


def has_email(c):
    return bool(c.get("email")) and c["email"] != "no_email_found"


def bucket_of(c):
    """Which page a row belongs on, in terms of what (if anything) it needs from you."""
    s = c["status"]
    if s in ("Review", "Failed") or (s == "Skipped" and not has_email(c)):
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
    if c["status"] == "Review":
        return "name"
    if c["status"] == "Failed":
        return "failed"
    return "email"


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


@app.context_processor
def shell_context():
    now = utcnow()
    return {
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


def triage_item(c):
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
        "added": c["date_added"],
        "name_suggestion": guess if guess and guess.lower() != c["company_name"].strip().lower() else "",
        "email_suggestions": [e for e in (f"{p}@{domain}" for p in ("careers", "hr", "jobs", "info")) if e != current] if domain else [],
        "google": "https://www.google.com/search?q=" + quote_plus(f'"{domain or c["company_name"]}" email careers OR hr OR contact'),
        "linkedin": "https://www.linkedin.com/search/results/companies/?keywords=" + quote_plus(guess or c["company_name"]),
    }


def safe_next(default_endpoint):
    """Redirect back to the page a form came from, but only to a path on this site."""
    nxt = request.form.get("next", "")
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
    hours = (now.hour // SCRAPE_EVERY_HOURS + 1) * SCRAPE_EVERY_HOURS
    return start_of_day + timedelta(hours=hours)


def next_send_run(now):
    today = now.replace(hour=SEND_HOUR_UTC, minute=0, second=0, microsecond=0)
    return today if today > now else today + timedelta(days=1)


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
        "overview.html", active="overview", nav=nav,
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
    order = {"email": 0, "name": 1, "failed": 2}
    items = sorted(
        (triage_item(c) for c in companies if c["bucket"] == "attention"),
        key=lambda i: (order[i["reason"]], i["name"].lower()),
    )
    return render_template("attention.html", active="attention", nav=nav, items=items)


@app.route("/attention/bulk", methods=["POST"])
@login_required
def attention_bulk():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    slugs = set(data.get("slugs") or [])
    if action not in ("drop", "retry") or not slugs:
        return jsonify(ok=False, message="Nothing selected."), 400

    def fn(rows):
        done = []
        for r in rows:
            slug = state_repo.slugify(r["company_name"])
            if slug not in slugs:
                continue
            if action == "drop":
                r["status"] = "Rejected"
                done.append(slug)
            elif r["status"] == "Failed":
                r["status"] = "Approved"
                done.append(slug)
        return done

    done = state_repo.mutate_queue(fn, f"{'Drop' if action == 'drop' else 'Retry'} {len(slugs)} leads via dashboard")
    if action == "drop":
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
                "track": form.get("track") if form.get("track") in TRACKS else TRACKS[0],
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
        company_types=COMPANY_TYPES, tracks=TRACKS,
    )


@app.route("/companies/<slug>", methods=["GET", "POST"])
@login_required
def company_detail(slug):
    companies, nav = load_data()
    company = next((c for c in companies if c["slug"] == slug), None)
    if company is None:
        flash("That company isn't in the queue any more.", "error")
        return redirect(url_for("companies_list"))

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
                "track": f.get("track") if f.get("track") in TRACKS else company["track"],
                "recipient_name": f.get("recipient_name", "").strip() or DEFAULT_RECIPIENT,
                "company_address": f.get("company_address", "").strip(),
                "status": status,
                "replied": "yes" if f.get("replied") else "",
                "bounced": "yes" if f.get("bounced") else "",
                "lead_reviewed": "yes",
            }
            state_repo.update_company(slug, changes, f"Edit {name} via dashboard")
        except (ValueError, KeyError) as e:
            flash(str(e) if isinstance(e, ValueError) else "That company isn't in the queue any more.", "error")
            return redirect(url_for("company_detail", slug=slug))
        flash("Saved.", "success")
        return redirect(url_for("company_detail", slug=state_repo.slugify(name)))

    return render_template(
        "company_detail.html", active="companies", nav=nav, c=company,
        statuses=STATUSES, company_types=COMPANY_TYPES, tracks=TRACKS,
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


@app.route("/companies/<slug>/resolve", methods=["POST"])
@login_required
def company_resolve(slug):
    """The one-step fix on the Needs you page: supply what was missing and requeue it."""
    reason = request.form.get("reason")
    name = request.form.get("company_name", "").strip()
    try:
        email = clean_email(request.form.get("email"))
        changes = {"lead_reviewed": "yes"}
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

def parse_queries(text):
    """Validate search_queries.txt the same way outreach_pipeline.load_search_queries reads it."""
    queries = []
    for n, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        query, _, limit = line.partition("|")
        query = query.strip()
        if not query:
            raise ValueError(f"Line {n} has a limit but no search text.")
        if limit.strip():
            if not limit.strip().isdigit() or not 1 <= int(limit) <= 25:
                raise ValueError(f"Line {n}: the number after | must be between 1 and 25.")
        queries.append(query)
    if not queries:
        raise ValueError("Add at least one search.")
    return queries


@app.route("/settings")
@login_required
def settings():
    _, nav = load_data()
    data, _ = state_repo.get_cv()
    queries_text = state_repo.get_queries_text()
    return render_template(
        "settings.html", active="settings", nav=nav,
        has_cv=data is not None,
        cv_size_kb=max(0.1, round(len(data) / 1024, 1)) if data is not None else None,
        queries_text=queries_text if queries_text is not None else DEFAULT_QUERIES_TEXT,
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
        queries = parse_queries(text)
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


@app.route("/settings/cv", methods=["POST"])
@login_required
def settings_cv():
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
            state_repo.save_cv(data, "Update CV via dashboard")
            flash("CV updated. The next send attaches this version.", "success")
    return redirect(url_for("settings") + "#cv")


@app.route("/settings/cv/download")
@login_required
def cv_download():
    data, _ = state_repo.get_cv()
    if data is None:
        flash("No CV on file yet.", "error")
        return redirect(url_for("settings"))
    return Response(
        data, mimetype="application/pdf",
        headers={"Content-Disposition": "inline; filename=current_cv.pdf"},
    )


if __name__ == "__main__":
    app.run(debug=True)
