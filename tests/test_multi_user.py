"""
Invited users: each friend gets a private workspace, and nothing of the
owner's (data, credentials, pitch, name) can leak into theirs, or theirs into
anyone else's.

Run from the repo root:  python3 -m unittest discover tests
"""
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("FLASK_SECRET_KEY", "test")

from cryptography.fernet import Fernet  # noqa: E402
from test_roles import InTempDir, FakeStateRepo, FakeSMTP, write_queue, write_file  # noqa: E402

OWNER = "owner@example.com"
FRIEND = "ama.mensah@gmail.com"
OTHER = "kofi@gmail.com"
KEY = Fernet.generate_key().decode()


class FakeDB:
    """In-memory stand-in for webapp/db.py's Turso tables."""
    def __init__(self, db):
        self.invites, self.members = {}, {}
        db.load_auth = lambda: {"password_hash": "owner-hash"}
        db.is_invited = lambda e: e in self.invites
        db.add_invite = lambda e, when: self.invites.setdefault(e, when)
        db.get_member = lambda e: self.members.get(e)
        db.create_member = lambda e, t, h, when: self.members.__setitem__(
            e, {"email": e, "tenant": t, "password_hash": h})
        db.set_member_password = lambda e, h, when: self.members[e].__setitem__("password_hash", h)

        def remove(e):
            self.invites.pop(e, None)
            m = self.members.pop(e, None)
            return m["tenant"] if m else None
        db.remove_access = remove
        db.list_invites = lambda: [{"email": e, "invited_at": w, "tenant": (self.members.get(e) or {}).get("tenant"),
                                    "joined_at": w} for e, w in self.invites.items()]


def queue_bytes(state_repo, rows):
    F = state_repo.QUEUE_FIELDS
    out = [",".join(F)]
    for r in rows:
        full = {f: "" for f in F}
        full.update(r)
        out.append(",".join(full[f] for f in F))
    return ("\n".join(out) + "\n").encode()


class DashboardIsolationTests(unittest.TestCase):
    def setUp(self):
        os.environ["SECRETS_KEY"] = KEY
        os.environ["ALLOWED_EMAIL"] = OWNER
        import state_repo, app as webapp, db
        importlib.reload(state_repo); importlib.reload(db); importlib.reload(webapp)
        self.sr, self.app, self.db = state_repo, webapp, db
        self.fake = FakeStateRepo(state_repo)
        self.fdb = FakeDB(db)
        self.app.check_password_hash = lambda h, pw: h == f"hash:{pw}" or (h == "owner-hash" and pw == "owner-pw")
        self.app.generate_password_hash = lambda pw: f"hash:{pw}"
        self.app.send_email = lambda *a, **k: None
        self.dispatched = []
        state_repo.dispatch_workflow = self.record_dispatch
        # The owner's data, at the repo root.
        self.fake.files["outreach_queue.csv"] = queue_bytes(state_repo, [
            {"company_name": "Owner Secret Co", "website": "https://ownersecret.co", "email": "hr@ownersecret.co",
             "status": "Approved", "track": "devops"}])
        self.fake.files["search_queries.txt"] = b"owner private search | 10 | devops\n"
        self.fake.files[state_repo.DEFAULT_TRACKS["devops"]["cv_path"]] = b"%PDF-owner"
        self.c = webapp.app.test_client()
        self.addCleanup(lambda: os.environ.pop("SECRETS_KEY", None))

    def record_dispatch(self, wf, inputs=None):
        inputs = dict(inputs or {})
        if self.sr.current_tenant():
            inputs["tenant"] = self.sr.current_tenant()
        self.dispatched.append((wf, inputs))

    def login(self, email, pw):
        return self.c.post("/login", data={"email": email, "password": pw})

    def signup(self, email, pw="friend-password"):
        return self.c.post("/signup", data={"email": email, "password": pw, "password2": pw})

    def friend(self, email=FRIEND):
        self.fdb.invites[email] = "2026-11-24"
        self.signup(email)
        return self.fdb.members[email]["tenant"]

    # --- sign-up and access -------------------------------------------------------------

    def test_only_invited_emails_can_sign_up(self):
        r = self.signup("stranger@gmail.com")
        self.assertIn(b"hasn&#39;t been invited", r.data)
        self.assertEqual(self.fdb.members, {})
        tenant = self.friend()
        self.assertRegex(tenant, r"^ama-mensah-[0-9a-f]{6}$")

    def test_owner_email_cannot_be_taken_by_signup(self):
        self.fdb.invites[OWNER] = "x"
        self.signup(OWNER)
        self.assertNotIn(OWNER, self.fdb.members)

    def test_member_never_sees_owner_data(self):
        self.friend()
        for url in ["/", "/companies", "/upcoming", "/attention", "/sent", "/roles", "/settings"]:
            with self.subTest(url=url):
                page = self.c.get(url, follow_redirects=True).data.decode()
                self.assertNotIn("Owner Secret Co", page)
                self.assertNotIn("owner private search", page)
        roles = self.c.get("/roles").data.decode()
        self.assertNotIn("Cloud &amp; DevOps Engineer", roles, "owner's built-in roles must not appear")
        self.assertIn("Add your first role", roles)
        self.assertNotEqual(self.c.get("/companies/owner-secret-co").status_code, 200)

    def test_member_writes_land_in_their_folder_only(self):
        tenant = self.friend()
        before = {k: v for k, v in self.fake.files.items() if not k.startswith("tenants/")}
        self.c.post("/settings/queries", data={"queries": "lagos fintechs | 10"})
        self.c.post("/profile", data={"full_name": "Ama Mensah", "email": FRIEND})
        after = {k: v for k, v in self.fake.files.items() if not k.startswith("tenants/")}
        self.assertEqual(before, after, "the owner's files were touched")
        self.assertIn(f"tenants/{tenant}/search_queries.txt", self.fake.files)
        self.assertIn(f"tenants/{tenant}/profile.json", self.fake.files)
        self.assertTrue(all(m.startswith(f"[{tenant}] ") for m in self.fake.messages))

    def test_two_members_are_isolated_from_each_other(self):
        ama = self.friend(FRIEND)
        self.c.post("/settings/queries", data={"queries": "ama only search | 10"})
        self.c.get("/logout")
        kofi = self.friend(OTHER)
        self.assertNotEqual(ama, kofi)
        self.assertNotIn("ama only search", self.c.get("/settings").data.decode())

    def test_owner_still_sees_own_data_after_a_member_request(self):
        self.friend()
        self.c.get("/companies")
        self.c.get("/logout")
        self.login(OWNER, "owner-pw")
        self.assertIn("Owner Secret Co", self.c.get("/companies").data.decode())

    def test_removed_member_is_signed_out_and_paused(self):
        tenant = self.friend()
        self.c.post("/profile", data={"full_name": "Ama Mensah", "email": FRIEND})
        other = self.app.app.test_client()
        other.post("/login", data={"email": OWNER, "password": "owner-pw"})
        other.post("/people", data={"action": "remove", "email": FRIEND})
        r = self.c.get("/companies")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        prof = json.loads(self.fake.files[f"tenants/{tenant}/profile.json"])
        self.assertTrue(prof["paused"], "their scheduled runs must stop too")

    def test_pause_stops_pipeline_but_keeps_sign_in(self):
        tenant = self.friend()
        self.c.post("/profile", data={"full_name": "Ama Mensah", "email": FRIEND})
        self.c.get("/logout")
        self.login(OWNER, "owner-pw")
        self.c.post("/people", data={"action": "pause", "email": FRIEND})
        prof = lambda: json.loads(self.fake.files[f"tenants/{tenant}/profile.json"])
        self.assertTrue(prof().get("paused"))
        self.assertIn(FRIEND, self.fdb.members, "pausing must not revoke the login")
        self.assertIn(b"Resume pipeline", self.c.get("/people").data)
        self.c.get("/logout")
        self.login(FRIEND, "friend-password")
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200, "a paused member can still sign in")
        self.assertIn(b"Your pipeline is paused", r.data)
        # They can still edit their profile without un-pausing themselves.
        self.c.post("/profile", data={"full_name": "Ama M.", "email": FRIEND})
        self.assertTrue(prof().get("paused"))
        before = len(self.dispatched)
        self.c.post("/settings/run/send")
        self.assertEqual(len(self.dispatched), before, "a paused member can't start a run")
        self.c.get("/logout")
        self.login(OWNER, "owner-pw")
        self.c.post("/people", data={"action": "resume", "email": FRIEND})
        self.assertNotIn("paused", prof())

    def test_removing_access_keeps_data_and_reinvite_restores_it(self):
        tenant = self.friend()
        self.c.post("/profile", data={"full_name": "Ama Mensah", "email": FRIEND})
        self.c.post("/settings/queries", data={"queries": "ama search | 10"})
        self.c.get("/logout")
        self.login(OWNER, "owner-pw")
        self.c.post("/people", data={"action": "remove", "email": FRIEND})
        kept = {k for k in self.fake.files if k.startswith(f"tenants/{tenant}/")}
        self.assertIn(f"tenants/{tenant}/search_queries.txt", kept, "removing access must keep their data")
        self.assertNotIn(FRIEND, self.fdb.members)
        self.c.post("/people", data={"action": "invite", "email": FRIEND})
        self.c.get("/logout")
        r = self.signup(FRIEND)
        self.assertEqual(self.fdb.members[FRIEND]["tenant"], tenant, "same workspace back")
        self.assertNotIn("paused", json.loads(self.fake.files[f"tenants/{tenant}/profile.json"]))
        self.assertIn("ama search", self.c.get("/settings").data.decode())

    def test_failed_invite_email_says_why(self):
        import smtplib
        with mock.patch.object(self.app, "send_email",
                               side_effect=smtplib.SMTPAuthenticationError(535, b"bad")):
            self.login(OWNER, "owner-pw")
            r = self.c.post("/people", data={"action": "invite", "email": "keren@gmail.com"}, follow_redirects=True)
        self.assertIn(b"Gmail rejected GMAIL_APP_PASSWORD", r.data)
        self.assertIn("keren@gmail.com", self.fdb.invites)

    def test_people_page_is_owner_only(self):
        self.friend()
        r = self.c.post("/people", data={"action": "invite", "email": "x@gmail.com"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("x@gmail.com", self.fdb.invites)

    def test_owner_invites(self):
        self.login(OWNER, "owner-pw")
        self.c.post("/people", data={"action": "invite", "email": "New@Gmail.com"})
        self.assertIn("new@gmail.com", self.fdb.invites)

    def test_owner_builtin_pitch_is_never_offered_to_a_member(self):
        tenant = self.friend()
        # A member who happens to name a role "devops", like the owner's.
        mine = dict(self.sr.DEFAULT_TRACKS["software"], role_title="Mine", cv_path="cv_devops.pdf",
                    highlights=["mine one", "mine two"])
        self.fake.files[f"tenants/{tenant}/tracks.json"] = json.dumps({"devops": mine}).encode()
        page = self.c.get("/roles/devops").data.decode()
        self.assertNotIn("Reset to built-in pitch", page)
        self.c.post("/roles/devops/reset")
        saved = json.loads(self.fake.files[f"tenants/{tenant}/tracks.json"])
        self.assertEqual(saved["devops"]["highlights"], ["mine one", "mine two"])

    def test_member_run_now_only_runs_them(self):
        tenant = self.friend()
        self.c.post("/settings/run/send")
        self.assertTrue(self.dispatched)
        self.assertTrue(all(i.get("tenant") == tenant for _, i in self.dispatched))

    def test_member_history_is_their_folder_only(self):
        import state_repo
        tenant = self.friend()
        seen = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            seen.update(params or {})
            r = mock.Mock(); r.raise_for_status = lambda: None; r.json = lambda: []
            return r
        with mock.patch.object(state_repo.requests, "get", fake_get):
            state_repo.set_tenant(tenant)
            try:
                state_repo.recent_commits(5)
            finally:
                state_repo.set_tenant("")
        self.assertEqual(seen.get("path"), f"tenants/{tenant}")

    # --- profile and keys --------------------------------------------------------------

    def test_keys_are_encrypted_and_never_shown(self):
        tenant = self.friend()
        self.c.post("/profile", data={"full_name": "Ama Mensah", "email": FRIEND,
                                      "gmail_app_password": "abcd efgh ijkl mnop", "exa_api_key": "exa-secret-123"})
        blob = self.fake.files[f"tenants/{tenant}/secrets.enc"]
        self.assertNotIn(b"abcdefghijklmnop", blob)
        self.assertNotIn(b"exa-secret-123", blob)
        self.assertEqual(json.loads(Fernet(KEY.encode()).decrypt(blob)),
                         {"gmail_app_password": "abcdefghijklmnop", "exa_api_key": "exa-secret-123"})
        page = self.c.get("/profile").data.decode()
        self.assertNotIn("abcdefghijklmnop", page)
        self.assertNotIn("exa-secret-123", page)
        self.assertIn("saved", page)
        prof = json.loads(self.fake.files[f"tenants/{tenant}/profile.json"])
        self.assertEqual(prof["keys_set"], ["exa_api_key", "gmail_app_password"])
        self.assertNotIn("abcdefghijklmnop", json.dumps(prof))

    def test_blank_key_keeps_the_saved_one_and_remove_removes(self):
        tenant = self.friend()
        self.c.post("/profile", data={"full_name": "A", "email": FRIEND, "exa_api_key": "k1"})
        self.c.post("/profile", data={"full_name": "A", "email": FRIEND, "exa_api_key": ""})
        dec = lambda: json.loads(Fernet(KEY.encode()).decrypt(self.fake.files[f"tenants/{tenant}/secrets.enc"]))
        self.assertEqual(dec(), {"exa_api_key": "k1"})
        self.c.post("/profile", data={"full_name": "A", "email": FRIEND, "remove_exa_api_key": "on"})
        self.assertEqual(dec(), {})

    def test_bad_app_password_rejected(self):
        tenant = self.friend()
        r = self.c.post("/profile", data={"full_name": "A", "email": FRIEND, "gmail_app_password": "short"},
                        follow_redirects=True)
        self.assertIn(b"16 letters", r.data)
        self.assertNotIn(f"tenants/{tenant}/secrets.enc", self.fake.files)

    def test_no_secrets_key_means_no_keys_saved(self):
        os.environ.pop("SECRETS_KEY")
        tenant = self.friend()
        r = self.c.post("/profile", data={"full_name": "A", "email": FRIEND, "exa_api_key": "k"}, follow_redirects=True)
        self.assertIn(b"SECRETS_KEY", r.data)
        self.assertNotIn(f"tenants/{tenant}/secrets.enc", self.fake.files)

    def test_setup_checklist_guides_a_new_member(self):
        self.friend()
        page = self.c.get("/").data.decode()
        self.assertIn("Get set up", page)
        self.assertIn("Save your Gmail app password", page)


class FriendModeTests(InTempDir):
    """The pipeline running in a friend's folder: their identity, never the owner's."""

    def write_friend(self, tracks=True, letter=True):
        write_file("profile.json", json.dumps({
            "full_name": "Ama Mensah", "headline": "Frontend Engineer", "email": FRIEND,
            "location": "Lagos, Nigeria", "linkedin": "https://linkedin.com/in/ama", "portfolio": ""}).encode())
        if tracks:
            import json as j
            t = {"frontend": {
                "cv_path": "cv_frontend.pdf", "headline": "Frontend Engineer", "role_title": "Frontend Engineer",
                "subject_area": "Frontend Engineering", "interest_area": "frontend engineering",
                "credentials": "a frontend engineer who builds accessible React apps",
                "closing_skills": "React skills", "highlights": ["Built A", "Shipped B"],
                "followup_expertise": "React", "keywords": ["react"], "active": True,
                "letter": ({"intro": "I want the {role} role at {company}.", "lead_in": "",
                            "bullets": [{"title": "One", "text": "x"}, {"title": "Two", "text": "y"}],
                            "closing": "Thanks, {company}."} if letter else None)}}
            write_file("tracks.json", j.dumps(t).encode())
        write_file("cv_frontend.pdf", b"%PDF-ama")

    def test_identity_comes_from_the_profile(self):
        self.write_friend()
        with mock.patch.dict(os.environ, {"APPLICANT_PHONE": ""}):
            applicant, _, _ = self.reload_pipeline()
        self.assertTrue(applicant.TENANT_MODE)
        self.assertEqual(applicant.FULL_NAME, "Ama Mensah")
        self.assertEqual(applicant.FILE_SLUG, "Ama_Mensah")
        self.assertEqual(applicant.CV_ATTACHMENT_NAME, "Ama_Mensah_CV.pdf")
        sig = applicant.signature_block()
        self.assertIn("Ama Mensah", sig)
        self.assertIn("Lagos, Nigeria", sig)
        self.assertNotIn("GitHub", sig, "no empty link lines")
        self.assertNotIn("Tetteh", sig)

    def test_no_roles_means_no_owner_roles(self):
        self.write_friend(tracks=False)
        with self.assertRaises(ValueError):
            self.reload_pipeline()

    def test_role_without_letter_is_held_not_sent_with_owner_letters(self):
        self.write_friend(letter=False)
        applicant, _, _ = self.reload_pipeline()
        self.assertIn("no cover letter", applicant.track_hold_reason("frontend"))

    def test_no_owner_searches(self):
        # Give the friend roles named like the owner's ("software", "devops"),
        # so the owner's tagged searches would otherwise parse as theirs.
        self.write_friend()
        t = json.load(open("tracks.json"))
        t["software"] = dict(t["frontend"]); t["devops"] = dict(t["frontend"])
        write_file("tracks.json", json.dumps(t).encode())
        _, op, _ = self.reload_pipeline()
        self.assertEqual(op.load_search_queries(), [])

    def test_a_friend_send_carries_nothing_of_the_owner(self):
        self.write_friend()
        saved = os.environ.get("SENDER_EMAIL")
        os.environ["SENDER_EMAIL"] = FRIEND
        try:
            applicant, op, _ = self.reload_pipeline()
            import smtp_client
            fake = FakeSMTP(); fake.install(smtp_client)
            op.time.sleep = lambda s: None
            write_queue(op, [{"company_name": "Lagos Co", "email": "hr@lagos.co", "status": "Approved",
                              "track": "frontend", "recipient_name": "The Team"}])
            op.run_generate_letters()
            op.run_send_emails()
        finally:
            if saved is None:
                os.environ.pop("SENDER_EMAIL", None)
            else:
                os.environ["SENDER_EMAIL"] = saved
        self.assertEqual(len(fake.sent), 1)
        msg = fake.sent[0][1]
        body = msg.get_body(preferencelist=("plain",)).get_content()
        files = {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}
        self.assertIn("Ama Mensah", body)
        self.assertEqual(files["Ama_Mensah_CV.pdf"], b"%PDF-ama")
        letter = [v for k, v in files.items() if k != "Ama_Mensah_CV.pdf"][0]
        pdf_text = subprocess.run(["pdftotext", "-", "-"], input=letter, capture_output=True).stdout.decode()
        everything = body + msg["Subject"] + pdf_text + " ".join(files)
        for owner_marker in ("Tetteh", "Asher", "Asheryram", "KNUST", "AmaliTech", "Accra"):
            self.assertNotIn(owner_marker, everything, owner_marker)
        self.assertIn("AMA MENSAH", pdf_text)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state)
        self.env = mock.patch.dict(os.environ, {
            "SECRETS_KEY": KEY, "SENDER_EMAIL": OWNER, "GMAIL_APP_PASSWORD": "ownerpassword123",
            "APPLICANT_EMAIL": OWNER, "APPLICANT_PHONE": "+233 owner", "EXA_API_KEY": "owner-exa",
            "APIFY_API_TOKEN": "owner-apify", "GITHUB_TOKEN": "owner-gh", "DASHBOARD_URL": "https://dash"})
        self.env.start(); self.addCleanup(self.env.stop)
        import tenants
        self.t = importlib.reload(tenants)

    def make(self, tid, secrets, profile=None, tracks=True):
        d = os.path.join(self.state, "tenants", tid)
        os.makedirs(d)
        json.dump(profile or {"full_name": "Ama", "email": FRIEND, "phone": "+234 1"}, open(os.path.join(d, "profile.json"), "w"))
        if tracks:
            json.dump({"frontend": {"cv_path": "cv_frontend.pdf"}}, open(os.path.join(d, "tracks.json"), "w"))
            open(os.path.join(d, "cv_frontend.pdf"), "wb").write(b"%PDF-ama")
        open(os.path.join(d, "outreach_queue.csv"), "w").write("company_name\nOld\n")
        if secrets is not None:
            open(os.path.join(d, "secrets.enc"), "wb").write(Fernet(KEY.encode()).encrypt(json.dumps(secrets).encode()))
        return d

    def run_with_fake(self, tid, kind):
        calls = []

        def fake_run(argv, cwd=None, env=None, **kw):
            calls.append({"argv": argv, "cwd": cwd, "env": dict(env), "files": sorted(os.listdir(cwd))})
            with open(os.path.join(cwd, "outreach_queue.csv"), "a") as f:
                f.write("New\n")
            with open(os.path.join(cwd, "stray.txt"), "w") as f:
                f.write("should not be copied back")
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(self.t.subprocess, "run", fake_run):
            outcome = self.t.run_tenant(self.state, tid, kind, log=lambda m: None)
        return outcome, calls

    def test_owner_credentials_never_reach_a_friend(self):
        self.make("ama-abc123", {"gmail_app_password": "amapasswordabcde"})
        _, calls = self.run_with_fake("ama-abc123", "scrape")
        self.assertTrue(calls)
        for c in calls:
            env = c["env"]
            self.assertEqual(env["SENDER_EMAIL"], FRIEND)
            self.assertEqual(env["GMAIL_APP_PASSWORD"], "amapasswordabcde")
            for leaked in ("owner-exa", "owner-apify", "owner-gh", "ownerpassword123", OWNER, "+233 owner", KEY):
                self.assertNotIn(leaked, json.dumps(env), leaked)
            self.assertNotIn("SECRETS_KEY", env)
            self.assertNotIn("EXA_API_KEY", env, "missing key: skip, never borrow the owner's")
        labels = [c["argv"][-1] for c in calls]
        self.assertNotIn("search-all", labels, "no Exa key: search skipped")
        self.assertNotIn("scrape", labels, "no Apify token: email lookup skipped")

    def test_workdir_holds_only_their_inputs_and_only_outputs_come_back(self):
        d = self.make("ama-abc123", {"gmail_app_password": "amapasswordabcde"})
        _, calls = self.run_with_fake("ama-abc123", "referrals")
        self.assertEqual(calls[0]["files"], sorted(["profile.json", "tracks.json", "outreach_queue.csv",
                                                    "cv_frontend.pdf"]))
        self.assertNotIn("secrets.enc", calls[0]["files"])
        self.assertIn("New", open(os.path.join(d, "outreach_queue.csv")).read())
        self.assertFalse(os.path.exists(os.path.join(d, "stray.txt")))
        self.assertFalse(os.path.exists(calls[0]["cwd"]), "temporary folder cleaned up")

    def test_skips_instead_of_running_half_set_up(self):
        self.make("no-roles-1", {"gmail_app_password": "x" * 16}, tracks=False)
        self.make("paused-12", {}, profile={"full_name": "P", "email": "p@gmail.com", "paused": True})
        self.assertEqual(self.run_with_fake("no-roles-1", "send"), ("skipped (no roles)", []))
        self.assertEqual(self.run_with_fake("paused-12", "send"), ("paused", []))

    def test_unreadable_keys_skip_that_user_only(self):
        self.make("bad-keys-1", None)
        open(os.path.join(self.state, "tenants", "bad-keys-1", "secrets.enc"), "wb").write(b"garbage")
        self.assertEqual(self.run_with_fake("bad-keys-1", "send")[0], "skipped (keys unreadable)")

    def test_only_flag_and_bad_ids(self):
        self.make("ama-abc123", {})
        self.make("kofi-def456", {})
        os.makedirs(os.path.join(self.state, "tenants", "..evil"))
        self.assertEqual(self.t.tenant_ids(self.state), ["ama-abc123", "kofi-def456"])
        self.assertEqual(self.t.tenant_ids(self.state, "kofi-def456"), ["kofi-def456"])
        self.assertEqual(self.t.tenant_ids(self.state, "nobody-000"), [])
        self.assertEqual(self.t.main(["send", "--state", self.state, "--only", "../x"]), 0)

    def test_a_failing_step_stops_that_user_and_never_fails_the_job(self):
        self.make("ama-abc123", {"gmail_app_password": "amapasswordabcde"})

        def failing(argv, cwd=None, env=None, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        with mock.patch.object(self.t.subprocess, "run", failing):
            self.assertEqual(self.t.main(["send", "--state", self.state]), 0)


class WorkflowTests(unittest.TestCase):
    def test_users_step_and_owner_guards(self):
        import yaml
        for name, kind in (("send-pipeline.yml", "send"), ("scrape-pipeline.yml", "scrape")):
            with open(os.path.join(ROOT, ".github", "workflows", name)) as f:
                wf = yaml.safe_load(f)
            steps = wf["jobs"][list(wf["jobs"])[0]]["steps"]
            users = [s for s in steps if s.get("name") == "Run the pipeline for invited users"][0]
            self.assertEqual(users["if"], "always()")
            self.assertIn("tenants.py", users["run"])
            for blank in ("SENDER_EMAIL", "GMAIL_APP_PASSWORD", "EXA_API_KEY", "APIFY_API_TOKEN", "APPLICANT_EMAIL"):
                self.assertEqual(users["env"][blank], "", f"{name}: {blank} must be blanked for users' runs")
            for s in steps:
                if "outreach_pipeline.py" in (s.get("run") or "") or "followup_pipeline.py" in (s.get("run") or ""):
                    self.assertIn("!inputs.tenant", s.get("if", ""), f"{name}: {s['name']} must skip a user-only run")
            on = wf.get("on") or wf.get(True)
            self.assertIn("tenant", on["workflow_dispatch"]["inputs"])
            push = [s for s in steps if s.get("name", "").startswith("Push updated state")][0]
            self.assertIn("git add tenants", push["run"])


if __name__ == "__main__":
    unittest.main()
