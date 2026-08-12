import os
import sys
import time
import random
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
from datetime import datetime, timedelta

from config import config
from outreach_pipeline import load_queue, save_queue
import applicant

CV_PATH = applicant.CV_PATH

# Wait this many days after the cold email before checking in.
FOLLOWUP_AFTER_DAYS = 6
FOLLOWUP_DAILY_LIMIT = 10

# Domains that hard-bounced. Add to this as bounces come in so the pipeline
# stops burning sender reputation on dead addresses.
BOUNCED_DOMAINS = []

# Companies that already replied - never chase these automatically.
# Substring match against the company name, lowercase.
REPLIED_COMPANIES = []


def get_followup_candidates(today=None):
    """Rows sent at least FOLLOWUP_AFTER_DAYS ago that have not replied or bounced."""
    queue = load_queue()
    candidates = []

    today = today or datetime.now()
    threshold_date = today - timedelta(days=FOLLOWUP_AFTER_DAYS)

    for r in queue:
        # Only chase a cold email that actually went out and was not answered.
        if r["status"] != "Sent" or not r["date_sent"]:
            continue

        comp_name_lower = r["company_name"].lower().strip()
        email_lower = r["email"].lower().strip()

        if REPLIED_COMPANIES and any(rep in comp_name_lower for rep in REPLIED_COMPANIES):
            continue

        if BOUNCED_DOMAINS and any(b in email_lower for b in BOUNCED_DOMAINS):
            continue

        date_str = r["date_sent"].split()[0]
        try:
            sent_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            print(f"[Followup] Skipping '{r['company_name']}': unparseable date_sent '{r['date_sent']}'")
            continue

        if sent_date <= threshold_date:
            candidates.append(r)

    return candidates


def send_followup_email(sender_email, password, r, dry_run=False):
    recipient_email = r["email"]
    company_name = r["company_name"]
    recipient_name = r["recipient_name"]

    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = recipient_email
    # Must echo the subject the original pitch used, or Gmail treats the
    # follow-up as a new conversation even with the threading headers set.
    track = r.get("track") or applicant.DEFAULT_TRACK
    msg["Subject"] = f"Re: {applicant.subject_for(track)}"
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@")[-1])
    msg["Date"] = formatdate(localtime=True)

    # Real threading: without these headers Gmail shows the follow-up as a new
    # conversation despite the "Re:" prefix. message_id is blank for rows sent
    # before this column existed, in which case we fall back to subject-only.
    original_id = r.get("message_id", "").strip()
    if original_id:
        msg["In-Reply-To"] = original_id
        msg["References"] = original_id

    msg.set_content(
        applicant.build_followup_body(company_name, recipient_name, r["date_sent"], track)
    )

    if os.path.exists(CV_PATH):
        with open(CV_PATH, "rb") as f:
            file_data = f.read()
            file_name = os.path.basename(CV_PATH)
        msg.add_attachment(file_data, maintype="application", subtype="pdf", filename=file_name)
    else:
        print(f"Error: CV file not found at {CV_PATH}")
        return False

    if dry_run:
        threading = "threaded" if original_id else "no message_id, will start a new thread"
        print(f"[DRY-RUN] Would send follow-up to {recipient_email} ({company_name}) - {threading}")
        return True

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
            smtp.login(sender_email, password)
            smtp.sendmail(sender_email, recipient_email, msg.as_string())
        print(f"Successfully sent follow-up to {recipient_email}")
        return True
    except Exception as e:
        print(f"SMTP Error sending follow-up to {recipient_email}: {e}")
        return False


def run_followups(limit=FOLLOWUP_DAILY_LIMIT, dry_run=False):
    sender_email = config.get("SENDER_EMAIL")
    password = config.get("GMAIL_APP_PASSWORD")

    if not sender_email or not password:
        print("Error: SENDER_EMAIL or GMAIL_APP_PASSWORD missing from .env.")
        sys.exit(1)

    candidates = get_followup_candidates()
    print(f"Found {len(candidates)} total candidates for follow-ups.")

    if not candidates:
        print("No candidates to follow up with.")
        return

    to_send = candidates[:limit]
    if len(candidates) > limit:
        print(f"Daily cap is {limit}; {len(candidates) - limit} candidates deferred to the next run.")
    print(f"Processing batch of {len(to_send)} follow-ups...")

    queue = load_queue()
    sent_count = 0

    for idx, c in enumerate(to_send):
        success = send_followup_email(sender_email, password, c, dry_run=dry_run)

        if success and not dry_run:
            for q_row in queue:
                if q_row["company_name"] == c["company_name"] and q_row["email"] == c["email"]:
                    q_row["status"] = "Followup-Sent"
                    q_row["date_followup"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    break
            save_queue(queue)
            sent_count += 1

            # Space out sends to stay under Gmail's spam heuristics.
            if idx < len(to_send) - 1:
                delay = random.randint(30, 60)
                print(f"Waiting {delay} seconds...")
                time.sleep(delay)

    if dry_run:
        print(f"Dry run complete. {len(to_send)} follow-ups would be sent. Run 'py followup_pipeline.py run' to send.")
    else:
        print(f"Follow-up batch completed. Sent: {sent_count}.")


if __name__ == "__main__":
    # Dry-run unless "run" is passed explicitly.
    is_live = len(sys.argv) > 1 and sys.argv[1] == "run"
    run_followups(dry_run=not is_live)
