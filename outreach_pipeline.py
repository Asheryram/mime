import os
import csv
import sys
import time
import random
from datetime import datetime
from urllib.parse import urlparse
from config import config
from exa_search import ExaSearch
from apify_scraper import ApifyScraper
from cover_letter_generator import CoverLetterGenerator
from email_sender import EmailSender
import applicant

QUEUE_FILE = "outreach_queue.csv"
LOG_FILE = "outreach_log.csv"
LETTERS_DIR = "generated_letters"
CV_PATH = applicant.CV_PATH

# Canonical column order for outreach_queue.csv. followup_pipeline imports this
# so both writers agree on the schema.
QUEUE_FIELDS = [
    "company_name", "website", "email", "company_type",
    "recipient_name", "company_address", "status",
    "date_added", "date_sent", "date_followup", "message_id",
    "track"
]

DAILY_SEND_LIMIT = 15

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

def add_to_queue(company_name, website, email="", company_type="general_startup", recipient="The Recruiting and Technology Team", address="Accra, Ghana", status="Pending", track=None):
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
        "company_address": address,
        "status": status,
        "date_added": datetime.now().strftime("%Y-%m-%d"),
        "date_sent": "",
        "date_followup": "",
        "message_id": "",
        "track": (track or applicant.DEFAULT_TRACK).strip().lower()
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
                    address="Accra, Ghana"
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
}

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
            
        success = add_to_queue(
            company_name=name,
            website=c["domain"],
            email="",
            company_type=ctype,
            recipient="The Recruiting and Technology Team",
            address="Accra, Ghana",
            status="Review" if review else "Pending",
            track=track
        )
        if success:
            added_count += 1

    print(f"[Pipeline] Exa search complete. Added {added_count} new companies to queue.")
    if skipped_boards:
        print(f"[Pipeline] Dropped {skipped_boards} job-board result(s).")
    if needs_review:
        print(f"[Pipeline] {needs_review} row(s) need a company name set by hand (status 'Review').")

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
    
    # We scrape in batches to avoid overwhelming the scraper
    # Let's take up to 5 URLs to crawl at a time
    batch = pending[:5]
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
    
    # Map scraped data back to queue
    updated_count = 0
    processed_domains = set()
    for name, web in urls_to_crawl:
        parsed = urlparse(web)
        dom = parsed.netloc.lower()
        if dom.startswith("www."):
            dom = dom[4:]
        processed_domains.add(dom)

    successful_domains = set()
    for item in scraped_data:
        scraped_url = item.get("originalStartUrl") or item.get("url", "")
        domain_field = item.get("domain", "")
        
        scraped_domain = domain_field.lower() if domain_field else ""
        if not scraped_domain and scraped_url:
            scraped_domain = urlparse(scraped_url).netloc.lower()
            
        if scraped_domain.startswith("www."):
            scraped_domain = scraped_domain[4:]
            
        if not scraped_domain:
            continue
        
        # Find emails
        emails_list = item.get("emails", [])
        # filter out invalid emails (like PNG, JPG, or placeholder strings)
        valid_emails = [e for e in emails_list if "@" in e and "." in e.split("@")[1]]
        
        if not valid_emails:
            continue
            
        # Get primary email (prefer recruiter, contact, HR, jobs; else first)
        primary_email = ""
        for email in valid_emails:
            el = email.lower()
            if any(k in el for k in ["hr", "career", "job", "recruitment", "hire", "people", "contact"]):
                primary_email = email
                break
        if not primary_email:
            primary_email = valid_emails[0]
            
        # Update queue row
        for r in queue:
            if r["website"]:
                parsed_row = urlparse(r["website"])
                row_domain = parsed_row.netloc.lower()
                if row_domain.startswith("www."):
                    row_domain = row_domain[4:]
                if row_domain == scraped_domain or row_domain.endswith("." + scraped_domain) or scraped_domain.endswith("." + row_domain):
                    r["email"] = primary_email
                    print(f"[Pipeline] Found email '{primary_email}' for '{r['company_name']}'")
                    updated_count += 1
                    successful_domains.add(row_domain)
                    break

    # Mark domains that were crawled but failed to find emails
    for dom in processed_domains:
        if dom not in successful_domains:
            for r in queue:
                if r["website"]:
                    parsed_row = urlparse(r["website"])
                    row_domain = parsed_row.netloc.lower()
                    if row_domain.startswith("www."):
                        row_domain = row_domain[4:]
                    if row_domain == dom:
                        r["email"] = "no_email_found"
                        r["status"] = "Skipped"
                        print(f"[Pipeline] No email found for '{r['company_name']}'. Marking as Skipped.")
                        break
                    
    save_queue(queue)
    print(f"[Pipeline] Scraping complete. Updated {updated_count} companies with email addresses.")

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
        
    to_send = approved[:DAILY_SEND_LIMIT]
    print(f"[Pipeline] Starting outreach run: sending {len(to_send)} of {len(approved)} approved emails...")

    if not os.path.exists(CV_PATH):
        print(f"[Pipeline] Aborting: CV not found at '{CV_PATH}'. Set CV_PATH in applicant.py.")
        return

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
        message_id = sender_service.send_outreach_email(
            recipient_email=r["email"],
            company_name=r["company_name"],
            cover_letter_path=pdf_path,
            cv_path=CV_PATH,
            role_title=applicant.get_track(r["track"])["role_title"],
            subject=applicant.subject_for(r["track"]),
            body=applicant.build_outreach_body(r["company_name"], r["track"])
        )

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
  scrape    - Crawl websites of Pending companies using Apify to harvest contact emails
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
                print("Usage: --track requires a value (devops | software | sysadmin)")
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
    elif cmd == "scrape":
        run_apify_scrape()
    elif cmd == "generate":
        run_generate_letters()
    elif cmd == "send":
        run_send_emails()
    elif cmd == "status":
        show_status()
    else:
        print(f"Unknown command: {cmd}")
        print_help()
