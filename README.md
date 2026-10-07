# 🚀 Automated Cold Job Outreach & Follow-up Pipeline

An end-to-end automated cold job application and follow-up engine for running high-volume, personalized outreach campaigns. It combines semantic web search, contact harvesting, dynamic PDF cover letter generation, and threaded email follow-ups to turn cold job hunting into a repeatable pipeline.

Nothing about the pipeline is specific to software or DevOps roles: the search queries, the pitch, and the letter content are all data, not code. A nursing graduate, an accountant, or a mechanical engineer can run the exact same pipeline for their own field. See [Replicating this for your own field](#-replicating-this-for-your-own-field) below.

All personal details live in a single file (`applicant.py`), so **any human user or AI coding agent** can clone the repository, edit one file, add a CV, and run their own campaign.

---

## 🛠️ Architecture Overview

Three phases, each runnable independently:

```
[Phase 1: DISCOVER] ──> [Phase 2: ENRICH] ──> [Phase 3: SEND & FOLLOW-UP]
  - Exa.ai semantic       - Apify contact-        - ReportLab PDF letter compiler
    web search              info scraper          - Gmail SMTP over SSL
  - Markdown list         - Email prioritisation  - Threaded follow-up (In-Reply-To)
    imports                 (hr@, careers@, ...)
```

*   **Phase 1 &mdash; Discover:** Find companies with semantic search queries (location, industry, tech stack), or import a hand-curated markdown list.
*   **Phase 2 &mdash; Enrich:** Resolve missing company websites via Exa, then crawl each domain with Apify's contact-info scraper. Recruiting-flavoured addresses (`hr@`, `careers@`, `jobs@`) are preferred over generic ones; domains with no email found are marked `Skipped`.
*   **Phase 3 &mdash; Send & Follow-up:** Compile a cover letter PDF tailored to the company's sector, attach it alongside your CV, send via Gmail SMTP with randomised anti-spam delays, and 6 days later send a check-in **in the original email thread**.

---

## 🎓 Replicating This for Your Own Field

Everything devops-specific in this repo lives in two files. Replace both and the rest of the pipeline (search, scraping, sending, follow-ups, CI, dashboard) works unchanged for any field.

### 1. `applicant.py`: your identity and pitch

Edit the top-level fields (name, headline, location, LinkedIn/GitHub, CV filename) and the `TRACKS` dictionary. Each track is one pitch for one kind of role:

```python
TRACKS = {
    "nursing": {
        "role_title": "Registered Nurse",
        "subject_area": "Nursing",
        "interest_area": "clinical nursing",
        "closing_skills": "patient care experience, clinical judgment, and adaptability",
        "credentials": "a nursing graduate of <your school> with <N> years of clinical rotation experience",
        "highlights": [
            "Four bullets here, each a real, specific, checkable claim your CV backs up.",
            "...",
            "...",
            "...",
        ],
    },
}
DEFAULT_TRACK = "nursing"
```

You can rename the keys (`devops`/`software`/`sysadmin`) to whatever fields you're applying across, add as many as you want, or keep just one. **Every highlight must be something your CV can back up.** The cover letter and CV are read side by side, and an unbacked claim is worse than no claim.

### 2. `cover_letter_generator.py`: the cover letter body text

This is the one place with field-specific prose, and the one place you'll actually edit code rather than data:

*   **If you're adding a track beyond the existing three:** write a new method styled like `get_software_paragraphs()` or `get_sysadmin_paragraphs()` (one intro paragraph, a lead-in line, 2 to 3 bullet highlights, a closing paragraph), then add one `elif (track or "").strip().lower() == "your_track_name":` branch in `generate_pdf()` right next to the existing two, calling your new method.
*   **If you're reusing the `devops` track's slot** (the one with company-type-specific variants: fintech, consultancy, bank, isp, startup), rewrite the bullet content inside `get_template_paragraphs()` for each `company_type` branch. The structure (one intro, one transition line, three bullets, one closing) is a reasonable template for any field; only the sentences themselves are devops-specific.

Either way, every paragraph is plain HTML-ish markup ReportLab parses (`<b>`, `&bull;`, `&#160;` for non-breaking spaces). Copy the formatting, replace the words.

### 3. Everything else is unaffected

`outreach_pipeline.py`'s search queries (`search "fintech startups in Lagos" 10`) are plain strings you choose at the command line or in `.github/workflows/scrape-pipeline.yml`. Search for whatever companies or employers are relevant to your field instead. `company_type` categories (`fintech`, `consultancy`, `bank`, `isp`, `general_startup`) are also just labels; relabel them for your own sectors if the defaults don't fit, updating the branches in `get_template_paragraphs()` to match.

---

## 📂 Repository Structure

| File | Purpose |
| :--- | :--- |
| **`applicant.py`** | 🔴 **Edit this first.** Your name, contact details, CV filename, and pitch. Every other file reads from here. |
| **`outreach_pipeline.py`** | Main CLI orchestrator: search, scrape, generate, send, status, import. |
| **`followup_pipeline.py`** | Follow-up manager: date eligibility, exclusions, threaded check-ins. |
| **`email_sender.py`** | Gmail SMTP client with PDF attachments; returns the `Message-ID` for threading. |
| **`cover_letter_generator.py`** | ReportLab PDF compiler with 5 sector-specific letter variants. |
| **`exa_search.py`** | Exa API wrapper for semantic company search. |
| **`apify_scraper.py`** | Apify actor interface for crawling domain contact pages. |
| **`config.py`** | Zero-dependency `.env` loader. |
| **`outreach_queue.csv`** | Central tracking file for target companies and their status. |
| **`outreach_log.csv`** | Append-only history of send attempts and outcomes. |
| **`.github/workflows/scrape-pipeline.yml`** | Unattended lead discovery every 6 hours: search + scrape (see [Scheduled CI](#-scheduled-ci-fully-unattended) below). |
| **`.github/workflows/send-pipeline.yml`** | Unattended sending once a day: auto-approve + generate + send + follow-up (see [Scheduled CI](#-scheduled-ci-fully-unattended) below). |
| **`webapp/`** | Optional hosted dashboard for the manual-review parts (see [Triage Dashboard](#-triage-dashboard-optional-web-app) below). |

### Queue status lifecycle

```
Pending ──(you approve, or the dashboard/CI auto-approve)──> Approved ──(send)──> Sent ──(6 days)──> Followup-Sent
   │                                                                                  └──(SMTP error)──> Failed
   ├──(no email found)──> Skipped
   └──(unusable scraped name)──> Review ──(you fix the name)──> back to Pending
```

A row can also be marked `Rejected` from the dashboard, which drops it for good. Three extra columns track manual review state without changing `status`:

*   **`lead_reviewed`**: set to `yes` from the dashboard to mark that a human has looked at or edited a scraped lead. It doesn't gate anything; `auto-approve` promotes any `Pending` row with a usable email regardless, since a clean scrape (good name match, real email) doesn't need a human to confirm it. `review_notified`: set once an email has gone out flagging a `Review` or missing-email row, so the same unresolved row doesn't nag you on every scrape run.
*   **`replied`** / **`bounced`**: set to `yes` to stop future follow-ups (`replied`) or future sends (`bounced`) to that row, without editing the hardcoded exclusion lists in `followup_pipeline.py`.

`Pending → Approved` is **manual and deliberate** by default. Nothing is ever emailed until you flip that column yourself, unless you've opted into the fully unattended CI pipeline below.

---

## 🚀 Setup Instructions

### 1. Install dependencies
Python 3.10+ required.
```bash
pip install -r requirements.txt
```

### 2. Configure credentials
```bash
cp .env.example .env
```
Fill in `.env`:
*   **`EXA_API_KEY`** &mdash; from [Exa.ai](https://exa.ai) (1000 free searches/month).
*   **`APIFY_API_TOKEN`** &mdash; from [Apify](https://apify.com). The contact scraper consumes paid compute units, so watch your free-tier balance.
*   **`SENDER_EMAIL`** &mdash; your Gmail address.
*   **`GMAIL_APP_PASSWORD`** &mdash; a 16-character [Google App Password](https://myaccount.google.com/apppasswords), *not* your login password. Requires 2-Step Verification on the account. Paste it with or without the spaces Google displays; both are accepted.
*   **`APPLICANT_PHONE`** &mdash; the number printed on the letterhead and email signature.
*   **`APPLICANT_EMAIL`** &mdash; the contact address shown to recruiters. Defaults to `SENDER_EMAIL` if unset.

Phone and contact email live in `.env` rather than `applicant.py` specifically so that `applicant.py` can be committed to a public repository without publishing a personal number and inbox for scrapers to harvest.

### 3. Personalise `applicant.py` and `cover_letter_generator.py`
Set your name, headline, location, LinkedIn/GitHub, target role(s), and CV filename. The default `TRACKS` are written for a DevOps/Cloud Engineering job search; see [Replicating This for Your Own Field](#-replicating-this-for-your-own-field) above for exactly what to edit if you're applying in a different field.

### 4. Add your CV
Save your master resume as a PDF in the root directory and point `CV_PATH` in `applicant.py` at it. `send` aborts up front if the file is missing.

### 5. Create the target queue
```bash
cp outreach_queue.example.csv outreach_queue.csv
```
Delete the `Example Tech` row before your first real run.

---

## 💻 CLI Commands

| Command | Usage | Description |
| :--- | :--- | :--- |
| **Search** | `py outreach_pipeline.py search "[query]" [limit]` | Find new target companies, e.g. `search "fintech startups in Lagos" 10`. |
| **Scrape** | `py outreach_pipeline.py scrape` | Resolves missing websites and crawls up to 5 domains per run for contact emails. |
| **Auto-approve** | `py outreach_pipeline.py auto-approve` | Promotes every `Pending` row with a usable, scraped email to `Approved`, no review required. For the scheduled CI pipeline below; not recommended for interactive use. |
| **Notify-review** | `py outreach_pipeline.py notify-review` | Emails you a summary of `Review` and missing-email rows that need a human, each only once. |
| **Generate** | `py outreach_pipeline.py generate` | Compiles cover letter PDFs into `generated_letters/` for review. |
| **Send** | `py outreach_pipeline.py send` | Emails up to 15 companies marked `Approved` and marks them `Sent`. |
| **Status** | `py outreach_pipeline.py status` | Queue counts by status. |
| **Import** | `py outreach_pipeline.py import list.md` | Imports a curated markdown list (`## Section` sets sector, `### 1. Company` adds a row). |

Only `send` transmits anything. `search`, `scrape`, `generate`, and `status` are all safe to run freely. `auto-approve` removes the manual gate, so treat it the same as `send`.

### 🔁 Running follow-ups

*   **Dry-run (default)** &mdash; shows who is eligible and whether each will thread correctly:
    ```bash
    py followup_pipeline.py
    ```
*   **Live send (up to 10):**
    ```bash
    py followup_pipeline.py run
    ```
    Sets status to `Followup-Sent` and stamps `date_followup`.

Follow-ups attach `In-Reply-To` and `References` headers built from the `message_id` recorded at send time, so the check-in lands inside the original Gmail conversation. Rows sent before that column existed still send, but start a new thread &mdash; the dry-run tells you which.

Two exclusion lists at the top of `followup_pipeline.py` are worth maintaining as your campaign runs:
*   `BOUNCED_DOMAINS` &mdash; add domains that hard-bounce, so you stop spending sender reputation on them.
*   `REPLIED_COMPANIES` &mdash; add companies that reply, so the automation never chases a live conversation.

---

## ⏰ Scheduled CI (fully unattended)

Two workflows run on independent schedules instead of one combined run, so discovery doesn't wait on the send cadence and vice versa:

*   **`.github/workflows/scrape-pipeline.yml`**, every 6 hours (00:00, 06:00, 12:00, 18:00 UTC): `search` against the queries written into the workflow, `scrape` to resolve contact emails for whatever's missing one, then `notify-review` to email you about any `Review` or missing-email row you haven't been told about yet.
*   **`.github/workflows/send-pipeline.yml`**, once a day at 09:00 UTC (9am in Accra, GMT with no DST): `auto-approve`, `generate`, `send`, then `followup_pipeline.py run`.

A clean lead (the scraper trusted the company name and found a real email) needs no human at all: `auto-approve` promotes it straight from `Pending` to `Approved` and the next `send` run emails it. The only rows that wait on a person are the ones the scraper itself couldn't resolve: an unclear company name (`Review`) or no email found (`Skipped`). Those are what the `notify-review` email and the [Triage Dashboard](#-triage-dashboard-optional-web-app) below are for. Run without the dashboard and those two cases simply sit until you fix them by hand in the CSV.

Because this repo is public, the queue, log, and CV cannot live here (see `.gitignore`). They're mirrored instead in a private companion repo (the author's own is `Asheryram/mime-state`), which both workflows pull at the start of their run and push back to at the end via the same SSH deploy key.

**If you forked this repo**, you need your own companion repo (see [Setting up your own companion repo](#setting-up-your-own-companion-repo) below) and these repo settings (Settings &rarr; Secrets and variables &rarr; Actions):

| Name | Kind | Used by | Value |
| :--- | :--- | :--- | :--- |
| `STATE_REPO` | Variable | both | `your-github-username/your-state-repo` |
| `STATE_DEPLOY_KEY` | Secret | both | Private half of an SSH deploy key with write access on your companion repo |
| `EXA_API_KEY`, `APIFY_API_TOKEN` | Secrets | scrape-pipeline | Same values as `.env` |
| `SENDER_EMAIL`, `GMAIL_APP_PASSWORD`, `APPLICANT_EMAIL` | Secrets | both (scrape-pipeline sends the review-needed notification to `APPLICANT_EMAIL`) | Same values as `.env` |
| `APPLICANT_PHONE` | Secret | send-pipeline | Same value as `.env` |

Trigger either workflow manually from the Actions tab (`workflow_dispatch`) to test before waiting for its schedule. Want a different cadence? Both are plain cron (`cron: "0 */6 * * *"` and `cron: "0 9 * * *"`) in each workflow's `on.schedule`.

### Setting up your own companion repo

1. Create a new **private** GitHub repo (any name).
2. Seed it with a header-only `outreach_queue.csv` (copy the header row from `outreach_queue.example.csv`), an empty `outreach_log.csv`, and your CV PDF under the same filename as `CV_PATH` in `applicant.py`.
3. Generate an SSH key pair (`ssh-keygen -t ed25519 -f state_deploy_key -N ""`), add the public half as a **Deploy key with write access** on that repo (Settings &rarr; Deploy keys), and add the private half as the `STATE_DEPLOY_KEY` secret on *this* repo.
4. Set the `STATE_REPO` repo variable to `your-username/your-repo-name`.

---

## 🖥️ Triage Dashboard (optional web app)

`webapp/` is a small hosted Flask app for the parts of the pipeline that genuinely need a human: fixing a scraped company name, supplying an email the scraper missed, reviewing a freshly scraped lead before it's approved, flagging a sent application as replied or bounced, and swapping in a new CV. It's a thin client over the same companion repo the scheduled CI uses, not a second source of truth: the dashboard and the CI both read and write the exact same `outreach_queue.csv`.

Pages: an **Overview** home with stat tiles and quick links, then one page each for **New leads**, **Name fixes**, **Missing emails**, **Sent (replies/bounces)**, **All companies**, and **Your CV**. Login is restricted to a single email you set, with first-run password setup and an email-based reset flow (via your existing Gmail credentials, no new email service needed).

It's entirely optional: everything it does can also be done by editing `outreach_queue.csv` directly in your companion repo, and the CI pipeline runs fine without it (new leads just wait in `Pending` for review). Full setup (a Turso database, a GitHub token, and deploying to Vercel) is documented in **[`webapp/README.md`](webapp/README.md)**.

---

## 🤖 AI Agent Delegation

This repository works well under an autonomous coding agent (Claude Code, Cursor, Gemini CLI):

*   **Find leads:** *"Search for 15 remote-friendly cloud companies hiring in Africa, scrape their contact details, and save them to the queue."*
*   **Verify & approve:** *"Open outreach_queue.csv, sanity-check the scraped emails, remove duplicates, and mark the strong targets as Approved."*
*   **Execute sends:** *"Generate the cover letters, confirm the CV path resolves, then run the daily send batch."*
*   **Run check-ins:** *"Dry-run the follow-up pipeline, show me who is eligible, then send if it looks right."*
*   **Set up automation:** *"Wire this repo up to run daily via GitHub Actions, with a hosted dashboard so I can review new leads from my phone."*

---

## ⚠️ Safeguards & Rate Limits
*   **Approval gate:** only rows marked `Approved` are ever emailed, whether that happened by your own hand, the dashboard, or `auto-approve` on a clean scrape.
*   **Scraper-confidence gate:** a lead only waits for a human if the scraper itself couldn't resolve it (an unclear company name, or no email found). A clean scrape is sent automatically; you're emailed about the rest so nothing silently stalls.
*   **Spam controls:** a random 30 to 60 second delay between every send.
*   **Daily caps:** 15 cold emails/day (`DAILY_SEND_LIMIT`), 10 follow-ups/day (`FOLLOWUP_DAILY_LIMIT`).
*   **Review queue:** inspect every draft in `generated_letters/` before sending.
*   **Crash safety:** the queue is written back to disk after each individual send, so an interruption never re-sends an email.
*   **Secrets:** `.gitignore` denies everything by default and whitelists only source files; your `.env`, CV, queue, and generated letters are never committed. Don't loosen it. The dashboard's own secrets (Turso, GitHub token, Flask session key) live only in Vercel's environment variables, never in the repo.
