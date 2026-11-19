# 🚀 Automated Cold Job Outreach & Follow-up Pipeline

An end-to-end automated cold job application and follow-up engine for running high-volume, personalized outreach campaigns. It combines semantic web search, contact harvesting, dynamic PDF cover letter generation, and threaded email follow-ups to turn cold job hunting into a repeatable pipeline.

Nothing about the pipeline is specific to software or DevOps roles: the search queries, the pitch, and the letter content are all data, not code. A nursing graduate, an accountant, or a mechanical engineer can run the exact same pipeline for their own field. See [Roles](#-roles-applying-for-several-kinds-of-job-at-once) below.

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

## 🎓 Roles: Applying for Several Kinds of Job at Once

A **role** (called a *track* in the code) is one complete pitch: its own CV, letterhead title, cold email, cover letter and follow-up. Each company in the queue is tagged with one role, and gets that role's CV and letter, so one campaign can approach a fintech about DevOps work and a SaaS startup about full-stack work without either letter, or CV, reading generic.

The built-in roles are `devops` (cloud, DevOps, and the systems/network administration side) and `software` (full-stack). Nothing about the pipeline is specific to software jobs: a nurse or accountant replaces these with their own.

### Managing roles

**On the dashboard (recommended):** the **Roles** page lists each role with its CV and status. *Add a role* opens a form for the pitch (role title, letterhead title, email highlights, cover letter intro / bullets / closing) and a CV upload, with a preview of the exact email a company would receive.

**By hand:** roles live in `tracks.json` in your companion repo, which the dashboard edits and CI copies in before each run. Until it exists, the pipeline uses the shipped defaults in `webapp/tracks_default.json`. The field reference is at the top of the tracks section in `applicant.py`. In letter text, `{company}` and `{role}` are filled in per company.

**Removing a role:** on its page, *Remove this role* moves every company tagged with it (and any search tagged `| role`) to the role you pick, and can add its keywords to that role. Companies left on a role that no longer exists (e.g. after editing `tracks.json` by hand) are never sent: they wait in Needs you, and the Roles page offers to move them all at once.

### Guardrails

*   **A role sends only when it's switched on *and* its own CV exists.** Otherwise its companies stay `Approved` in the queue (marked *held* on Up next) and don't use up any of the day's sends. They are **never** sent with another role's CV. New roles start switched off.
*   **Recipients see one CV filename** (`<FILE_SLUG>_CV.pdf`) whichever role's CV is attached, so nobody learns you have several versions.
*   **Follow-ups re-attach the same role's CV** the original email carried.
*   **A malformed `tracks.json` fails the run** rather than silently falling back to old pitches.
*   **Every claim must be one that role's CV backs up.** Tailor each CV's title, summary and project order to the role, but keep the facts identical across them: recruiters compare against LinkedIn.

### Searches

Each scrape runs **2 of your searches in turn**, not all of them, so every run looks somewhere new (the same search mostly returns the same results) and Exa costs stay flat, and looks up emails for up to 10 companies. With the 19 shipped searches the whole list is covered about every two and a half days. That is already more new companies than 15 sends a weekday can use, so add searches for variety rather than volume. Banks and telecoms are left out on purpose: their sites rarely show what tech they run, and cold email to them reaches customer care. Searches use Exa's company index, so results are company homepages rather than articles or job boards. The shipped list is `webapp/search_queries_default.txt`; save your own from **Settings** and it replaces it.

### Emails the scraper won't use

An address is used only if it's on the company's own domain (`hr@hubtel.com` for `hubtel.com.gh` counts) or a free mailbox like Gmail, which small firms often use. Anything else (an applicant-tracking vendor's `contact@manatal.com`, an agency) is not sent to: the company waits in **Needs you** with a note saying what was found. Job aggregators (Expertini, Jooble...) and applicant-tracking systems (Workable, Lever, Manatal...) are skipped as leads.

### Replies and bounces are detected automatically

Before each send and every scrape, `check-inbox` reads the sending Gmail (`SENDER_EMAIL` + `GMAIL_APP_PASSWORD`; IMAP is always on for personal Gmail) and marks companies **replied** or **bounced**, so follow-ups never chase someone who answered and the reply rate is real. It is read-only and headers-only: the mailbox is opened with EXAMINE and fetched with `BODY.PEEK`, so nothing is marked read or moved; only Gmail's own delivery-failure notices are read in full.

- **Reply:** answers one of your emails (or a follow-up), or comes from the company's domain after you wrote (`kofi@hubtel.com` for `careers@hubtel.com.gh`). For a company on Gmail etc., only that exact address counts.
- **Not a reply:** out-of-office and other auto-replies, and no-reply "thanks for applying" acknowledgements.
- **Bounce:** a mailer-daemon/postmaster notice naming the address or quoting the email's ID.

Detected marks show as *replied (auto)* with what was found; marks you make by hand are never undone. If the inbox can't be checked, new applications still go out but **follow-ups are skipped** that run. A reply from an unrelated address with a fresh subject (a recruiter's personal Gmail) can't be linked: mark those by hand.

### The review email

After a scrape, companies that need you arrive in one email, grouped by what each needs (choose a role, find an email, confirm the name), with the evidence and a **Fix** button that opens that company in Needs you. The links use `https://sage-apply.vercel.app`; if you fork this, set a `DASHBOARD_URL` repo Variable to your own dashboard's address.

### How companies get their role

The scraper reads each company's page and gives it the role whose **keywords** it mentions most (`role_match.py`). Each role's keywords are editable on its dashboard page, e.g. `react, node.js, saas` for full-stack or `kubernetes, terraform, aws` for DevOps. A role wins only with a clear lead: at least 2 different keywords, and more than any other role. One word repeated many times doesn't count, and partial words don't match (`laws` is not `aws`).

**Otherwise the search's role settles it** (`| software` on the search line), unless the site leans to a *different* role. A bank found by a DevOps search whose site says nothing about tech goes out as DevOps; a company found by a DevOps search whose site only mentions React is a conflict.

**Conflicts, and companies from untagged searches with no clear fit, wait for you** in **Needs you** under *Choose a role*, with a best guess pre-selected and the reason it's unsure. They're never auto-approved or sent until you pick one (on Needs you, by saving the company's page, or by selecting several and pressing **Accept best guess**), and the review email lists them. Each line of `search_queries.txt` (dashboard &rarr; Settings) can name one as a third part: `SaaS startups in Accra | 10 | software`. Lines without one use the default role. From the CLI: `search "fintech startups in Lagos" 10 --track software`.

Every company records why it got its role (`role_reason`, e.g. *site mentions react, typescript*, or *site doesn't mention any role's keywords; pre-selected this search's role* for one waiting on you). This shows on its page and on Up next, where you can change it before the send; your change is recorded as *set by you*. Companies already in the queue keep their current role.

### The DevOps sector letters

The `devops` role has no `letter` in its data (`null`), so it keeps the five sector-specific letters in `get_template_paragraphs()` (fintech, consultancy, bank, isp, general_startup), chosen by each company's `company_type`. To reuse that for another field, rewrite those paragraphs; or give `devops` a `letter` on the dashboard and stop using them.

---

## 📂 Repository Structure

| File | Purpose |
| :--- | :--- |
| **`applicant.py`** | 🔴 **Edit this first.** Your name and contact details, plus the loader for your roles (`tracks.json`). Every other file reads from here. |
| **`webapp/tracks_default.json`** | The built-in roles, used until `tracks.json` is first saved. |
| **`role_match.py`** | Picks each scraped company's role from the keywords its page mentions. |
| **`tests/`** | `python3 -m unittest discover tests`: role CVs, held rows, follow-ups and the dashboard, with SMTP and GitHub faked. |
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

### 3. Personalise `applicant.py` and your roles
Set your name, location, LinkedIn/GitHub and `FILE_SLUG` in `applicant.py`. Then set up your roles, on the dashboard or in `tracks.json` (see [Roles](#-roles-applying-for-several-kinds-of-job-at-once) above). The defaults are written for a DevOps / full-stack / sysadmin job search.

### 4. Add your CVs
One PDF per role, in the root directory, named as that role's `cv_path` (e.g. `cv_software.pdf`). `send` holds any role whose CV is missing rather than sending it with another role's CV.

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
| **Search-all** | `py outreach_pipeline.py search-all` | Runs every search in `search_queries.txt` (falls back to built-in defaults if it doesn't exist). What the scheduled scrape uses. |
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

*   **`.github/workflows/scrape-pipeline.yml`**, every 6 hours (00:17, 06:17, 12:17, 18:17 UTC): `search` against the queries written into the workflow, `scrape` to resolve contact emails for whatever's missing one, then `notify-review` to email you about any `Review` or missing-email row you haven't been told about yet.
*   **`.github/workflows/send-pipeline.yml`**, weekdays at 07:23 UTC (7:23am in Accra, GMT with no DST; off the hour because GitHub delays top-of-the-hour schedules, and weekends skipped so cold emails don't sit under Monday's pile): `auto-approve`, `generate`, `send`, then `followup_pipeline.py run`.

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

Trigger either workflow manually from the Actions tab (`workflow_dispatch`) to test before waiting for its schedule. Want a different cadence? Both are plain cron (`cron: "17 */6 * * *"` and `cron: "23 7 * * 1-5"`); if you change them, update `SEND_*` / `SCRAPE_*` in `webapp/app.py` so the dashboard shows the right times in each workflow's `on.schedule`.

### Setting up your own companion repo

1. Create a new **private** GitHub repo (any name).
2. Seed it with a header-only `outreach_queue.csv` (copy the header row from `outreach_queue.example.csv`), an empty `outreach_log.csv`, and one CV PDF per role, each named as that role's `cv_path` (or upload them later from the dashboard's Roles page).
3. Generate an SSH key pair (`ssh-keygen -t ed25519 -f state_deploy_key -N ""`), add the public half as a **Deploy key with write access** on that repo (Settings &rarr; Deploy keys), and add the private half as the `STATE_DEPLOY_KEY` secret on *this* repo.
4. Set the `STATE_REPO` repo variable to `your-username/your-repo-name`.

---

## 🖥️ Triage Dashboard (optional web app)

`webapp/` is a small hosted Flask app for the parts of the pipeline that genuinely need a human: fixing a scraped company name, supplying an email the scraper missed, reviewing a freshly scraped lead before it's approved, flagging a sent application as replied or bounced, and managing your roles and their CVs. It's a thin client over the same companion repo the scheduled CI uses, not a second source of truth: the dashboard and the CI both read and write the exact same `outreach_queue.csv`.

Pages: an **Overview** that says what needs you, what goes out next and who owes you a reply; **Needs you** (every stuck lead with its fix inline); **Up next** (what the next send emails); **Awaiting reply**; searchable **Companies** with an edit page for each; **Activity**; **Roles** (each role's pitch, CV and on/off switch); and **Settings** (run now, searches). Login is restricted to a single email you set, with first-run password setup and an email-based reset flow (via your existing Gmail credentials, no new email service needed).

It also lets you add companies by hand, edit or delete any company, change the scraper's searches, and start a scrape or send immediately instead of waiting for the schedule.

It's entirely optional: everything it does can also be done by editing files in your companion repo directly, and the CI pipeline runs fine without it. Full setup (a Turso database, a GitHub token, and deploying to Vercel) is documented in **[`webapp/README.md`](webapp/README.md)**.

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
