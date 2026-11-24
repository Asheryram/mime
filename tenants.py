"""
Run the pipeline for every invited user, each fully isolated from the owner
and from each other.

    python3 tenants.py scrape    --state state-repo [--only <id>]
    python3 tenants.py send      --state state-repo [--only <id>]
    python3 tenants.py referrals --state state-repo [--only <id>]

Each invited user has a folder in the private state repo, tenants/<id>/, with
their profile.json, roles (tracks.json), searches, CVs, queue and log, and
their secrets (secrets.enc, encrypted; see webapp/secretbox.py).

Isolation, by construction rather than by care:
- Each user runs in their own fresh temporary directory holding only copies
  of their own files, as a separate process. The pipeline reads every file by
  relative path, so it cannot see anyone else's.
- The child process gets an allowlisted environment: the owner's Gmail, Exa,
  Apify and state-repo credentials are never passed through, so a user with a
  missing key fails that step instead of quietly using the owner's.
- profile.json switches the pipeline into friend mode (applicant.TENANT_MODE):
  no fallback to the owner's built-in roles, searches or sector letters.
- Only their queue and log are copied back, into their own folder.
- One user's failure (a revoked app password, a bad key) is reported and
  skipped; it never stops the others or the owner's run.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "webapp"))
import secretbox  # noqa: E402

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
PIPELINE = os.path.join(HERE, "outreach_pipeline.py")
FOLLOWUPS = os.path.join(HERE, "followup_pipeline.py")

# Files a run may read. Everything else in the folder (secrets.enc above all)
# stays out of the working directory.
INPUT_FILES = ("profile.json", "tracks.json", "search_queries.txt", "outreach_queue.csv", "outreach_log.csv")
# Files a run may write back.
OUTPUT_FILES = ("outreach_queue.csv", "outreach_log.csv")

# Environment variables passed through to a user's run. Nothing else is: in
# particular not SENDER_EMAIL, GMAIL_APP_PASSWORD, APPLICANT_*, EXA_API_KEY,
# APIFY_API_TOKEN, SECRETS_KEY or GITHUB_TOKEN.
PASS_THROUGH = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT", "DASHBOARD_URL",
                "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"}

# (label, argv, needs) per run type. "needs" names the secret a step can't run
# without; a step whose secret is missing is skipped with a note.
STEPS = {
    "scrape": [
        ("check inbox", [PIPELINE, "check-inbox"], "gmail_app_password"),
        ("search", [PIPELINE, "search-all"], "exa_api_key"),
        ("find emails", [PIPELINE, "scrape"], "apify_api_token"),
        ("review email", [PIPELINE, "notify-review"], "gmail_app_password"),
    ],
    "send": [
        ("check inbox", [PIPELINE, "check-inbox"], "gmail_app_password"),
        ("referrals", [PIPELINE, "send-referrals"], "gmail_app_password"),
        ("check-ins", [PIPELINE, "send-checkins"], "gmail_app_password"),
        ("auto-approve", [PIPELINE, "auto-approve"], None),
        ("letters", [PIPELINE, "generate"], None),
        ("send", [PIPELINE, "send"], "gmail_app_password"),
        ("follow-ups", [FOLLOWUPS, "run"], "gmail_app_password"),
    ],
    "referrals": [
        ("check inbox", [PIPELINE, "check-inbox"], "gmail_app_password"),
        ("referrals", [PIPELINE, "send-referrals"], "gmail_app_password"),
        ("check-ins", [PIPELINE, "send-checkins"], "gmail_app_password"),
    ],
}
STEP_TIMEOUT = 45 * 60


def tenant_ids(state_dir, only=None):
    root = os.path.join(state_dir, "tenants")
    if not os.path.isdir(root):
        return []
    ids = sorted(d for d in os.listdir(root) if TENANT_ID_RE.match(d) and os.path.isdir(os.path.join(root, d)))
    if only:
        return [only] if only in ids else []
    return ids


def child_env(profile, secrets):
    env = {k: v for k, v in os.environ.items() if k in PASS_THROUGH}
    env["PYTHONUNBUFFERED"] = "1"
    env["SENDER_EMAIL"] = env["APPLICANT_EMAIL"] = profile["email"].strip()
    if profile.get("phone"):
        env["APPLICANT_PHONE"] = profile["phone"].strip()
    if secrets.get("gmail_app_password"):
        env["GMAIL_APP_PASSWORD"] = secrets["gmail_app_password"]
    if secrets.get("exa_api_key"):
        env["EXA_API_KEY"] = secrets["exa_api_key"]
    if secrets.get("apify_api_token"):
        env["APIFY_API_TOKEN"] = secrets["apify_api_token"]
    return env


def prepare_workdir(folder, tracks):
    work = tempfile.mkdtemp(prefix="tenant-")
    for name in INPUT_FILES:
        src = os.path.join(folder, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(work, name))
    # CVs named by their roles only, as plain filenames (validated on load too).
    for t in tracks.values():
        cv = str(t.get("cv_path", ""))
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.pdf$", cv) and ".." not in cv:
            src = os.path.join(folder, cv)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(work, cv))
    return work


def run_tenant(state_dir, tid, kind, log=print):
    """Returns a one-line outcome for the summary."""
    folder = os.path.join(state_dir, "tenants", tid)

    def say(msg):
        log(f"[{tid}] {msg}")

    try:
        with open(os.path.join(folder, "profile.json"), encoding="utf-8") as f:
            profile = json.load(f)
    except (OSError, ValueError):
        say("skipped: no profile yet.")
        return "skipped (no profile)"
    if not (profile.get("full_name") or "").strip() or "@" not in (profile.get("email") or ""):
        say("skipped: profile needs a name and Gmail address.")
        return "skipped (profile incomplete)"
    if profile.get("paused"):
        say("skipped: paused.")
        return "paused"
    try:
        with open(os.path.join(folder, "tracks.json"), encoding="utf-8") as f:
            tracks = json.load(f)
        assert isinstance(tracks, dict) and tracks
    except (OSError, ValueError, AssertionError):
        say("skipped: no roles set up yet.")
        return "skipped (no roles)"
    try:
        blob = open(os.path.join(folder, "secrets.enc"), "rb").read() if os.path.exists(
            os.path.join(folder, "secrets.enc")) else b""
        secrets = secretbox.decrypt(blob)
    except secretbox.SecretsUnavailable as e:
        say(f"skipped: can't read their keys ({e}).")
        return "skipped (keys unreadable)"

    work = prepare_workdir(folder, tracks)
    env = child_env(profile, secrets)
    outcome = "ok"
    try:
        for label, argv, needs in STEPS[kind]:
            if needs and not secrets.get(needs):
                say(f"{label}: skipped, no {needs.replace('_', ' ')} saved.")
                continue
            say(f"{label}...")
            try:
                proc = subprocess.run([sys.executable] + argv, cwd=work, env=env, capture_output=True,
                                      text=True, timeout=STEP_TIMEOUT)
            except subprocess.TimeoutExpired:
                say(f"{label}: timed out; stopping this user's run.")
                outcome = f"stopped at {label} (timeout)"
                break
            for line in (proc.stdout + proc.stderr).splitlines():
                if line.strip():
                    log(f"[{tid}]   {line}")
            if proc.returncode != 0:
                say(f"{label}: failed (exit {proc.returncode}); stopping this user's run.")
                outcome = f"stopped at {label}"
                break
        for name in OUTPUT_FILES:
            src = os.path.join(work, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(folder, name))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return outcome


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the pipeline for each invited user, isolated.")
    ap.add_argument("kind", choices=sorted(STEPS))
    ap.add_argument("--state", required=True, help="path to the cloned state repo")
    ap.add_argument("--only", default="", help="run just this user (a dashboard 'Run now')")
    args = ap.parse_args(argv)
    if args.only and not TENANT_ID_RE.match(args.only):
        print(f"Ignoring invalid user id {args.only!r}.")
        return 0
    if not secretbox.available():
        print("[users] SECRETS_KEY isn't set, so invited users' keys can't be read; skipping them.")
        return 0
    ids = tenant_ids(args.state, args.only or None)
    if not ids:
        print("[users] No invited users to run." if not args.only else f"[users] No user {args.only!r}.")
        return 0
    summary = {tid: run_tenant(args.state, tid, args.kind) for tid in ids}
    print("[users] Summary: " + "; ".join(f"{t}: {o}" for t, o in summary.items()))
    return 0  # one user's trouble never fails the owner's run


if __name__ == "__main__":
    sys.exit(main())
