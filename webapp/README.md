# Outreach Triage Desk

A small hosted web app for the parts of the [mime](../README.md) job-outreach pipeline that need a human: fixing a scraped company name, supplying a missing email, reviewing a freshly scraped lead before it's approved, flagging a sent application as replied or bounced, and swapping in a new CV.

It's a thin client over the same private companion repo the scheduled GitHub Actions pipeline uses. There's no separate database for the queue itself; the dashboard and the CI read and write the exact same `outreach_queue.csv`, so nothing can drift out of sync between them.

Entirely optional. Everything it does can be done by hand-editing the CSV in your companion repo, and the daily pipeline runs fine without it.

---

## How it fits together

```
 Vercel (this app)                 GitHub                        Turso
 ┌───────────────────┐   HTTPS     ┌──────────────────────┐      ┌─────────────┐
 │ Flask, stateless    │ ───────── │ private companion repo│      │ password hash│
 │ login + dashboard   │  Contents │  outreach_queue.csv    │      │ (single row) │
 │ pages               │  API      │  outreach_log.csv      │ ◄──┐ │              │
 └───────────────────┘             │  your_cv.pdf           │    │ └─────────────┘
                                   └──────────────────────┘    │
                                              ▲                 │ login/reset only,
                                              │ git (SSH deploy │ never the queue
                                              │ key)            │
                                   ┌──────────────────────┐    │
                                   │ .github/workflows/     │    │
                                   │ scrape- & send-        │────┘
                                   │ pipeline.yml (scheduled)│
                                   └──────────────────────┘
```

Vercel's Python functions are stateless between requests (no persistent disk), so instead of `git clone`-ing the companion repo like the GitHub Actions workflow does, this app calls GitHub's Contents API directly over HTTPS on every read and write (see `state_repo.py`). Turso holds exactly one thing: your login's password hash, re-read on every request for the same stateless reason.

---

## Prerequisites

You'll need accounts on three services, all of which have usable free tiers:

1. **A private companion repo** for `outreach_queue.csv`, `outreach_log.csv`, and your CV. If you already set one up for the [Scheduled CI](../README.md#-scheduled-ci-fully-unattended) workflow, reuse it. Otherwise follow [Setting up your own companion repo](../README.md#setting-up-your-own-companion-repo) in the root README first.
2. **[Turso](https://turso.tech)**: a free SQLite-compatible database, used only for your login.
3. **[Vercel](https://vercel.com)**: hosts the app, free tier is plenty for a single-user tool.

---

## Environment variables

| Variable | What it's for | Where to get it |
| :--- | :--- | :--- |
| `FLASK_SECRET_KEY` | Signs session cookies and password-reset tokens | Generate one: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `SENDER_EMAIL` | Sends password-reset emails | Same Gmail address as the main pipeline's `.env` |
| `GMAIL_APP_PASSWORD` | Same | Same value as the main pipeline's `.env` |
| `ALLOWED_EMAIL` | The one email allowed to log in | Your own email; defaults to the repo author's if unset |
| `GITHUB_TOKEN` | Reads/writes the companion repo, and shows/starts pipeline runs | A **fine-grained PAT**: github.com/settings/personal-access-tokens/new &rarr; Repository access: your companion repo **and** the pipeline repo &rarr; Permissions: Contents = Read and write, Actions = Read and write. Without Actions, everything works except the run status and the Run now buttons |
| `STATE_REPO` | Which companion repo to use | `your-github-username/your-state-repo`. Only needed if you're not the original repo author |
| `PIPELINE_REPO` | Which repo holds the two workflows (for run status and Run now) | `your-github-username/your-fork`. Only needed if you're not the original repo author |
| `CV_FILENAME` | Your CV's filename in the companion repo | Same as `CV_PATH` in `applicant.py`. Only needed if you're not the original repo author |
| `TURSO_DATABASE_URL` | Your Turso database | `turso db show <name>` or the Turso dashboard |
| `TURSO_AUTH_TOKEN` | Auth for that database | `turso db tokens create <name>` or the Turso dashboard |

Copy `.env.example` to `.env` and fill these in for local development; `app.py` loads `.env` automatically (and never overrides a real environment variable, so Vercel's own settings always win in production).

---

## Deploying to Vercel

1. **Import the project**: New Project &rarr; import your fork of this repo &rarr; set **Root Directory** to `webapp`.
2. **Set the environment variables** above (Project &rarr; Settings &rarr; Environment Variables).
3. **Deploy.**
4. Visit `your-app.vercel.app/setup` and choose a password. This route only works once, before a password exists; after that it redirects to `/login`.

`vercel.json` routes every path to `api/index.py`, which imports the Flask `app` object from `app.py` (Vercel's Python runtime serves any WSGI `app` it finds there).

---

## Running it locally

```bash
cd webapp
pip install -r requirements.txt
cp .env.example .env   # then fill in the real values
python app.py
```

Opens on `http://127.0.0.1:5000`. One known snag: `libsql` (the Turso client) ships no prebuilt wheel for every platform, so `pip install` may try to compile it from source and fail if you don't have a working Rust toolchain. This is a local-machine issue only; Vercel's Linux runtime installs the prebuilt wheel without any of this.

---

## Pages

| Page | Route | What it's for |
| :--- | :--- | :--- |
| Overview | `/` | What needs you, what goes out next, who owes you a reply; pipeline schedule and last-run status; recent changes |
| Needs you | `/attention` | A triage inbox for every lead the pipeline couldn't finish (unclear name, no email found, failed send): list on the left, the selected lead's fix on the right, with suggested names and addresses and Google/LinkedIn search links. Saves in place and moves to the next lead; `j`/`k` move, `e` edits, Enter saves, `x` drops, Space selects for bulk drop/retry |
| Up next | `/upcoming` | What the next 09:00 send will email, in order, with which ones fit the 15-a-day cap. "Don't send" holds one back. Also lists leads still waiting for the scraper to find an email |
| Awaiting reply | `/sent` | Everything sent, filterable by awaiting / replied / bounced. Marking a reply or bounce stops the follow-up; undo is one click |
| Companies | `/companies` | Search and filter the whole queue; every row opens its edit page |
| Company | `/companies/<name>` | Edit any field or status, quick actions (retry, restore, don't email), history, delete |
| Add company | `/companies/new` | Add a lead you found yourself. With an email it goes out at the next send; with a website, the next scrape finds the email |
| Activity | `/activity` | Every email the pipeline sent, and every change to the queue by the pipeline or by you |
| Settings | `/settings` | Run scrape/send now, edit the scraper's searches, replace your CV |

A shared tab bar with live counts sits under the header on every page.

### Editing at the same time as the pipeline

Every dashboard write is pinned to the version of the queue it read. If a pipeline run commits in between, GitHub rejects the write and the dashboard re-reads and re-applies your change instead of overwriting the run's. In the other direction, both workflows rebase onto any dashboard edits made while they ran before pushing, so a send run's "Sent" marks aren't lost (which would re-send those emails the next day). The two workflows also share a concurrency group, so "Run now" can't start one on top of the other.

---

## Security notes

*   Login is restricted to exactly one email address (`ALLOWED_EMAIL`); there's no signup.
*   The GitHub token should be a fine-grained PAT scoped to only your companion repo with only Contents read/write. Never use a broad classic token with access to every repo you own.
*   `FLASK_SECRET_KEY` signs both session cookies and password-reset tokens; treat it like any other secret and don't commit it.
*   A password-reset token embeds a fragment of the current password hash, so it stops working automatically the moment the password changes, even if the link leaks.
