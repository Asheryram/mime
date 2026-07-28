# 🚀 Automated Cold Job Outreach & Follow-up Pipeline

An end-to-end automated cold job application and follow-up engine for running high-volume, personalized outreach campaigns. It combines semantic web search, contact harvesting, dynamic PDF cover letter generation, and threaded email follow-ups to turn cold job hunting into a repeatable pipeline.

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

### Queue status lifecycle

```
Pending ──(you approve)──> Approved ──(send)──> Sent ──(6 days)──> Followup-Sent
   └──(no email found)──> Skipped        └──(SMTP error)──> Failed
```

`Pending → Approved` is **manual and deliberate**. Nothing is ever emailed until you flip that column yourself.

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

### 3. Personalise `applicant.py`
Set your name, headline, location, LinkedIn/GitHub, target role, and the four achievement bullets used in the cold email. Then edit the five sector variants in `cover_letter_generator.py` (`get_template_paragraphs`) so each letter cites your own work.

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
| **Generate** | `py outreach_pipeline.py generate` | Compiles cover letter PDFs into `generated_letters/` for review. |
| **Send** | `py outreach_pipeline.py send` | Emails up to 15 companies marked `Approved` and marks them `Sent`. |
| **Status** | `py outreach_pipeline.py status` | Queue counts by status. |
| **Import** | `py outreach_pipeline.py import list.md` | Imports a curated markdown list (`## Section` sets sector, `### 1. Company` adds a row). |

Only `send` transmits anything. `search`, `scrape`, `generate`, and `status` are all safe to run freely.

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

## 🤖 AI Agent Delegation

This repository works well under an autonomous coding agent (Claude Code, Cursor, Gemini CLI):

*   **Find leads:** *"Search for 15 remote-friendly cloud companies hiring in Africa, scrape their contact details, and save them to the queue."*
*   **Verify & approve:** *"Open outreach_queue.csv, sanity-check the scraped emails, remove duplicates, and mark the strong targets as Approved."*
*   **Execute sends:** *"Generate the cover letters, confirm the CV path resolves, then run the daily send batch."*
*   **Run check-ins:** *"Dry-run the follow-up pipeline, show me who is eligible, then send if it looks right."*

---

## ⚠️ Safeguards & Rate Limits
*   **Manual approval gate:** only rows you set to `Approved` are ever emailed.
*   **Spam controls:** a random 30&ndash;60 second delay between every send.
*   **Daily caps:** 15 cold emails/day (`DAILY_SEND_LIMIT`), 10 follow-ups/day (`FOLLOWUP_DAILY_LIMIT`).
*   **Review queue:** inspect every draft in `generated_letters/` before sending.
*   **Crash safety:** the queue is written back to disk after each individual send, so an interruption never re-sends an email.
*   **Secrets:** `.gitignore` denies everything by default and whitelists only source files &mdash; your `.env`, CV, queue, and generated letters are never committed. Don't loosen it.
