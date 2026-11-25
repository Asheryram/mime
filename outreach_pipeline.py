import os
import csv
import sys
import time
import random
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate
from urllib.parse import urlparse
from config import config
from exa_search import ExaSearch
from apify_scraper import ApifyScraper
from cover_letter_generator import CoverLetterGenerator
from email_sender import EmailSender
import applicant
import inbox_check
import review_email
import role_match
import smtp_client
from smtp_client import GmailAuthError

QUEUE_FILE = "outreach_queue.csv"
LOG_FILE = "outreach_log.csv"
LETTERS_DIR = "generated_letters"

# Canonical column order for outreach_queue.csv. followup_pipeline imports this
# so both writers agree on the schema.
QUEUE_FIELDS = [
    "company_name", "website", "email", "company_type",
    "recipient_name", "company_address", "status",
    "date_added", "date_sent", "date_followup", "message_id",
    "track", "lead_reviewed", "replied", "bounced", "review_notified",
    "role_reason", "role_review", "email_note", "reply_note",
    # A company that answered "send your application to X": the follow-on
    # email to X, queued from the dashboard. One row per company throughout.
    "referral_email", "referral_by", "referral_attach", "referral_status",
    "referral_date", "referral_message_id", "referral_replied", "referral_note",
    # What their reply said (interview / later / declined), and for "later",
    # when to check back and how that check-in went.
    "outcome", "outcome_date", "checkback_date", "checkback_status", "checkback_sent_date",
    "checkback_message_id", "checkback_replied", "checkback_note", "checkback_notified"
]

DAILY_SEND_LIMIT = 15

# search_queries.txt (edited from the dashboard's Settings page, stored in the
# companion repo) drives the scheduled scrape. These are used until it exists.
SEARCH_QUERIES_FILE = "search_queries.txt"
# The shipped searches, used until you save your own from the dashboard
# (Settings -> searches). The dashboard shows the same file, so the two agree.
DEFAULT_SEARCH_QUERIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "webapp", "search_queries_default.txt")

# Searches run per scrape. Running every line every 6 hours would re-fetch the
# same results (Exa is mostly stable for a given query) and pay for it; a few
# per run, in rotation, means each run looks somewhere new.
# 2 x ~10 results x 4 runs a day is ~80 new companies a day at most, already
# more than the email lookups (EMAIL_LOOKUPS_PER_RUN x 4) and the weekday send
# cap can absorb; searching more would only grow the backlog.
QUERIES_PER_RUN = 2  # keep in step with SEARCHES_PER_RUN in webapp/app.py

# Initialize services
exa = None
apify = None
generator = CoverLetterGenerator()
sender = None

def get_exa():
    global exa
    if not exa:
        exa = ExaSearch()
    return exa

def get_apify():
    global apify
    if not apify:
        apify = ApifyScraper()
    return apify

def get_sender():
    global sender
    if not sender:
        sender = EmailSender()
    return sender

def load_queue():
    queue = []
    if not os.path.exists(QUEUE_FILE):
        return queue
    with open(QUEUE_FILE, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Backfill columns added after a queue was first created.
            for field in QUEUE_FIELDS:
                row.setdefault(field, "")
            # Rows predating the track column are the original campaign.
            if not row["track"]:
                row["track"] = applicant.DEFAULT_TRACK
            queue.append(row)
    return queue

def save_queue(queue):
    with open(QUEUE_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=QUEUE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in queue:
            writer.writerow(row)

def add_to_queue(company_name, website, email="", company_type="general_startup", recipient="The Recruiting and Technology Team", address=None, status="Pending", track=None, role_reason="", role_review=""):
    queue = load_queue()
    # Deduplicate by company name or website
    for row in queue:
        if row["company_name"].lower() == company_name.lower():
            return False
        if website and row["website"].lower() == website.lower():
            return False
            
    new_row = {
        "company_name": company_name,
        "website": website,
        "email": email,
        "company_type": company_type,
        "recipient_name": recipient,
        "company_address": address if address is not None else applicant.DEFAULT_COMPANY_LOCATION,
        "status": status,
        "date_added": datetime.now().strftime("%Y-%m-%d"),
        "date_sent": "",
        "date_followup": "",
        "message_id": "",
        "track": (track or applicant.DEFAULT_TRACK).strip().lower(),
        "role_reason": role_reason,
        # "yes" = no role clearly fit; waits in Needs you until a human picks.
        "role_review": role_review,
    }
    queue.append(new_row)
    save_queue(queue)
    print(f"[Queue] Added company: {company_name}")
    return True

def import_from_markdown(md_path):
    """
    Imports a hand-curated target list from a markdown file into the queue.

    Expected shape: "## Section Name" headings set the company_type, and each
    "### 1. Company Name" heading beneath becomes a queue row.
    """
    print(f"[Import] Importing companies from {md_path}...")
    if not os.path.exists(md_path):
        print(f"[Import] Error: {md_path} not found.")
        return

    # Standard mapping based on sections in the markdown file
    current_type = "general_startup"
    with open(md_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("## "):
                # Identify section type
                sec = line.lower()
                if "consultancies" in sec:
                    current_type = "consultancy"
                elif "software houses" in sec:
                    current_type = "fintech" # softtribe/itconsortium are transactional/database systems
                elif "isps" in sec or "telecommunications" in sec:
                    current_type = "isp"
                elif "bank" in sec:
                    current_type = "bank"
                elif "power" in sec or "utilities" in sec:
                    current_type = "consultancy" # utility cloud platforms behave like consultancy/infrastructure
                    
            if line.startswith("### "):
                # Found a company: e.g. "### 1. Enterprise Computing Limited (ECL)"
                name = line.replace("###", "").strip()
                # strip number prefixes like "1. " or "2. "
                parts = name.split(". ", 1)
                if len(parts) > 1:
                    name = parts[1]
                    
                # Read description lines below to find details
                # For simplicity, we add it with default values first.
                # The user can edit or we can scrape to enrich.
                add_to_queue(
                    company_name=name,
                    website="", # Scraper/Exa can resolve website URL
                    email="",
                    company_type=current_type,
                    recipient="The Recruiting and Technology Team",
                    address=applicant.DEFAULT_COMPANY_LOCATION
                )

# Aggregators and job boards. A search for "companies hiring cloud engineers"
# surfaces these constantly, but they are listing sites, not employers - cold
# emailing them wastes a send and the row's name is a job title, not a company.
JOB_BOARD_DOMAINS = {
    "bebee.com", "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "jobberman.com", "jobberman.com.gh", "gethiredjobsgh.com", "ghanajobsearch.com",
    "techjobsinghana.com", "ghanacareers.com", "jobsinghana.com", "jobsearchghana.com",
    "myjobmag.com", "brightermonday.com", "workinghana.com", "joblistghana.com",
    "careersinghana.com", "remoteok.com", "wellfound.com", "angel.co", "glints.com",
    # Job-ad aggregators: page titles are job titles ("Senior Frontend
    # Developer At Expresspay Ghana Limited"), not employers.
    "expertini.com", "jooble.org", "jobrapido.com", "careerjet.com", "talent.com",
    "jobzmall.com", "simplyhired.com", "adzuna.com", "jobsora.com", "whatjobs.com",
    "ghanajob.com", "jobwebghana.com", "jobsinafrica.com", "africajobs.net",
    "remotive.com", "weworkremotely.com", "himalayas.app", "otta.com",
}


# Applicant-tracking systems. A company's careers page hosted on one of these
# is the vendor's site: crawling it finds the vendor's own address
# (contact@manatal.com), never the employer's.
ATS_DOMAINS = {
    "manatal.com", "careers-page.com", "workable.com", "apply.workable.com", "lever.co",
    "greenhouse.io", "bamboohr.com", "zohorecruit.com", "smartrecruiters.com", "breezy.hr",
    "recruitee.com", "teamtailor.com", "myworkdayjobs.com", "applytojob.com", "jazzhr.com",
    "ashbyhq.com", "personio.de", "personio.com", "jobvite.com", "icims.com", "rippling.com",
}
JOB_BOARD_DOMAINS |= ATS_DOMAINS

# Free mailbox providers. Small firms often use these as their real contact
# address, so an address here found on the company's own site is accepted.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "aol.com",
    "proton.me", "protonmail.com", "zoho.com", "gmx.com", "mail.com",
}

# Second-level suffixes, so "hubtel.com.gh" reduces to hubtel.com.gh, not com.gh.
_SECOND_LEVEL = {"com", "co", "org", "net", "edu", "gov", "ac", "or", "ltd", "biz"}


def registrable_domain(host):
    """'careers.hubtel.com.gh' -> 'hubtel.com.gh'; 'mail.example.com' -> 'example.com'."""
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def email_fits_site(email, website):
    """
    Whether an address scraped from a company's site plausibly belongs to that
    company: same organisation domain, or a free mailbox (small firms use them).
    Anything else - a vendor, an agency, a parent group - needs a human to decide.
    """
    domain = email.rsplit("@", 1)[-1].lower()
    if domain in FREE_MAIL_DOMAINS:
        return True
    if registrable_domain(domain) in ATS_DOMAINS:
        return False
    email_org, site_org = registrable_domain(domain), registrable_domain(urlparse(website).netloc)
    # Same name label across TLDs counts too: Ghanaian firms often run
    # hubtel.com.gh as the site and hubtel.com for mail.
    return email_org == site_org or email_org.split(".")[0] == site_org.split(".")[0]


def choose_email(emails, website):
    """
    Pick the address to write to. Returns (email, note): email is "" when only
    addresses from other organisations were found, and note says what was
    rejected so the dashboard can show it.
    """
    valid = [e.strip() for e in emails if "@" in e and "." in e.split("@")[-1]]
    fitting = [e for e in valid if email_fits_site(e, website)]
    if not fitting:
        others = sorted({e.lower() for e in valid})
        note = (f"only found {', '.join(others[:3])}, not on this company's domain"
                if others else "")
        return "", note
    # Prefer the company's own domain over a free mailbox, then a hiring inbox.
    own = [e for e in fitting if e.rsplit("@", 1)[-1].lower() not in FREE_MAIL_DOMAINS] or fitting
    for e in own:
        if any(k in e.lower() for k in ["hr", "career", "job", "recruit", "hire", "people", "contact"]):
            return e, ""
    return own[0], ""

# Page titles that carry no company identity. If a title reduces to one of
# these, the name has to be set by hand before the row can safely be emailed.
GENERIC_TITLES = {
    "home", "homepage", "about", "about us", "welcome", "index", "contact",
    "contact us", "services", "our services", "careers", "jobs", "blog",
}

# Separators that typically divide a company name from its tagline in a <title>.
TITLE_SEPARATORS = ["|", "–", "—", " - ", "::", " : ", "»", "·", "•"]


def is_job_board(domain):
    """True if a URL's host is a listing site rather than a potential employer."""
    host = urlparse(domain).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in JOB_BOARD_DOMAINS:
        return True
    # Catch subdomains, e.g. gh.indeed.com
    return any(host.endswith("." + d) for d in JOB_BOARD_DOMAINS)


def name_matches_domain(name, domain):
    """
    True if a company name plausibly belongs to the domain it was found on.

    Exa often returns a directory listing, blog post, or careers aggregator that
    is *about* a company rather than owned by it - "Hubtel" found on
    founderstackafrica.com. Left alone, that row scrapes the wrong site's email
    and sends a letter addressed to Hubtel somewhere else entirely, which is far
    worse than a merely ugly company name.

    Acronym domains (ghipss.net for "Ghana Interbank Payment...") fail this test
    too, so a mismatch marks the row for review rather than dropping it.
    """
    host = urlparse(domain).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    # Second-level label: "careers.vodafone.com" -> "vodafone"
    parts = [p for p in host.split(".") if p not in ("com", "net", "org", "io", "co", "gh", "africa", "tech")]
    # Alphanumeric only, so "swift-infra" compares equal to "swiftinfra".
    label = "".join(c for c in "".join(parts) if c.isalnum())

    squashed = "".join(c for c in name.lower() if c.isalnum())
    if not label or not squashed:
        return False

    # Whole name inside the domain ("De-MannyTech Consult" / demannytechconsult),
    # or the domain inside the name ("Telecel Ghana" / telecel).
    if squashed in label or label in squashed:
        return True

    # Otherwise any distinctive word of the name appearing in the domain.
    stop = {"limited", "company", "ghana", "group", "technologies", "technology",
            "solutions", "services", "systems", "consult", "consulting", "africa"}
    words = [w for w in "".join(c if c.isalnum() else " " for c in name.lower()).split()
             if len(w) >= 4 and w not in stop]
    return any(w in label for w in words)


def clean_company_name(title):
    """
    Reduce a page <title> to a company name.

    Titles arrive as "Acme Ltd | Cloud Consulting in Accra" or bare taglines,
    and the result is interpolated straight into "Dear Recruiting Team at {X}"
    and onto the letterhead. Returns "" when the title yields nothing usable,
    which the caller treats as needs-human-review rather than guessing.
    """
    name = (title or "").strip()

    # Keep the segment before the first separator - conventionally the brand.
    for sep in TITLE_SEPARATORS:
        if sep in name:
            name = name.split(sep)[0].strip()

    name = " ".join(name.split())  # collapse newlines and runs of whitespace

    if not name or name.lower().strip(" .!") in GENERIC_TITLES:
        return ""

    # A full sentence is a tagline, not a name ("Through digital transformation,
    # we enable organisations to realise their creative potential.").
    if len(name) > 60 or name.count(" ") > 7:
        return ""

    # Titles that are entirely lower- or upper-case read as broken on a formal
    # letter; title-case them but leave deliberate mixed case (e.g. TiaCloud).
    if name.islower() or name.isupper():
        name = name.title()

    return name


def load_search_queries(path=SEARCH_QUERIES_FILE):
    """Your saved searches, else the shipped defaults (the owner's only: an
    invited user with no searches of their own searches for nothing)."""
    queries = _parse_search_queries(path) if os.path.exists(path) else []
    if queries or applicant.TENANT_MODE:
        return queries
    return _parse_search_queries(DEFAULT_SEARCH_QUERIES_FILE)


def queries_for_this_run(queries, now=None, per_run=QUERIES_PER_RUN):
    """
    The slice of searches this scrape runs. Slots advance every 6 hours (the
    scrape cadence), so consecutive runs take consecutive slices and the whole
    list is covered every ceil(n / per_run) runs - with the defaults, about
    every day and a quarter.
    """
    if len(queries) <= per_run:
        return list(queries)
    now = now or datetime.now(timezone.utc)
    slot = int(now.timestamp() // (6 * 3600))
    start = (slot * per_run) % len(queries)
    return [queries[(start + i) % len(queries)] for i in range(per_run)]


def _parse_search_queries(path):
    """
    One search per line, optionally "query | limit | track" (limit 1-25,
    default 10); blank lines and "#" comments ignored.
    """
    queries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # "query | limit | track"; limit and track are both optional.
            parts = [p.strip() for p in line.split("|")]
            query = parts[0]
            if not query:
                continue
            limit = parts[1] if len(parts) > 1 else ""
            track = parts[2].lower() if len(parts) > 2 and parts[2] else None
            try:
                n = int(limit) if limit else 10
            except ValueError:
                n = 10
            if track and track not in applicant.TRACKS:
                # Tagging these leads with the default pitch would send the
                # wrong CV, so skip the line loudly instead.
                print(f"[Pipeline] Skipping search '{query}': unknown track '{track}'. "
                      f"Use one of: {', '.join(applicant.TRACKS)}")
                continue
            queries.append((query, max(1, min(n, 25)), track))
    return queries


def run_search_all():
    # Each query carries its own track (None = DEFAULT_TRACK), so one scrape
    # can feed several roles.
    queries = load_search_queries()
    this_run = queries_for_this_run(queries)
    print(f"[Pipeline] Running {len(this_run)} of {len(queries)} searches this time (they rotate each run).")
    for query, limit, track in this_run:
        run_exa_search(query, limit, track=track)


def run_exa_search(query_str, limit=15, track=None):
    """
    Searches for new companies using Exa and adds them to queue.

    Results that are job boards are dropped. Results whose title yields no
    usable company name are still queued, but with status "Review" so they are
    inert until a human supplies the name - `send` only ever reads "Approved".
    """
    print(f"[Pipeline] Executing Exa search for query: '{query_str}'...")
    search_service = get_exa()
    raw_results = search_service.search_companies(query_str, num_results=limit)
    cleaned = search_service.get_company_domains(raw_results)

    added_count = 0
    skipped_boards = 0
    needs_review = 0
    for c in cleaned:
        if is_job_board(c["domain"]):
            print(f"[Pipeline] Skipping job board: {c['domain']}")
            skipped_boards += 1
            continue

        # Determine company type based on name or metadata text
        txt = c["text"].lower()
        name = clean_company_name(c["name"])
        review = not name
        if review:
            # Fall back to the bare host so the row is identifiable on review.
            host = urlparse(c["domain"]).netloc.lower()
            name = host[4:] if host.startswith("www.") else host
            print(f"[Pipeline] Unusable title for {c['domain']} - queued as '{name}' for review.")
            needs_review += 1
        elif not name_matches_domain(name, c["domain"]):
            # The page is probably about this company rather than theirs.
            review = True
            print(f"[Pipeline] '{name}' does not match {c['domain']} - queued for review.")
            needs_review += 1

        ctype = "general_startup"
        if "consulting" in txt or "managed service" in txt:
            ctype = "consultancy"
        elif "fintech" in txt or "payment" in txt or "transaction" in txt:
            ctype = "fintech"
        elif "telecom" in txt or "isp" in txt or "network" in txt:
            ctype = "isp"
        elif "bank" in txt or "fidelity" in txt or "ecobank" in txt:
            ctype = "bank"
            
        # The role comes from what the company's page says it does; the
        # search's own role (or the default) is only the fallback.
        # The role comes from what the company's page says it does. With no
        # clear winner, the search's role (or the default) is only a guess,
        # and the row waits for you to choose before it can be sent.
        role, role_reason, clear = role_match.pick_role(
            c["text"], applicant.TRACKS,
            fallback=(track or applicant.DEFAULT_TRACK).strip().lower(),
            fallback_source="this search's role" if track else "the default role",
            search_role=(track or "").strip().lower() or None,
        )
        print(f"[Pipeline] {name}: {'role ' + repr(role) if clear else 'role needs you'} ({role_reason}).")

        success = add_to_queue(
            company_name=name,
            website=c["domain"],
            email="",
            company_type=ctype,
            recipient="The Recruiting and Technology Team",
            address=applicant.DEFAULT_COMPANY_LOCATION,
            status="Review" if review else "Pending",
            track=role,
            role_reason=role_reason,
            role_review="" if clear else "yes",
        )
        if success:
            added_count += 1

    print(f"[Pipeline] Exa search complete. Added {added_count} new companies to queue.")
    if skipped_boards:
        print(f"[Pipeline] Dropped {skipped_boards} job-board result(s).")
    if needs_review:
        print(f"[Pipeline] {needs_review} row(s) need a company name set by hand (status 'Review').")

EMAIL_LOOKUPS_PER_RUN = 10


def run_apify_scrape():
    """
    Finds all companies in queue with empty website or empty email, 
    resolves websites (if empty) or crawls them via Apify.
    """
    queue = load_queue()
    pending = [r for r in queue if r["status"] == "Pending" and not r["email"]]
    
    if not pending:
        print("[Pipeline] No pending companies in queue that require email scraping.")
        return
        
    print(f"[Pipeline] Found {len(pending)} companies needing email scraping.")
    
    # Crawl a bounded batch per run (Apify bills per page crawled).
    batch = pending[:EMAIL_LOOKUPS_PER_RUN]
    urls_to_crawl = []
    
    for r in batch:
        web = r["website"]
        # If website URL is missing, we use Exa to find it!
        if not web:
            print(f"[Pipeline] Website URL missing for '{r['company_name']}'. Searching Exa...")
            search_service = get_exa()
            results = search_service.search_companies(f"{r['company_name']} Ghana official website homepage", num_results=1)
            if results:
                parsed = urlparse(results[0]["url"])
                web = f"{parsed.scheme}://{parsed.netloc}"
                r["website"] = web
                print(f"[Pipeline] Found website: {web}")
            else:
                print(f"[Pipeline] Could not find website for '{r['company_name']}'. Skipping scrape.")
                continue
        urls_to_crawl.append((r["company_name"], web))
        
    if not urls_to_crawl:
        save_queue(queue)
        print("[Pipeline] No valid URLs found to crawl in this batch.")
        return
        
    # Execute Apify scrape
    just_urls = [web for name, web in urls_to_crawl]
    scraper_service = get_apify()
    scraped_data = scraper_service.run_contact_scraper(just_urls, max_pages=8, depth=1)
    
    # Map scraped data back to queue. Only rows in this batch are eligible: an
    # earlier version matched the first row anywhere in the queue with the same
    # domain, so a crawl could write to an old row and leave this one empty
    # forever (re-crawled every run).
    def dom_of(url):
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d

    batch_rows = {dom_of(web): r for r in batch for name, web in urls_to_crawl if r["company_name"] == name}
    found = {}  # domain -> list of emails across all crawled pages
    for item in scraped_data:
        scraped_url = item.get("originalStartUrl") or item.get("url", "")
        scraped_domain = (item.get("domain") or "").lower() or dom_of(scraped_url)
        if scraped_domain.startswith("www."):
            scraped_domain = scraped_domain[4:]
        for row_domain in batch_rows:
            if (row_domain == scraped_domain or row_domain.endswith("." + scraped_domain)
                    or scraped_domain.endswith("." + row_domain)):
                found.setdefault(row_domain, []).extend(item.get("emails", []))
                break

    updated_count = 0
    for row_domain, r in batch_rows.items():
        email, note = choose_email(found.get(row_domain, []), r["website"])
        if email:
            r["email"] = email
            r["email_note"] = ""
            updated_count += 1
            print(f"[Pipeline] Found email '{email}' for '{r['company_name']}'")
        else:
            # No usable address: wait in Needs you rather than guess. The note
            # records any other-domain address so you can judge it yourself.
            r["email"] = "no_email_found"
            r["status"] = "Skipped"
            r["email_note"] = note
            print(f"[Pipeline] No email found for '{r['company_name']}'"
                  + (f" ({note})" if note else "") + ". Marking as Skipped.")

    save_queue(queue)
    print(f"[Pipeline] Scraping complete. Updated {updated_count} companies with email addresses.")

def needs_role_choice(r):
    """No role clearly fit when scraped, or its role has since been removed."""
    return (r.get("role_review", "").strip().lower() == "yes"
            or (r.get("track") or applicant.DEFAULT_TRACK) not in applicant.TRACKS)


# Written when the inbox couldn't be checked; follow-ups refuse to run while
# it exists, since they'd risk chasing someone whose reply wasn't seen.
INBOX_CHECK_FAILED_FILE = ".inbox_check_failed"


def run_check_inbox():
    """Mark replies and bounces from the sending inbox (see inbox_check.py)."""
    user, password = config.get("SENDER_EMAIL"), config.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        print("[Inbox] SENDER_EMAIL or GMAIL_APP_PASSWORD missing; skipping the inbox check.")
        open(INBOX_CHECK_FAILED_FILE, "w").write("missing credentials\n")
        return
    queue = load_queue()
    try:
        replied, bounced = inbox_check.check_inbox(queue, user, password)
    except Exception as e:  # login, network, IMAP errors: never fail the whole run
        print(f"[Inbox] Couldn't check the inbox ({type(e).__name__}: {e}). "
              f"New applications still go out; follow-ups are skipped this run.")
        open(INBOX_CHECK_FAILED_FILE, "w").write(f"{type(e).__name__}: {e}\n")
        return
    if os.path.exists(INBOX_CHECK_FAILED_FILE):
        os.remove(INBOX_CHECK_FAILED_FILE)
    if replied or bounced:
        save_queue(queue)
    for name in replied:
        print(f"[Inbox] Reply detected from {name}: marked replied, no follow-up will be sent.")
    for name in bounced:
        print(f"[Inbox] Bounce detected for {name}: marked bounced.")
    print(f"[Inbox] Checked the inbox: {len(replied)} new repl{'y' if len(replied) == 1 else 'ies'}, "
          f"{len(bounced)} new bounce{'' if len(bounced) == 1 else 's'}.")


def run_auto_approve():
    """
    Promotes every "Pending" row with a usable, scraped email straight to
    "Approved" - no human needs to click anything for a clean lead (good
    company-name match at search time, real email found by the scraper) to
    go out on the next send run. A "Review" row (bad company-name match) or
    a "Skipped" row (no email found) is left alone either way, since both
    need a human to supply the missing piece before there's anything to send.
    See run_notify_needs_review() for how those get surfaced without anyone
    having to go looking for them.

    The dashboard's "lead_reviewed" flag is no longer a gate here; it only
    marks that a human has looked at / edited a row before this step runs.
    """
    queue = load_queue()
    approved = 0
    for r in queue:
        if (r["status"] == "Pending" and r["email"] and r["email"] != "no_email_found"
                and not needs_role_choice(r)):
            r["status"] = "Approved"
            approved += 1
    save_queue(queue)
    print(f"[Pipeline] Auto-approved {approved} row(s) with a usable email.")


def run_notify_needs_review():
    """
    Emails the applicant a summary of rows that need a human: a "Review" row
    (the scraper couldn't trust the company name) or a "Skipped" row with no
    email found. Each row is only ever included once (tracked via
    "review_notified"), so a row that sits unresolved for days doesn't nag on
    every scrape run - fix it (or ignore it) in the dashboard or the CSV.
    """
    queue = load_queue()
    # "Skipped" (no email found) rows aren't included: they wait under
    # Companies → Skipped, since usually there's nothing to fix.
    needs_review = [
        r for r in queue
        if r.get("review_notified", "").strip().lower() != "yes"
        and (r["status"] == "Review" or (r["status"] in ("Pending", "Approved") and needs_role_choice(r)))
    ]
    # Companies that said "not now" whose check-back date has come: once each
    # (their own flag, since review_notified may be set from long ago).
    checkins = [r for r in queue if checkin_due(r) and r.get("checkback_notified", "").strip().lower() != "yes"]
    needs_review += checkins

    if not needs_review:
        print("[Pipeline] No new rows need review.")
        return

    # Ones flagged in an earlier email and still unresolved: counted, not
    # repeated, so the email stays about what's new.
    earlier = [
        r for r in queue
        if r.get("review_notified", "").strip().lower() == "yes"
        and (r["status"] == "Review" or (r["status"] in ("Pending", "Approved") and needs_role_choice(r)))
    ]
    count = len(needs_review)
    subject, text, html = review_email.build(
        needs_review, list(applicant.TRACKS),
        dashboard_url=config.get("DASHBOARD_URL") or "", earlier_waiting=len(earlier))

    msg = EmailMessage()
    msg["From"] = config.get("SENDER_EMAIL") or ""
    msg["To"] = applicant.EMAIL
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    try:
        smtp_client.send(msg, applicant.EMAIL)
    except GmailAuthError as e:
        # Rows stay un-notified, so the next scrape run tries again once it's fixed.
        print(f"[Pipeline] Couldn't send the review notification: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[Pipeline] Failed to send review notification: {e}")
        return

    for r in needs_review:
        if r in checkins:
            r["checkback_notified"] = "yes"
        else:
            r["review_notified"] = "yes"
    save_queue(queue)
    print(f"[Pipeline] Sent review notification for {count} compan{'y' if count == 1 else 'ies'}.")

def letter_filename(company_name):
    """Cover letter PDF filename for a company. Single definition, used everywhere."""
    safe_name = company_name.replace(" ", "_").replace("/", "_").replace(".", "")
    return f"{applicant.FILE_SLUG}_Cover_Letter_{safe_name}.pdf"

def letter_path(company_name):
    return os.path.join(LETTERS_DIR, letter_filename(company_name))

def run_generate_letters():
    """
    Generates tailored cover letter PDFs for companies in the queue
    that have emails populated and status is Pending or Approved.
    """
    if not os.path.exists(LETTERS_DIR):
        os.makedirs(LETTERS_DIR)

    queue = load_queue()
    target_rows = [r for r in queue if r["email"] and r["status"] in ["Pending", "Approved"]]

    if not target_rows:
        print("[Pipeline] No companies in queue with emails that need cover letters generated.")
        return

    print(f"[Pipeline] Generating cover letters for {len(target_rows)} companies...")
    for r in target_rows:
        generator.generate_pdf(
            company_name=r["company_name"],
            role_title=applicant.get_track(r["track"])["role_title"],
            company_type=r["company_type"],
            recipient_name=r["recipient_name"],
            company_address=r["company_address"],
            output_path=letter_path(r["company_name"]),
            track=r["track"]
        )
    print("[Pipeline] Cover letter generation complete.")

def run_send_emails():
    """
    Sends emails to companies marked "Approved" in the queue.
    Obeys a target limit (e.g. 15 per day) and standard delay.
    """
    queue = load_queue()
    approved = [r for r in queue if r["status"] == "Approved" and r["email"]]
    
    if not approved:
        print("[Pipeline] No emails marked 'Approved' in queue. Change 'Pending' to 'Approved' in outreach_queue.csv first.")
        return
        
    # A row whose track is switched off or has no CV is held (stays Approved)
    # rather than sent with another track's CV. Filtering before the daily cap means held rows
    # never eat into the day's sends.
    sendable, held = [], {}
    for r in approved:
        if r.get("role_review", "").strip().lower() == "yes":
            reason = "role not chosen yet (pick one in Needs you)"
        else:
            reason = applicant.track_hold_reason(r["track"])
        if reason:
            held.setdefault(reason, []).append(r["company_name"])
        else:
            sendable.append(r)
    for reason, names in held.items():
        print(f"[Pipeline] Holding {len(names)} row(s): {reason}. "
              f"They stay Approved and go out once that's fixed.")

    if not sendable:
        print("[Pipeline] Nothing sendable: every approved row is on a held track.")
        return

    # The cap is per day, not per run: a second "Send now" the same day only
    # sends what's left of today's allowance, so Gmail never sees a burst.
    already = sent_today(queue)
    allowance = max(0, DAILY_SEND_LIMIT - already)
    if not allowance:
        print(f"[Pipeline] Already sent {already} today (the daily cap is {DAILY_SEND_LIMIT}). "
              f"The rest go out on the next run tomorrow.")
        return
    to_send = sendable[:allowance]
    print(f"[Pipeline] Starting outreach run: sending {len(to_send)} of {len(approved)} approved emails...")

    # Check the login once before touching any row: a bad app password would
    # otherwise mark every company in the batch "Failed" for no fault of theirs.
    try:
        smtp, _ = smtp_client.connect()
        smtp.quit()
    except GmailAuthError as e:
        print(f"[Pipeline] Aborting before sending anything: {e}")
        print("[Pipeline] Nothing was marked Failed; these stay Approved and go out once the login works.")
        sys.exit(1)

    sender_service = get_sender()
    sent_count = 0
    failed_count = 0

    for i, r in enumerate(to_send):
        pdf_path = letter_path(r["company_name"])

        # Auto-generate letter if missing
        if not os.path.exists(pdf_path):
            print(f"[Pipeline] Cover letter PDF missing for {r['company_name']}. Generating on the fly...")
            if not os.path.exists(LETTERS_DIR):
                os.makedirs(LETTERS_DIR)
            generator.generate_pdf(
                company_name=r["company_name"],
                role_title=applicant.get_track(r["track"])["role_title"],
                company_type=r["company_type"],
                recipient_name=r["recipient_name"],
                company_address=r["company_address"],
                output_path=pdf_path,
                track=r["track"]
            )

        # Send. Returns the Message-ID on success so the follow-up can thread.
        try:
            message_id = sender_service.send_outreach_email(
                recipient_email=r["email"],
                company_name=r["company_name"],
                cover_letter_path=pdf_path,
                cv_path=applicant.cv_path_for(r["track"]),
                role_title=applicant.get_track(r["track"])["role_title"],
                subject=applicant.subject_for(r["track"]),
                body=applicant.build_outreach_body(r["company_name"], r["track"])
            )
        except GmailAuthError as e:
            # The login broke mid-run; this row and the rest stay Approved.
            print(f"[Pipeline] Stopping: {e}")
            print(f"[Pipeline] Sent {sent_count} before the login failed; the rest stay Approved.")
            sys.exit(1)

        if message_id:
            r["status"] = "Sent"
            r["date_sent"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            r["message_id"] = message_id
            sent_count += 1
            log_outreach(r, "SUCCESS")
        else:
            r["status"] = "Failed"
            failed_count += 1
            log_outreach(r, "FAILED")
            
        save_queue(queue)
        
        # Random delay between emails (30 to 60 seconds) to bypass spam filters
        if i < len(to_send) - 1:
            delay = random.randint(30, 60)
            print(f"[Pipeline] Waiting {delay} seconds before sending next email...")
            time.sleep(delay)
            
    print(f"[Pipeline] Run complete. Sent: {sent_count}, Failed: {failed_count}.")

def run_send_referrals():
    """
    Send queued referrals: the application a company asked for at another
    address ("please send your CV to recruitment@..."). Queued from the
    company's dashboard page. Not counted against the daily cap (you chose
    each one), and never followed up automatically: they told you where to
    apply, so chasing would be pushy.
    """
    queue = load_queue()
    due = [r for r in queue if r.get("referral_status") == "queued"]
    if not due:
        print("[Referral] Nothing queued.")
        return

    try:
        smtp, _ = smtp_client.connect()
        smtp.quit()
    except GmailAuthError as e:
        print(f"[Referral] Not sending: {e}. They stay queued.")
        sys.exit(1)

    sender_service = get_sender()
    for r in due:
        to = (r.get("referral_email") or "").strip()
        attach = r.get("referral_attach") or "both"
        want_cv, want_letter = attach in ("both", "cv"), attach in ("both", "letter")
        problem = None
        if "@" not in to:
            problem = "no valid address"
        elif attach not in applicant.REFERRAL_ATTACHMENTS:
            problem = f"unknown attachment choice '{attach}'"
        elif r["track"] not in applicant.TRACKS:
            problem = f"its role '{r['track']}' no longer exists"
        elif want_cv and not os.path.exists(applicant.cv_path_for(r["track"]) or ""):
            problem = f"no CV for role '{r['track']}'"
        if problem:
            r["referral_status"], r["referral_note"] = "failed", problem
            print(f"[Referral] Not sending to {r['company_name']}: {problem}.")
            save_queue(queue)
            continue

        pdf_path = os.path.join(LETTERS_DIR, "referral_" + letter_filename(r["company_name"]))
        if want_letter:
            os.makedirs(LETTERS_DIR, exist_ok=True)
            generator.generate_pdf(
                company_name=r["company_name"],
                role_title=applicant.get_track(r["track"])["role_title"],
                company_type=r["company_type"],
                recipient_name="The Recruitment Team",
                company_address=r["company_address"],
                output_path=pdf_path,
                track=r["track"],
            )
        try:
            message_id = sender_service.send_outreach_email(
                recipient_email=to,
                company_name=r["company_name"],
                cover_letter_path=pdf_path,
                cv_path=applicant.cv_path_for(r["track"]),
                role_title=applicant.get_track(r["track"])["role_title"],
                subject=applicant.referral_subject(r["track"]),
                body=applicant.build_referral_body(r["company_name"], r["track"], r.get("referral_by", ""), attach),
                attach_letter=want_letter, attach_cv=want_cv,
            )
        except GmailAuthError as e:
            print(f"[Referral] Stopping: {e}. The rest stay queued.")
            sys.exit(1)
        if message_id:
            r.update({"referral_status": "sent", "referral_message_id": message_id,
                      "referral_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "referral_note": ""})
            log_outreach(dict(r, email=to), "REFERRAL")
            print(f"[Referral] Sent {r['company_name']}'s application to {to} ({attach}).")
        else:
            r["referral_status"], r["referral_note"] = "failed", "Gmail didn't accept it; queue it again to retry"
            print(f"[Referral] Failed to send to {to}.")
        save_queue(queue)


def _month_of(date_str):
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%B %Y")
    except (ValueError, TypeError):
        return ""


def run_send_checkins():
    """
    Send check-ins you approved on the dashboard: companies that said "not
    now, maybe later" and whose check-back date came round. Only ever sent
    after you've read the draft and pressed Send; CV only (they've read your
    letter); not counted against the daily cap.
    """
    queue = load_queue()
    due = [r for r in queue if r.get("checkback_status") == "queued"]
    if not due:
        print("[Check-in] Nothing queued.")
        return
    try:
        smtp, _ = smtp_client.connect()
        smtp.quit()
    except GmailAuthError as e:
        print(f"[Check-in] Not sending: {e}. They stay queued.")
        sys.exit(1)

    sender_service = get_sender()
    for r in due:
        problem = None
        if "@" not in (r.get("email") or ""):
            problem = "no email address on this company"
        elif r["track"] not in applicant.TRACKS:
            problem = f"its role '{r['track']}' no longer exists"
        elif not os.path.exists(applicant.cv_path_for(r["track"]) or ""):
            problem = f"no CV for role '{r['track']}'"
        if problem:
            r["checkback_status"], r["checkback_note"] = "failed", problem
            print(f"[Check-in] Not sending to {r['company_name']}: {problem}.")
            save_queue(queue)
            continue
        try:
            message_id = sender_service.send_outreach_email(
                recipient_email=r["email"], company_name=r["company_name"],
                cover_letter_path="", cv_path=applicant.cv_path_for(r["track"]),
                subject=applicant.checkin_subject(r["track"]),
                body=applicant.build_checkin_body(r["company_name"], r["track"], _month_of(r.get("outcome_date", ""))),
                attach_letter=False, attach_cv=True,
            )
        except GmailAuthError as e:
            print(f"[Check-in] Stopping: {e}. The rest stay queued.")
            sys.exit(1)
        if message_id:
            r.update({"checkback_status": "sent", "checkback_message_id": message_id, "checkback_note": "",
                      "checkback_sent_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            log_outreach(r, "CHECK-IN")
            print(f"[Check-in] Sent to {r['company_name']} ({r['email']}).")
        else:
            r["checkback_status"], r["checkback_note"] = "failed", "Gmail didn't accept it; send it again to retry"
        save_queue(queue)


def checkin_due(r, today=None):
    """Said "not now", the check-back date has come, and nothing's been done yet."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    return (r.get("outcome") == "later" and not r.get("checkback_status")
            and bool(r.get("checkback_date")) and r["checkback_date"] <= today)


def sent_today(queue, field="date_sent"):
    """How many rows were sent today (by `field`, which holds "%Y-%m-%d %H:%M:%S" in runner time)."""
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(1 for r in queue if (r.get(field) or "").startswith(today))


def log_outreach(row, status):
    """
    Logs successful or failed emails to outreach_log.csv
    """
    fields = ["date", "company_name", "email", "status", "cover_letter"]
    write_header = not os.path.exists(LOG_FILE)
    
    with open(LOG_FILE, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            writer.writeheader()

        writer.writerow({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "company_name": row["company_name"],
            "email": row["email"],
            "status": status,
            "cover_letter": letter_filename(row["company_name"])
        })

def show_status():
    """
    Prints stats of the queue
    """
    queue = load_queue()
    stats = {"Pending": 0, "Approved": 0, "Sent": 0, "Followup-Sent": 0, "Failed": 0, "Skipped": 0, "Review": 0}
    no_email = 0

    for r in queue:
        stats[r["status"]] = stats.get(r["status"], 0) + 1
        if not r["email"]:
            no_email += 1

    print("\n--- Outreach Queue Status ---")
    print(f"Total Companies in Queue: {len(queue)}")
    print(f"  Pending:       {stats['Pending']} (of which {no_email} have no email address)")
    print(f"  Approved:      {stats['Approved']} (ready to send)")
    print(f"  Sent:          {stats['Sent']}")
    print(f"  Followup-Sent: {stats['Followup-Sent']}")
    print(f"  Failed:        {stats['Failed']}")
    print(f"  Skipped:       {stats['Skipped']}")
    print(f"  Review:        {stats['Review']} (name needs setting by hand)")
    print("-----------------------------\n")

def print_help():
    print(f"""
Usage: py outreach_pipeline.py [command]

Commands:
  search    - Search Exa for new tech companies (args: query [limit])
              e.g., py outreach_pipeline.py search "fintech companies in Accra Ghana" 10
  search-all - Run every search in search_queries.txt (falls back to built-in defaults).
              Lines are 'query | limit | track'; track picks the pitch and CV for leads it finds.
  scrape    - Crawl websites of Pending companies using Apify to harvest contact emails
  auto-approve   - Promote every Pending row with a usable email to Approved (no human review;
              for the scheduled CI pipeline only, not recommended for interactive use)
  send-checkins  - Send check-ins you approved for companies that said "not now"
  send-referrals - Send applications queued from a company's "They referred me" panel
  check-inbox    - Read the sending Gmail (read-only) and mark replies and bounces automatically
  notify-review  - Email a summary of Review/no-email-found rows that need a human, each only once
  generate  - Compile ReportLab cover letter PDFs for companies with emails in the queue
  send      - Send up to {DAILY_SEND_LIMIT} emails marked 'Approved' via SMTP with CV & Letter attachments
  status    - Show queue statistics and counts
  import    - Import a curated target list from a markdown file (arg: path/to/list.md)
""")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print_help()
        sys.exit(0)
        
    cmd = sys.argv[1].lower()
    
    if cmd == "import":
        if len(sys.argv) < 3:
            print("Usage: py outreach_pipeline.py import path/to/target_list.md")
            sys.exit(1)
        import_from_markdown(sys.argv[2])
    elif cmd == "search":
        # Optional trailing "--track <name>" tags every row this run adds.
        argv = sys.argv[:]
        track = None
        if "--track" in argv:
            i = argv.index("--track")
            if i + 1 < len(argv):
                track = argv[i + 1]
                del argv[i:i + 2]
            else:
                print(f"Usage: --track requires a value ({' | '.join(applicant.TRACKS)})")
                sys.exit(1)
        if track and track.strip().lower() not in applicant.TRACKS:
            print(f"Unknown track '{track}'. Choose from: {', '.join(applicant.TRACKS)}")
            sys.exit(1)

        q = "tech startup software development company Accra Ghana"
        lim = 15
        if len(argv) > 2:
            q = argv[2]
        if len(argv) > 3:
            lim = int(argv[3])
        run_exa_search(q, lim, track=track)
    elif cmd == "search-all":
        run_search_all()
    elif cmd == "scrape":
        run_apify_scrape()
    elif cmd == "auto-approve":
        run_auto_approve()
    elif cmd == "check-inbox":
        run_check_inbox()
    elif cmd == "send-referrals":
        run_send_referrals()
    elif cmd == "send-checkins":
        run_send_checkins()
    elif cmd == "notify-review":
        run_notify_needs_review()
    elif cmd == "generate":
        run_generate_letters()
    elif cmd == "send":
        run_send_emails()
    elif cmd == "status":
        show_status()
    else:
        print(f"Unknown command: {cmd}")
        print_help()
