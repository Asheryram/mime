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
        return role_match.pick_role(text, TRACKS, fallback=fallback)

    def test_company_gets_the_role_its_site_describes(self):
        cases = [(FINTECH, "software"), (CLOUD_CONSULTANCY, "devops"), (IT_SERVICES, "sysadmin")]
        for text, expected in cases:
            with self.subTest(expected=expected):
                role, reason = self.pick(text, fallback="sysadmin" if expected != "sysadmin" else "devops")
                self.assertEqual(role, expected, reason)
                self.assertTrue(reason.startswith("site mentions"), reason)

    def test_no_signal_falls_back_and_says_so(self):
        role, reason = self.pick(BANK, fallback="devops")
        self.assertEqual(role, "devops")
        self.assertIn("no clear signal", reason)

    def test_one_keyword_repeated_is_not_enough(self):
        role, _ = self.pick("React. " * 50, fallback="devops")
        self.assertEqual(role, "devops")

    def test_tie_falls_back(self):
        role, reason = self.pick("We use React, TypeScript, Kubernetes and Terraform.", fallback="sysadmin")
        self.assertEqual(role, "sysadmin")
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
        role, _ = role_match.pick_role(FINTECH, tracks, fallback="devops")
        self.assertEqual(role, "devops")


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
        self.assertEqual(rows["Savings Bank"]["track"], "devops")
        self.assertIn("this search's role", rows["Savings Bank"]["role_reason"])
        self.assertIn("react", rows["PayFlow"]["role_reason"])

    def test_untagged_search_falls_back_to_default_role(self):
        applicant, op, _ = self.reload_pipeline()
        op.exa = FakeExa([{"title": "Savings Bank", "url": "https://savingsbank.com", "text": BANK}])
        op.run_exa_search("banks Ghana", 1)
        row = op.load_queue()[0]
        self.assertEqual(row["track"], applicant.DEFAULT_TRACK)
        self.assertIn("the default role", row["role_reason"])

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
            + "PayFlow,https://payflow.com,a@payflow.com,fintech,The Team,,Approved,,,,,software,,,,,site mentions react\n"
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


if __name__ == "__main__":
    unittest.main()
