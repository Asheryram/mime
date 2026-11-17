"""
Role-by-company-fit: the scraper tags each company with the role its own page
matches, not just the role of the search that found it.

Run from the repo root:  python3 -m unittest discover tests
"""
import importlib
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("FLASK_SECRET_KEY", "test")

import role_match  # noqa: E402
from test_roles import InTempDir, FakeStateRepo  # noqa: E402

with open(os.path.join(ROOT, "webapp", "tracks_default.json"), encoding="utf-8") as f:
    TRACKS = json.load(f)

FINTECH = """PayFlow is a Ghanaian fintech making mobile money payments simple for merchants.
Our product engineering team ships a SaaS dashboard built with React and TypeScript
on a Node.js backend. Join us."""
CLOUD_CONSULTANCY = """We help African enterprises with cloud migration to AWS and Azure.
Our engineers design Kubernetes platforms with Terraform and CI/CD pipelines,
and provide managed cloud and observability for production workloads."""
IT_SERVICES = """Your trusted partner for managed IT services in Accra: helpdesk support,
structured cabling, CCTV installation, firewall configuration and Microsoft 365 setup."""
BANK = """Open a savings account today. Personal and business banking, loans, mortgages
and investment products across 40 branches in Ghana."""


class PickRoleTests(unittest.TestCase):
    def pick(self, text, fallback="devops"):
        role, reason, clear = role_match.pick_role(text, TRACKS, fallback=fallback)
        self.last_clear = clear
        return role, reason

    def test_company_gets_the_role_its_site_describes(self):
        # IT services firms now get the DevOps pitch: sysadmin was merged into it.
        cases = [(FINTECH, "software"), (CLOUD_CONSULTANCY, "devops"), (IT_SERVICES, "devops")]
        for text, expected in cases:
            with self.subTest(text=text[:20]):
                role, reason = self.pick(text, fallback="guess")
                self.assertEqual(role, expected, reason)
                self.assertTrue(self.last_clear)
                self.assertTrue(reason.startswith("site mentions"), reason)

    def test_no_signal_falls_back_and_says_so(self):
        role, reason = self.pick(BANK, fallback="devops")
        self.assertEqual(role, "devops")
        self.assertFalse(self.last_clear, "no signal must go to a human, not be sent on a guess")
        self.assertIn("no clear signal", reason)

    def test_one_keyword_repeated_is_not_enough(self):
        role, reason = self.pick("React. " * 50, fallback="devops")
        self.assertEqual(role, "devops")
        self.assertFalse(self.last_clear)
        self.assertIn("weak signal", reason)

    def test_tie_falls_back(self):
        role, reason = self.pick("We use React, TypeScript, Kubernetes and Terraform.", fallback="guess")
        self.assertEqual(role, "guess")
        self.assertFalse(self.last_clear)
        self.assertIn("equally", reason)

    def test_no_partial_word_matches(self):
        # "laws" contains "aws", "reaction" contains "react", "dockers" isn't "docker".
        hits = role_match.score("New laws drew a strong reaction from dockers and javascripts.", TRACKS)
        self.assertEqual(hits, {})

    def test_punctuated_keywords_match(self):
        hits = role_match.score("Pipelines (CI/CD) and Node.js services.", TRACKS)
        self.assertIn("ci/cd", hits["devops"])
        self.assertIn("node.js", hits["software"])

    def test_role_without_keywords_is_never_picked(self):
        tracks = {k: dict(v) for k, v in TRACKS.items()}
        tracks["software"]["keywords"] = []
        role, _, clear = role_match.pick_role(FINTECH, tracks, fallback="devops")
        self.assertEqual((role, clear), ("devops", False))


class FakeExa:
    def __init__(self, pages):
        self.pages = pages

    def search_companies(self, query, num_results=10):
        return self.pages

    def get_company_domains(self, results):
        return [{"name": p["title"], "url": p["url"], "domain": p["url"], "text": p["text"]} for p in results]


class ScrapeTaggingTests(InTempDir):
    def test_search_tags_each_company_by_its_own_site(self):
        _, op, _ = self.reload_pipeline()
        op.exa = FakeExa([
            {"title": "PayFlow", "url": "https://payflow.com", "text": FINTECH},
            {"title": "Nimbus Cloud", "url": "https://nimbuscloud.com", "text": CLOUD_CONSULTANCY},
            {"title": "Savings Bank", "url": "https://savingsbank.com", "text": BANK},
        ])
        # A DevOps-flavoured search still sends the React fintech the full-stack pitch.
        op.run_exa_search("tech companies Accra", 3, track="devops")
        rows = {r["company_name"]: r for r in op.load_queue()}
        self.assertEqual(rows["PayFlow"]["track"], "software")
        self.assertEqual(rows["Nimbus Cloud"]["track"], "devops")
        self.assertEqual(rows["Savings Bank"]["track"], "devops", "pre-selected guess")
        self.assertIn("this search's role", rows["Savings Bank"]["role_reason"])
        self.assertIn("react", rows["PayFlow"]["role_reason"])
        self.assertEqual(rows["PayFlow"]["role_review"], "")
        self.assertEqual(rows["Savings Bank"]["role_review"], "yes")

    def test_unclear_role_is_never_approved_or_sent_until_chosen(self):
        applicant, op, _ = self.reload_pipeline()
        from test_roles import FakeSMTP, write_queue, write_file
        import smtp_client
        fake = FakeSMTP(); fake.install(smtp_client)
        op.time.sleep = lambda s: None
        write_file(applicant.cv_path_for("devops"), b"%PDF-devops")
        write_queue(op, [
            {"company_name": "Unsure Co", "email": "a@unsure.co", "status": "Pending", "track": "devops",
             "role_review": "yes", "recipient_name": "The Team"},
            {"company_name": "Unsure Approved", "email": "a@ua.co", "status": "Approved", "track": "devops",
             "role_review": "yes", "recipient_name": "The Team"},
            {"company_name": "Sure Co", "email": "a@sure.co", "status": "Pending", "track": "devops",
             "recipient_name": "The Team"},
        ])
        op.run_auto_approve()
        status = {r["company_name"]: r["status"] for r in op.load_queue()}
        self.assertEqual(status["Unsure Co"], "Pending", "auto-approve must skip it")
        op.run_send_emails()
        self.assertEqual([to for to, _ in fake.sent], ["a@sure.co"])

        # The review email lists it, once.
        sent_before = len(fake.sent)
        op.run_notify_needs_review()
        body = fake.sent[sent_before][1].get_content()
        self.assertIn("Unsure Co", body)
        self.assertIn("choose a role", body)

    def test_untagged_search_falls_back_to_default_role(self):
        applicant, op, _ = self.reload_pipeline()
        op.exa = FakeExa([{"title": "Savings Bank", "url": "https://savingsbank.com", "text": BANK}])
        op.run_exa_search("banks Ghana", 1)
        row = op.load_queue()[0]
        self.assertEqual(row["track"], applicant.DEFAULT_TRACK)
        self.assertIn("the default role", row["role_reason"])
        self.assertEqual(row["role_review"], "yes")

    def test_tracks_json_saved_before_keywords_gets_defaults(self):
        applicant, _, _ = self.reload_pipeline()
        old = {k: {f: v for f, v in t.items() if f != "keywords"} for k, t in applicant.TRACKS.items()}
        old["platform"] = dict(old["devops"], cv_path="cv_platform.pdf")
        with open("tracks.json", "w") as f:
            json.dump(old, f)
        applicant, _, _ = self.reload_pipeline()
        self.assertIn("react", applicant.TRACKS["software"]["keywords"])
        self.assertEqual(applicant.TRACKS["platform"]["keywords"], [], "custom roles don't borrow keywords")

    def test_bad_keywords_in_tracks_json_fail_loudly(self):
        applicant, _, _ = self.reload_pipeline()
        bad = {k: dict(v) for k, v in applicant.TRACKS.items()}
        bad["devops"]["keywords"] = "kubernetes, docker"  # a string, not a list
        with open("tracks.json", "w") as f:
            json.dump(bad, f)
        with self.assertRaises(ValueError):
            applicant.load_tracks()


class DashboardKeywordTests(unittest.TestCase):
    def setUp(self):
        import state_repo, app as webapp
        importlib.reload(state_repo); importlib.reload(webapp)
        self.fake = FakeStateRepo(state_repo)
        self.fake.files["outreach_queue.csv"] = (
            ",".join(state_repo.QUEUE_FIELDS) + "\n"
            + "PayFlow,https://payflow.com,a@payflow.com,fintech,The Team,,Approved,,,,,software,,,,,site mentions react,\n"
            + "Unsure Co,https://unsure.co,a@unsure.co,general_startup,The Team,,Pending,,,,,devops,,,,,no clear signal on site; pre-selected the default role,yes\n"
        ).encode()
        self.c = webapp.app.test_client()
        with self.c.session_transaction() as s:
            s["authed"] = True

    def test_keywords_editable_and_cleaned(self):
        page = self.c.get("/roles/software").data.decode()
        self.assertIn("react, next.js", page, "existing keywords pre-filled")
        import state_repo
        role = state_repo.DEFAULT_TRACKS["software"]
        form = {f: role[f] for f in ["role_title", "headline", "subject_area", "interest_area",
                                     "credentials", "closing_skills", "followup_expertise"]}
        form.update({
            "highlights": "\n".join(role["highlights"]),
            "keywords": "React,  Vue\nreact, Svelte ,",
            "letter_intro": role["letter"]["intro"], "letter_closing": role["letter"]["closing"],
            "bullet_title_1": "A", "bullet_text_1": "a", "bullet_title_2": "B", "bullet_text_2": "b",
        })
        r = self.c.post("/roles/software", data=form)
        self.assertEqual(r.status_code, 302)
        saved = json.loads(self.fake.files["tracks.json"])
        self.assertEqual(saved["software"]["keywords"], ["react", "vue", "svelte"])

    def test_old_tracks_json_shows_default_keywords(self):
        import state_repo
        old = {k: {f: v for f, v in t.items() if f != "keywords"} for k, t in state_repo.DEFAULT_TRACKS.items()}
        self.fake.files["tracks.json"] = json.dumps(old).encode()
        self.assertIn("react, next.js", self.c.get("/roles/software").data.decode())

    def test_reason_shown_and_manual_change_recorded(self):
        page = self.c.get("/companies/payflow").data.decode()
        self.assertIn("Why: site mentions react", page)
        self.assertIn("Why: site mentions react", self.c.get("/upcoming").data.decode())
        self.c.post("/companies/payflow", data={
            "company_name": "PayFlow", "email": "a@payflow.com", "website": "https://payflow.com",
            "company_type": "fintech", "track": "devops", "status": "Approved",
            "recipient_name": "The Team",
        })
        self.assertIn(b",devops,", self.fake.files["outreach_queue.csv"])
        self.assertIn(b"set by you", self.fake.files["outreach_queue.csv"])


class DashboardChooseRoleTests(DashboardKeywordTests):
    def queue(self):
        import state_repo
        return {r["company_name"]: r for r in state_repo._parse_queue(self.fake.files["outreach_queue.csv"].decode())}

    def test_unclear_role_waits_in_needs_you_with_a_picker(self):
        page = self.c.get("/attention").data.decode()
        self.assertIn('"reason": "role"', page)
        self.assertIn("no clear signal on site", page)
        # (Every page embeds all names for the search palette, so look for the table row's link.)
        self.assertNotIn('href="/companies/unsure-co"', self.c.get("/upcoming").data.decode(), "not queued to send")

    def test_choosing_a_role_requeues_it(self):
        r = self.c.post("/companies/unsure-co/resolve", data={"reason": "role", "track": "software"},
                        headers={"Accept": "application/json"})
        self.assertTrue(r.get_json()["ok"], r.get_json())
        row = self.queue()["Unsure Co"]
        self.assertEqual((row["track"], row["role_review"], row["role_reason"]), ("software", "", "set by you"))
        self.assertIn('href="/companies/unsure-co"', self.c.get("/upcoming").data.decode())

    def test_unknown_role_rejected(self):
        r = self.c.post("/companies/unsure-co/resolve", data={"reason": "role", "track": "nope"},
                        headers={"Accept": "application/json"})
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self.queue()["Unsure Co"]["role_review"], "yes")

    def test_saving_company_page_counts_as_choosing(self):
        self.c.post("/companies/unsure-co", data={
            "company_name": "Unsure Co", "email": "a@unsure.co", "website": "https://unsure.co",
            "company_type": "general_startup", "track": "devops", "status": "Pending", "recipient_name": "The Team",
        })
        row = self.queue()["Unsure Co"]
        self.assertEqual((row["track"], row["role_review"], row["role_reason"]), ("devops", "", "set by you"))


if __name__ == "__main__":
    unittest.main()
