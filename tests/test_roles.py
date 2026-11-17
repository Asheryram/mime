"""
Per-role (track) CVs and dashboard-managed roles.

Run from the repo root:  python3 -m unittest discover tests
No network and no real email: SMTP and the GitHub-backed state repo are faked.
"""
import csv
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from email import message_from_bytes, policy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("SENDER_EMAIL", "me@example.com")
os.environ.setdefault("GMAIL_APP_PASSWORD", "x" * 16)
os.environ.setdefault("FLASK_SECRET_KEY", "test")

PDF = b"%PDF-1.4\n%fake\n"


class InTempDir(unittest.TestCase):
    """Each test runs in an empty working dir, like a fresh CI checkout."""

    def setUp(self):
        self._cwd = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.dir)

    def reload_pipeline(self):
        """applicant reads tracks.json at import, so reload after changing files."""
        import applicant, cover_letter_generator, email_sender, outreach_pipeline, followup_pipeline
        for m in (applicant, cover_letter_generator, email_sender, outreach_pipeline, followup_pipeline):
            importlib.reload(m)
        return applicant, outreach_pipeline, followup_pipeline


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def install(self, smtp_client):
        class Conn:
            def quit(self):
                pass
        smtp_client.connect = lambda: (Conn(), None)
        smtp_client.send = lambda msg, to: self.sent.append((to, msg))


def write_file(path, data):
    with open(path, "wb") as f:
        f.write(data)


def write_queue(op, rows):
    with open(op.QUEUE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=op.QUEUE_FIELDS)
        w.writeheader()
        for r in rows:
            full = {k: "" for k in op.QUEUE_FIELDS}
            full.update(r)
            w.writerow(full)


class TracksFileTests(InTempDir):
    def test_defaults_used_without_tracks_json(self):
        applicant, _, _ = self.reload_pipeline()
        self.assertEqual(set(applicant.TRACKS), {"devops", "software", "sysadmin"})
        self.assertEqual(applicant.DEFAULT_TRACK, "devops")

    def test_tracks_json_overrides_defaults(self):
        applicant, _, _ = self.reload_pipeline()
        tracks = json.loads(json.dumps(applicant.TRACKS))
        tracks["platform"] = dict(tracks["devops"], cv_path="cv_platform.pdf", role_title="Platform Engineer")
        with open("tracks.json", "w") as f:
            json.dump(tracks, f)
        applicant, _, _ = self.reload_pipeline()
        self.assertIn("platform", applicant.TRACKS)
        self.assertEqual(applicant.cv_path_for("platform"), "cv_platform.pdf")

    def test_malformed_tracks_json_fails_loudly(self):
        applicant, _, _ = self.reload_pipeline()
        bad_cases = [
            {"x": {}},                                                       # missing fields
            {"Bad Key": dict(applicant.TRACKS["devops"])},                   # key with space
            {"ok": dict(applicant.TRACKS["devops"], cv_path="../etc/x.pdf")},  # path traversal
            {"ok": dict(applicant.TRACKS["devops"], letter={"intro": "hi"})},  # letter w/o closing
        ]
        for case in bad_cases:
            with self.subTest(case=list(case)):
                with open("tracks.json", "w") as f:
                    json.dump(case, f)
                with self.assertRaises(ValueError):
                    applicant.load_tracks()

    def test_hold_reasons(self):
        applicant, _, _ = self.reload_pipeline()
        self.assertIn("no CV", applicant.track_hold_reason("software"))
        self.assertIn("unknown", applicant.track_hold_reason("nope"))
        write_file(applicant.cv_path_for("software"), PDF)
        self.assertIsNone(applicant.track_hold_reason("software"))
        applicant.TRACKS["software"]["active"] = False
        self.assertIn("switched off", applicant.track_hold_reason("software"))
        # Unknown tracks never borrow the default CV.
        self.assertIsNone(applicant.cv_path_for("nope"))


class SendTests(InTempDir):
    def test_each_role_gets_its_own_cv_and_missing_cv_is_held(self):
        applicant, op, _ = self.reload_pipeline()
        import smtp_client
        fake = FakeSMTP(); fake.install(smtp_client)
        op.time.sleep = lambda s: None

        write_file(applicant.cv_path_for("devops"), b"%PDF-devops")
        write_file(applicant.cv_path_for("sysadmin"), b"%PDF-sysadmin")
        # software CV deliberately missing
        write_queue(op, [
            {"company_name": "Soft Co", "email": "a@soft.co", "status": "Approved", "track": "software",
             "company_type": "general_startup", "recipient_name": "The Team"},
            {"company_name": "Dev Co", "email": "a@dev.co", "status": "Approved", "track": "devops",
             "company_type": "fintech", "recipient_name": "The Team"},
            {"company_name": "Sys Co", "email": "a@sys.co", "status": "Approved", "track": "sysadmin",
             "company_type": "bank", "recipient_name": "The Team"},
        ])
        op.run_send_emails()

        by_rcpt = {to: msg for to, msg in fake.sent}
        self.assertEqual(set(by_rcpt), {"a@dev.co", "a@sys.co"})
        for to, expected_cv in [("a@dev.co", b"%PDF-devops"), ("a@sys.co", b"%PDF-sysadmin")]:
            msg = message_from_bytes(by_rcpt[to].as_bytes(), policy=policy.default)
            atts = {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}
            self.assertIn(applicant.CV_ATTACHMENT_NAME, atts, "CV goes out under the neutral name")
            self.assertEqual(atts[applicant.CV_ATTACHMENT_NAME], expected_cv)
            self.assertFalse(any("cv_" in n for n in atts), "internal per-role filename must not leak")
        self.assertIn("IT and Systems Administration", by_rcpt["a@sys.co"]["Subject"])

        status = {r["company_name"]: r["status"] for r in op.load_queue()}
        self.assertEqual(status, {"Soft Co": "Approved", "Dev Co": "Sent", "Sys Co": "Sent"})

    def test_held_rows_dont_use_up_the_daily_cap(self):
        applicant, op, _ = self.reload_pipeline()
        import smtp_client
        fake = FakeSMTP(); fake.install(smtp_client)
        op.time.sleep = lambda s: None
        op.DAILY_SEND_LIMIT = 2
        write_file(applicant.cv_path_for("devops"), PDF)
        rows = [{"company_name": f"Soft {i}", "email": f"s{i}@x.co", "status": "Approved", "track": "software",
                 "recipient_name": "The Team"} for i in range(5)]
        rows += [{"company_name": f"Dev {i}", "email": f"d{i}@x.co", "status": "Approved", "track": "devops",
                  "recipient_name": "The Team"} for i in range(3)]
        write_queue(op, rows)
        op.run_send_emails()
        self.assertEqual([to for to, _ in fake.sent], ["d0@x.co", "d1@x.co"])

    def test_followup_reattaches_same_role_cv_and_holds_missing(self):
        applicant, op, fp = self.reload_pipeline()
        import smtp_client
        fake = FakeSMTP(); fake.install(smtp_client)
        fp.time.sleep = lambda s: None
        write_file(applicant.cv_path_for("sysadmin"), b"%PDF-sysadmin")
        write_queue(op, [
            {"company_name": "Sys Co", "email": "a@sys.co", "status": "Sent", "track": "sysadmin",
             "date_sent": "2020-01-01 09:00:00", "message_id": "<m1@x>", "recipient_name": "The Team"},
            {"company_name": "Soft Co", "email": "a@soft.co", "status": "Sent", "track": "software",
             "date_sent": "2020-01-01 09:00:00", "message_id": "<m2@x>", "recipient_name": "The Team"},
        ])
        fp.run_followups(dry_run=False)
        self.assertEqual([to for to, _ in fake.sent], ["a@sys.co"])
        msg = message_from_bytes(fake.sent[0][1].as_bytes(), policy=policy.default)
        atts = {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}
        self.assertEqual(atts, {applicant.CV_ATTACHMENT_NAME: b"%PDF-sysadmin"})

    def test_search_queries_carry_a_role(self):
        _, op, _ = self.reload_pipeline()
        with open("search_queries.txt", "w") as f:
            f.write("# c\nfintech Accra | 5 | software\nbanks Ghana | 7\nisps | 3 | nonsense\n")
        self.assertEqual(op.load_search_queries(),
                         [("fintech Accra", 5, "software"), ("banks Ghana", 7, None)])


class FakeStateRepo:
    """In-memory stand-in for the GitHub contents API used by state_repo."""

    def __init__(self, state_repo):
        self.files = {}
        state_repo._get_file_bytes = self.get
        state_repo._put_file_bytes = self.put
        state_repo.cv_size = lambda p: len(self.files[p]) if p in self.files else None
        state_repo.latest_run = lambda wf: None

    def get(self, path):
        return (self.files[path], "sha") if path in self.files else (None, None)

    def put(self, path, data, sha, message):
        self.files[path] = data
        return "sha"


class DashboardTests(unittest.TestCase):
    def setUp(self):
        import state_repo, app as webapp
        importlib.reload(state_repo); importlib.reload(webapp)
        self.fake = FakeStateRepo(state_repo)
        self.fake.files["outreach_queue.csv"] = (
            ",".join(state_repo.QUEUE_FIELDS) + "\n"
            + "Dev Co,https://dev.co,a@dev.co,fintech,The Team,,Approved,,,,,devops,,,,\n"
            + "Full Co,https://full.co,a@full.co,general_startup,The Team,,Approved,,,,,fullstack,,,,\n"
        ).encode()
        self.state_repo = state_repo
        self.app = webapp
        self.c = webapp.app.test_client()
        with self.c.session_transaction() as s:
            s["authed"] = True

    def tracks(self):
        return json.loads(self.fake.files["tracks.json"])

    def role_form(self, **over):
        form = {
            "key": "fullstack", "role_title": "Full-Stack Engineer", "headline": "Full-Stack Engineer",
            "subject_area": "Full-Stack Engineering", "interest_area": "full-stack engineering",
            "credentials": "a CS graduate who builds web products", "closing_skills": "product focus",
            "followup_expertise": "React and NestJS",
            "highlights": "- Built X with Y\n- Shipped Z\n",
            "letter_intro": "I want the {role} role at {company}.", "letter_lead_in": "",
            "bullet_title_1": "Frontend", "bullet_text_1": "React <app> & things",
            "bullet_title_2": "Backend", "bullet_text_2": "NestJS",
            "letter_closing": "Thanks, {company}.",
        }
        form.update(over)
        return form

    def test_pages_render(self):
        for url in ["/roles", "/roles/new", "/roles/devops", "/roles/software", "/settings", "/upcoming",
                    "/companies/new"]:
            with self.subTest(url=url):
                r = self.c.get(url)
                self.assertEqual(r.status_code, 200, r.data[:500])

    def test_add_role_starts_off_and_needs_cv_to_turn_on(self):
        r = self.c.post("/roles/new", data=self.role_form())
        self.assertEqual(r.status_code, 302)
        t = self.tracks()
        self.assertEqual(set(t), {"devops", "software", "sysadmin", "fullstack"}, "defaults copied on first save")
        self.assertFalse(t["fullstack"]["active"])
        self.assertEqual(t["fullstack"]["cv_path"], "cv_fullstack.pdf")
        self.assertEqual(t["fullstack"]["highlights"], ["Built X with Y", "Shipped Z"])

        # Up next shows the fullstack company as held, not "goes out today".
        page = self.c.get("/upcoming").data.decode()
        self.assertIn("held: role is off", page)

        self.c.post("/roles/fullstack/active", data={"active": "on"})
        self.assertFalse(self.tracks()["fullstack"]["active"], "can't turn on without a CV")

        self.c.post("/roles/fullstack/cv", data={"cv": (io.BytesIO(PDF), "mine.pdf", "application/pdf")},
                    content_type="multipart/form-data")
        self.assertEqual(self.fake.files["cv_fullstack.pdf"], PDF)
        self.c.post("/roles/fullstack/active", data={"active": "on"})
        self.assertTrue(self.tracks()["fullstack"]["active"])

        # The pipeline accepts what the dashboard wrote.
        import applicant
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tracks.json")
            write_file(p, self.fake.files["tracks.json"])
            loaded = applicant.load_tracks(p)
        self.assertEqual(loaded["fullstack"]["letter"]["bullets"][0]["text"], "React <app> & things")

    def test_role_form_rejects_bad_input(self):
        cases = [
            self.role_form(key="Full Stack"),
            self.role_form(key="devops"),                          # duplicate
            self.role_form(highlights="only one"),
            self.role_form(bullet_text_2=""),                      # title without text
            self.role_form(letter_intro="", letter_closing="", bullet_title_1="", bullet_text_1="",
                           bullet_title_2="", bullet_text_2=""),   # new role with no letter
        ]
        for form in cases:
            with self.subTest(key=form["key"]):
                self.c.post("/roles/new", data=form)
                self.assertNotIn("Full Stack", self.fake.files.get("tracks.json", b"{}").decode())
        self.assertNotIn("tracks.json", self.fake.files)

    def test_devops_can_keep_sector_templates(self):
        form = self.role_form(letter_intro="", letter_closing="", bullet_title_1="", bullet_text_1="",
                              bullet_title_2="", bullet_text_2="")
        form.pop("key")
        r = self.c.post("/roles/devops", data=form)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.tracks()["devops"]["letter"])

    def test_query_roles_validated(self):
        self.c.post("/settings/queries", data={"queries": "fintech | 5 | nonsense"})
        self.assertNotIn("search_queries.txt", self.fake.files)
        self.c.post("/settings/queries", data={"queries": "fintech | 5 | software\nbanks"})
        self.assertIn(b"| software", self.fake.files["search_queries.txt"])

    def test_preview_matches_the_real_email(self):
        import applicant
        for key, role in self.state_repo.DEFAULT_TRACKS.items():
            with self.subTest(role=key):
                preview = self.app.role_preview(role, company="Acme Ltd")
                real = applicant.build_outreach_body("Acme Ltd", key)
                real = real.replace(applicant.FULL_NAME, "<your name>")
                self.assertTrue(real.startswith(preview["body"]), "preview drifted from build_outreach_body")
                self.assertEqual(preview["subject"],
                                 applicant.subject_for(key).replace(applicant.FULL_NAME, "<your name>"))


if __name__ == "__main__":
    unittest.main()
