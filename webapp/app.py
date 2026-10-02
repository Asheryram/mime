import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps

from flask import Flask, request, session, redirect, url_for, render_template, flash, Response
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

ALLOWED_EMAIL = os.environ.get("ALLOWED_EMAIL", "ashertettehabotsi@gmail.com").strip().lower()
COMPANY_TYPES = ["fintech", "consultancy", "isp", "bank", "general_startup"]
TRACKS = ["devops", "software", "sysadmin"]
RESET_TOKEN_MAX_AGE = 3600  # 1 hour


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
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
        smtp.login(sender, password)
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
            send_email(
                ALLOWED_EMAIL,
                "Reset your Outreach Triage Desk password",
                f"Reset your password here (expires in 1 hour):\n\n{link}\n\n"
                f"If you didn't request this, ignore this email.",
            )
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


def bucket(companies):
    new_leads, name_fixes, missing_email, sent_for_triage = [], [], [], []
    for c in companies:
        truthy = lambda k: str(c.get(k) or "").strip().lower() == "yes"
        if c["status"] == "Review":
            name_fixes.append(c)
        elif c["status"] == "Skipped" and c["email"] == "no_email_found":
            missing_email.append(c)
        elif c["status"] == "Pending" and c["email"] and c["email"] != "no_email_found" and not truthy("lead_reviewed"):
            new_leads.append(c)
        elif c["status"] in ("Sent", "Followup-Sent") and not truthy("replied") and not truthy("bounced"):
            sent_for_triage.append(c)
    return new_leads, name_fixes, missing_email, sent_for_triage


def load_dashboard_data():
    """Shared by every logged-in page so the nav tabs always show live counts,
    even on a page that only renders one section in full."""
    companies = state_repo.load_queue()
    for c in companies:
        c["slug"] = state_repo.slugify(c["company_name"])
    new_leads, name_fixes, missing_email, sent_for_triage = bucket(companies)
    counts = {}
    for c in companies:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    return {
        "companies": sorted(companies, key=lambda c: c["company_name"].lower()),
        "new_leads": new_leads,
        "name_fixes": name_fixes,
        "missing_email": missing_email,
        "sent_for_triage": sent_for_triage,
        "counts": counts,
        "total": len(companies),
    }


# Where an action redirects back to, picked from the form's hidden "next"
# field. Whitelisted so a crafted field name can't send a redirect anywhere
# other than one of this app's own pages.
NEXT_PAGES = {
    "overview", "leads_new", "leads_name_fix", "leads_missing_email",
    "leads_sent", "companies_all", "cv",
}


def redirect_next(default):
    nxt = request.form.get("next", "")
    return redirect(url_for(nxt if nxt in NEXT_PAGES else default))


@app.route("/")
@login_required
def overview():
    return render_template("overview.html", active="overview", **load_dashboard_data())


@app.route("/leads/new")
@login_required
def leads_new():
    return render_template(
        "leads_new.html", active="leads_new",
        company_types=COMPANY_TYPES, tracks=TRACKS, **load_dashboard_data(),
    )


@app.route("/leads/name-fix")
@login_required
def leads_name_fix():
    return render_template("leads_name_fix.html", active="leads_name_fix", **load_dashboard_data())


@app.route("/leads/missing-email")
@login_required
def leads_missing_email():
    return render_template("leads_missing_email.html", active="leads_missing_email", **load_dashboard_data())


@app.route("/leads/sent")
@login_required
def leads_sent():
    return render_template("leads_sent.html", active="leads_sent", **load_dashboard_data())


@app.route("/companies")
@login_required
def companies_all():
    return render_template("companies_all.html", active="companies_all", **load_dashboard_data())


@app.route("/cv", methods=["GET", "POST"])
@login_required
def cv():
    if request.method == "POST":
        f = request.files.get("cv")
        if not f or not f.filename:
            flash("Choose a PDF file first.", "error")
            return redirect(url_for("cv"))
        if not f.filename.lower().endswith(".pdf") or f.mimetype != "application/pdf":
            flash("That doesn't look like a PDF.", "error")
            return redirect(url_for("cv"))
        data = f.read()
        if not data.startswith(b"%PDF-"):
            flash("That doesn't look like a valid PDF file.", "error")
            return redirect(url_for("cv"))
        state_repo.save_cv(data, "Update CV via triage dashboard")
        flash("CV updated. The next pipeline run will attach this version.", "success")
        return redirect(url_for("cv"))

    data, _ = state_repo.get_cv()
    size_kb = round(len(data) / 1024, 1) if data else None
    return render_template(
        "cv.html", active="cv", size_kb=size_kb, has_cv=data is not None, **load_dashboard_data(),
    )


@app.route("/cv/download")
@login_required
def cv_download():
    data, _ = state_repo.get_cv()
    if data is None:
        flash("No CV on file yet.", "error")
        return redirect(url_for("cv"))
    return Response(
        data, mimetype="application/pdf",
        headers={"Content-Disposition": "inline; filename=current_cv.pdf"},
    )


@app.route("/action/<slug>/approve", methods=["POST"])
@login_required
def action_approve(slug):
    state_repo.update_company(slug, {
        "company_name": request.form.get("company_name", "").strip(),
        "company_type": request.form.get("company_type", "general_startup"),
        "track": request.form.get("track", "devops"),
        "lead_reviewed": "yes",
    }, f"Approve lead {slug}")
    flash("Lead approved.", "success")
    return redirect_next("leads_new")


@app.route("/action/<slug>/reject", methods=["POST"])
@login_required
def action_reject(slug):
    state_repo.update_company(slug, {"status": "Rejected"}, f"Reject lead {slug}")
    flash("Lead dropped.", "success")
    return redirect_next("leads_new")


@app.route("/action/<slug>/fix-name", methods=["POST"])
@login_required
def action_fix_name(slug):
    name = request.form.get("company_name", "").strip()
    if not name:
        flash("Name can't be empty.", "error")
        return redirect_next("leads_name_fix")
    state_repo.update_company(slug, {
        "company_name": name, "status": "Pending", "lead_reviewed": "yes",
    }, f"Fix company name for {slug}")
    flash("Name saved and approved.", "success")
    return redirect_next("leads_name_fix")


@app.route("/action/<slug>/save-email", methods=["POST"])
@login_required
def action_save_email(slug):
    email = request.form.get("email", "").strip()
    if "@" not in email:
        flash("Enter a valid email.", "error")
        return redirect_next("leads_missing_email")
    state_repo.update_company(slug, {
        "email": email, "status": "Pending", "lead_reviewed": "yes",
    }, f"Add manual email for {slug}")
    flash("Email saved and approved.", "success")
    return redirect_next("leads_missing_email")


@app.route("/action/<slug>/mark-replied", methods=["POST"])
@login_required
def action_mark_replied(slug):
    state_repo.update_company(slug, {"replied": "yes"}, f"Mark {slug} replied")
    flash("Marked as replied.", "success")
    return redirect_next("leads_sent")


@app.route("/action/<slug>/mark-bounced", methods=["POST"])
@login_required
def action_mark_bounced(slug):
    state_repo.update_company(slug, {"bounced": "yes"}, f"Mark {slug} bounced")
    flash("Marked as bounced.", "success")
    return redirect_next("leads_sent")


if __name__ == "__main__":
    app.run(debug=True)
