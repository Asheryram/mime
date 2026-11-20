"""
Mark replies and bounces automatically by reading the sending Gmail inbox.

Runs before each send (so a follow-up never chases someone who answered) and
after each scrape. Uses the same SENDER_EMAIL and GMAIL_APP_PASSWORD the
pipeline sends with; personal Gmail has IMAP on permanently since Jan 2025.

Read-only and headers-only by design:
- the mailbox is opened with EXAMINE (read-only) and messages are fetched
  with BODY.PEEK, so nothing is marked read, moved, or changed;
- only headers are read, except for Gmail's own "Delivery failed" notices,
  whose body names the failed address.

What counts:
- Reply: answers one of your emails (its In-Reply-To/References carry the
  stored Message-ID; replies to a follow-up do too, since follow-ups
  reference the original), or comes from the company's own domain after you
  emailed them. For a company that uses a free mailbox (Gmail etc.), only
  that exact address counts, or every Gmail user would match.
- Not a reply: auto-replies and autoresponders (Auto-Submitted, X-Autoreply,
  Precedence: auto_reply/bulk/list, "Out of office"/"Automatic reply"
  subjects) and no-reply senders ("Thanks for your application" from
  noreply@acme.com). They'd make your reply rate look better than it is.
- Bounce: a notice from mailer-daemon/postmaster naming the address, or
  quoting the Message-ID, of an email you sent.

Flags are only ever set, never cleared: a mark you made by hand always wins.
"""
import email
import imaplib
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

IMAP_HOST = "imap.gmail.com"
MAX_MESSAGES = 3000          # newest first; a backstop for very busy inboxes
LOOKBACK_DAYS = 120          # never search further back than this

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
    "proton.me", "protonmail.com", "zoho.com", "gmx.com", "mail.com",
}
NO_REPLY = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|notifications?|mailer|bounces?|alerts?)\b", re.I)
AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(matic)?[- ]?reply|auto:|out of (the )?office|ooo\b|"
    r"away from (the )?office|on leave|thank you for (contacting|your (email|message)))", re.I)
BOUNCE_FROM = re.compile(r"^(mailer-daemon|postmaster)@", re.I)
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")

_SECOND_LEVEL = {"com", "co", "org", "net", "edu", "gov", "ac", "or", "ltd", "biz"}

HEADERS = ("FROM TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES AUTO-SUBMITTED "
           "X-AUTOREPLY X-AUTORESPOND X-AUTORESPONSE PRECEDENCE X-FAILED-RECIPIENTS")


def org_of(domain):
    """'mail.hubtel.com.gh' -> 'hubtel.com.gh' (same rule as the scraper)."""
    parts = (domain or "").lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_org(a, b):
    """Same organisation, allowing hubtel.com vs hubtel.com.gh."""
    oa, ob = org_of(a), org_of(b)
    return oa == ob or (oa.split(".")[0] == ob.split(".")[0] and oa.split(".")[0] != "")


def _text(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _ids(value):
    return set(re.findall(r"<[^<>\s]+>", value or ""))


def is_auto(msg):
    if (msg.get("Auto-Submitted") or "no").strip().lower() != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond") or msg.get("X-Autoresponse"):
        return True
    if (msg.get("Precedence") or "").strip().lower() in ("auto_reply", "bulk", "junk", "list"):
        return True
    return bool(AUTO_SUBJECT.match(_text(msg.get("Subject"))))


def _sent_at(r):
    try:
        return datetime.strptime(r.get("date_sent", "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _msg_date(msg):
    try:
        d = parsedate_to_datetime(msg.get("Date"))
        return d.replace(tzinfo=None) - (d.utcoffset() or timedelta(0)) if d.tzinfo else d
    except Exception:
        return None


def open_rows(queue):
    """Rows you've emailed that aren't yet marked replied or bounced."""
    return [r for r in queue
            if r["status"] in ("Sent", "Followup-Sent") and r.get("email") and "@" in r["email"]
            and r.get("replied", "").strip().lower() != "yes"
            and r.get("bounced", "").strip().lower() != "yes"]


def classify(messages, rows, me):
    """
    Pure matching, no network. messages: list of (email.message.Message,
    body_text_or_None). Returns {row index: ("replied"|"bounced", note)}.
    """
    by_id = {}
    for i, r in enumerate(rows):
        if r.get("message_id"):
            by_id[r["message_id"].strip()] = i
    found = {}

    for msg, body in messages:
        sender = parseaddr(msg.get("From", ""))[1].lower()
        if not sender or sender == me.lower():
            continue  # your own sent mail
        when = _msg_date(msg)
        refs = _ids(msg.get("In-Reply-To")) | _ids(msg.get("References"))

        if BOUNCE_FROM.match(sender):
            failed = {a.lower() for a in EMAIL_RE.findall(msg.get("X-Failed-Recipients", "") + " " + (body or ""))}
            quoted = _ids(body) | refs
            for i, r in enumerate(rows):
                if i in found:
                    continue
                if r["email"].lower() in failed or (r.get("message_id") and r["message_id"].strip() in quoted):
                    found[i] = ("bounced", f"bounce notice on {when:%Y-%m-%d}" if when else "bounce notice")
            continue

        if is_auto(msg) or NO_REPLY.match(sender.split("@")[0]):
            continue

        sender_domain = sender.rsplit("@", 1)[-1]
        for i, r in enumerate(rows):
            if i in found:
                continue
            sent = _sent_at(r)
            if sent and when and when < sent - timedelta(minutes=5):
                continue  # mail from before you wrote to them
            target = r["email"].lower()
            target_domain = target.rsplit("@", 1)[-1]
            threaded = r.get("message_id") and r["message_id"].strip() in refs
            if target_domain in FREE_MAIL:
                matched = threaded or sender == target
            else:
                matched = threaded or same_org(sender_domain, target_domain)
            if matched:
                subject = _text(msg.get("Subject"))[:80]
                found[i] = ("replied", f"reply from {sender}" + (f" on {when:%Y-%m-%d}" if when else "")
                            + (f": “{subject}”" if subject else ""))
    return found


def _all_mail_folder(imap):
    """Gmail's All Mail (replies may be archived or labelled), whatever its localised name."""
    typ, data = imap.list()
    for line in data or []:
        line = line.decode(errors="replace") if isinstance(line, bytes) else line
        if "\\All" in line:
            m = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if m:
                return m.group(1)
    return "INBOX"


def fetch_messages(user, password, since, host=IMAP_HOST, imap_factory=imaplib.IMAP4_SSL):
    """Headers of messages since `since` (plus bodies of bounce notices), read-only."""
    imap = imap_factory(host)
    try:
        imap.login(user, password)
        folder = _all_mail_folder(imap)
        typ, _ = imap.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        nums = (data[0].split() if data and data[0] else [])[-MAX_MESSAGES:]
        out = []
        for chunk in range(0, len(nums), 200):
            ids = b",".join(nums[chunk:chunk + 200]).decode()
            typ, resp = imap.fetch(ids, f"(BODY.PEEK[HEADER.FIELDS ({HEADERS})])")
            for part in resp or []:
                if isinstance(part, tuple):
                    out.append([part[0], email.message_from_bytes(part[1]), None])
        for item in out:
            if BOUNCE_FROM.match(parseaddr(item[1].get("From", ""))[1]):
                num = item[0].split()[0].decode()
                typ, resp = imap.fetch(num, "(BODY.PEEK[TEXT])")
                for part in resp or []:
                    if isinstance(part, tuple):
                        item[2] = part[1][:200_000].decode(errors="replace")
        return [(m, b) for _, m, b in out]
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def check_inbox(queue, user, password, **kw):
    """
    Marks rows in `queue` (in place). Returns (replied, bounced) company names.
    Raises on login/network failure: the caller decides whether that's fatal.
    """
    rows = open_rows(queue)
    # Referrals: the application sent on to the address a company gave you.
    # Watched separately, since the company row is usually already "replied".
    for r in queue:
        if (r.get("referral_status") == "sent" and "@" in (r.get("referral_email") or "")
                and r.get("referral_replied", "").strip().lower() != "yes"):
            rows.append({"company_name": r["company_name"], "email": r["referral_email"].strip(),
                         "message_id": r.get("referral_message_id", ""),
                         "date_sent": r.get("referral_date", ""), "_referral_of": r})
    if not rows:
        return [], []
    sent_dates = [d for d in (_sent_at(r) for r in rows) if d]
    since = max(min(sent_dates) if sent_dates else datetime.utcnow(),
                datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)) - timedelta(days=1)
    messages = fetch_messages(user, password, since, **kw)
    replied, bounced = [], []
    for i, (kind, note) in classify(messages, rows, user).items():
        r = rows[i]
        if "_referral_of" in r:
            row = r["_referral_of"]
            if kind == "replied":
                row["referral_replied"] = "yes"
            else:
                row["referral_status"] = "bounced"
            row["referral_note"] = f"auto-detected: {note}"
            (replied if kind == "replied" else bounced).append(f"{r['company_name']} (referral)")
            continue
        r[kind] = "yes"
        r["reply_note"] = f"auto-detected: {note}"
        (replied if kind == "replied" else bounced).append(r["company_name"])
    return replied, bounced
