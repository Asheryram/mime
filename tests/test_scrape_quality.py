"""
Scrape quality: right email for the right row, no vendor or aggregator
addresses, rotating searches, a usable review email, and morning sends.

Run from the repo root:  python3 -m unittest discover tests
"""
import importlib
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("FLASK_SECRET_KEY", "test")

from test_roles import InTempDir, FakeStateRepo, FakeSMTP, write_queue  # noqa: E402


class EmailChoiceTests(unittest.TestCase):
    def setUp(self):
        import outreach_pipeline as op
        self.op = op

    def test_fits_site(self):
        cases = [
            ("contact@manatal.com", "https://paytechnexus.com", False),   # ATS vendor
            ("x@agency.com", "https://acme.com", False),                  # someone else
            ("hr@acme.com", "https://www.acme.com", True),
            ("jobs@careers.acme.com", "https://acme.com", True),          # subdomain
            ("hr@hubtel.com", "https://hubtel.com.gh", True),             # same name, other TLD
            ("info@hubtel.com.gh", "https://www.hubtel.com.gh", True),
            ("acme.gh@gmail.com", "https://acme.com", True),              # small firms use these
            ("info@manatal.com", "https://manatal.com", False),           # an ATS is never an employer here
        ]
        for email, site, ok in cases:
            with self.subTest(email=email, site=site):
                self.assertEqual(self.op.email_fits_site(email, site), ok)

    def test_choose_prefers_own_domain_then_hiring_inbox(self):
        email, note = self.op.choose_email(
            ["acme@gmail.com", "info@acme.com", "careers@acme.com", "x@vendor.io", "logo@2x.png"],
            "https://acme.com")
        self.assertEqual((email, note), ("careers@acme.com", ""))

    def test_choose_rejects_vendor_only_with_a_note(self):
        email, note = self.op.choose_email(["contact@manatal.com"], "https://paytechnexus.com")
        self.assertEqual(email, "")
        self.assertIn("contact@manatal.com", note)

    def test_job_boards_and_ats_hosts_skipped(self):
        for url in ["https://gh.expertini.com", "https://paytech.careers-page.com",
                    "https://apply.workable.com/acme", "https://jobs.lever.co/acme"]:
            with self.subTest(url=url):
                self.assertTrue(self.op.is_job_board(url))
        self.assertFalse(self.op.is_job_board("https://hubtel.com"))


class FakeApify:
    def __init__(self, items):
        self.items = items
        self.urls = None

    def run_contact_scraper(self, urls, max_pages=8, depth=1):
        self.urls = urls
        return self.items


class ScrapeMappingTests(InTempDir):
    def test_email_goes_to_the_row_being_scraped_not_an_older_one(self):
        # The bug: an older row on the same domain was matched first, so the
        # pending row was never filled in and got re-crawled every run.
        _, op, _ = self.reload_pipeline()
        write_queue(op, [
            {"company_name": "Acme (old)", "website": "https://acme.com", "email": "old@acme.com", "status": "Sent"},
            {"company_name": "Acme", "website": "https://www.acme.com", "email": "", "status": "Pending"},
        ])
        op.get_apify = lambda: FakeApify([{"domain": "acme.com", "emails": ["careers@acme.com"]}])
        op.run_apify_scrape()
        rows = {r["company_name"]: r for r in op.load_queue()}
        self.assertEqual(rows["Acme"]["email"], "careers@acme.com")
        self.assertEqual(rows["Acme (old)"]["email"], "old@acme.com", "older row untouched")

    def test_vendor_only_address_goes_to_needs_you_with_evidence(self):
        _, op, _ = self.reload_pipeline()
        write_queue(op, [{"company_name": "PayTech Nexus Ltd", "website": "https://paytechnexus.com",
                          "email": "", "status": "Pending"}])
        op.get_apify = lambda: FakeApify([{"domain": "paytechnexus.com", "emails": ["contact@manatal.com"]}])
        op.run_apify_scrape()
        row = op.load_queue()[0]
        self.assertEqual((row["status"], row["email"]), ("Skipped", "no_email_found"))
        self.assertIn("contact@manatal.com", row["email_note"])
        op.run_apify_scrape()  # nothing left to crawl: not re-crawled every run
        self.assertEqual(op.load_queue()[0]["status"], "Skipped")

    def test_emails_from_several_pages_are_pooled(self):
        _, op, _ = self.reload_pipeline()
        write_queue(op, [{"company_name": "Acme", "website": "https://acme.com", "email": "", "status": "Pending"}])
        op.get_apify = lambda: FakeApify([
            {"domain": "acme.com", "emails": ["x@vendor.io"]},
            {"url": "https://acme.com/careers", "emails": ["jobs@acme.com"]},
        ])
        op.run_apify_scrape()
        self.assertEqual(op.load_queue()[0]["email"], "jobs@acme.com")


class SearchRotationTests(unittest.TestCase):
    def setUp(self):
        import outreach_pipeline as op
        self.op = op

    def test_defaults_parse_and_every_role_is_real(self):
        import applicant
        qs = self.op._parse_search_queries(self.op.DEFAULT_SEARCH_QUERIES_FILE)
        self.assertGreaterEqual(len(qs), 12)
        self.assertTrue(all(t in applicant.TRACKS for _, _, t in qs))
        self.assertTrue(any(t == "software" for _, _, t in qs) and any(t == "devops" for _, _, t in qs))
        self.assertEqual(len({q for q, _, _ in qs}), len(qs), "no duplicate searches")

    def test_rotation_covers_everything_and_moves_each_run(self):
        qs = [(f"q{i}", 10, None) for i in range(19)]
        t0 = datetime(2026, 11, 19, 0, 17, tzinfo=timezone.utc)
        seen, previous = set(), None
        for k in range(5):  # ceil(19 / 4) runs
            batch = self.op.queries_for_this_run(qs, now=t0 + timedelta(hours=6 * k), per_run=4)
            self.assertEqual(len(batch), 4)
            self.assertNotEqual(batch, previous)
            previous = batch
            seen.update(q for q, _, _ in batch)
        self.assertEqual(len(seen), 19)

    def test_default_volume(self):
        import app as webapp
        self.assertEqual(self.op.QUERIES_PER_RUN, 2)
        self.assertEqual(webapp.SEARCHES_PER_RUN, self.op.QUERIES_PER_RUN, "dashboard text must match")
        self.assertEqual(self.op.EMAIL_LOOKUPS_PER_RUN, 10)

    def test_no_bank_or_telecom_searches(self):
        with open(self.op.DEFAULT_SEARCH_QUERIES_FILE) as f:
            lines = [l.lower() for l in f if l.strip() and not l.startswith("#")]
        for word in ("bank", "telecom", "internet service provider", "insurer"):
            self.assertFalse(any(word in l for l in lines), word)

    def test_short_lists_run_whole(self):
        qs = [("a", 10, None), ("b", 10, None)]
        self.assertEqual(self.op.queries_for_this_run(qs), qs)

    def test_saved_file_wins_over_defaults(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("my search | 5 | software\n")
        try:
            self.assertEqual(self.op.load_search_queries(f.name), [("my search", 5, "software")])
        finally:
            os.unlink(f.name)
        self.assertGreaterEqual(len(self.op.load_search_queries("/nonexistent.txt")), 12)


class ExaFallbackTests(unittest.TestCase):
    def test_company_search_then_plain_on_400(self):
        import exa_search
        calls = []

        def fake_post(url, json=None, headers=None):
            calls.append(dict(json))
            r = mock.Mock()
            r.status_code = 400 if "category" in json else 200
            r.text = "category not supported"
            r.json.return_value = {"results": [{"title": "Acme", "url": "https://acme.com", "text": "t"}]}
            return r
        with mock.patch.object(exa_search.requests, "post", fake_post):
            results = exa_search.ExaSearch(api_key="k").search_companies("fintech Ghana", 5) \
                if "api_key" in exa_search.ExaSearch.__init__.__code__.co_varnames \
                else self._search(exa_search)
        self.assertEqual(calls[0]["category"], "company")
        self.assertNotIn("category", calls[1])
        self.assertEqual(len(results), 1)

    def _search(self, exa_search):
        s = exa_search.ExaSearch.__new__(exa_search.ExaSearch)
        s.api_key = "k"
        return s.search_companies("fintech Ghana", 5)


class ReviewEmailTests(unittest.TestCase):
    ROWS = [
        {"company_name": "Unsure Co", "website": "https://unsure.co", "status": "Pending", "track": "devops",
         "role_reason": "site only mentions backend, not enough to be sure; pre-selected this search's role"},
        {"company_name": "PayTech Nexus Ltd", "website": "https://paytechnexus.com", "status": "Skipped",
         "email": "no_email_found", "email_note": "only found contact@manatal.com, not on this company's domain"},
        {"company_name": "<b>Join</b> Our Team", "website": "https://spalce.dev", "status": "Review"},
    ]

    def build(self, url="https://dash.example.com"):
        import review_email
        return review_email.build(self.ROWS, ["devops", "software"], url, earlier_waiting=2)

    def test_subject_says_what_is_needed(self):
        subject, _, _ = self.build()
        self.assertEqual(subject, "3 companies need you: 1 to choose a role, 1 missing an email, 1 with an unclear name")

    def test_fix_links_open_that_company_in_needs_you(self):
        import state_repo
        _, text, html = self.build()
        for r in self.ROWS:
            slug = state_repo.slugify(r["company_name"])
            self.assertIn(f"https://dash.example.com/attention?focus={slug}", html)
            self.assertIn(f"/attention?focus={slug}", text)

    def test_evidence_and_guess_shown_plainly(self):
        _, text, html = self.build()
        self.assertIn("Site only mentions backend, not enough to be sure.", text)
        self.assertIn("Best guess: devops (this search's role)", text)
        self.assertIn("Only found contact@manatal.com", text)
        self.assertIn("Try careers@paytechnexus.com", text)
        self.assertIn("Plus 2 from earlier", text)

    def test_names_are_escaped(self):
        _, _, html = self.build()
        self.assertNotIn("<b>Join</b>", html)
        self.assertIn("&lt;b&gt;Join&lt;/b&gt;", html)

    def test_without_a_dashboard_url_there_are_no_dead_links(self):
        _, text, html = self.build(url="")
        self.assertNotIn("/attention", html + text)
        self.assertIn("Needs you", html)


class NotifyEmailTests(InTempDir):
    def test_notify_sends_html_and_text_and_marks_rows(self):
        _, op, _ = self.reload_pipeline()
        import smtp_client
        fake = FakeSMTP(); fake.install(smtp_client)
        os.environ["DASHBOARD_URL"] = "https://dash.example.com"
        try:
            write_queue(op, [
                {"company_name": "Kraado", "website": "https://kraado.com", "status": "Skipped",
                 "email": "no_email_found"},
                {"company_name": "Old One", "website": "https://old.com", "status": "Review", "review_notified": "yes"},
            ])
            op.run_notify_needs_review()
        finally:
            del os.environ["DASHBOARD_URL"]
        msg = fake.sent[0][1]
        self.assertEqual(msg["Subject"], "1 company needs you: 1 missing an email")
        html = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn("https://dash.example.com/attention?focus=kraado", html)
        self.assertIn("Plus 1 from earlier", msg.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual({r["company_name"]: r["review_notified"] for r in op.load_queue()},
                         {"Kraado": "yes", "Old One": "yes"})


class DashboardTests(unittest.TestCase):
    def setUp(self):
        import state_repo, app as webapp
        importlib.reload(state_repo); importlib.reload(webapp)
        self.app = webapp
        self.fake = FakeStateRepo(state_repo)
        self.fake.files["outreach_queue.csv"] = (",".join(state_repo.QUEUE_FIELDS) + "\n").encode()
        self.c = webapp.app.test_client()

    def test_fix_link_survives_login(self):
        r = self.c.get("/attention?focus=kraado")
        self.assertEqual(r.status_code, 302)
        self.assertIn("next=/attention?focus%3Dkraado", r.headers["Location"].replace("%2F", "/"))
        with mock.patch.object(self.app.db, "load_auth", return_value={"password_hash": "h"}), \
                mock.patch.object(self.app, "check_password_hash", return_value=True):
            r = self.c.post("/login", data={"email": self.app.ALLOWED_EMAIL, "password": "x",
                                            "next": "/attention?focus=kraado"})
        self.assertEqual(r.headers["Location"], "/attention?focus=kraado")

    def test_login_never_redirects_off_site(self):
        with mock.patch.object(self.app.db, "load_auth", return_value={"password_hash": "h"}), \
                mock.patch.object(self.app, "check_password_hash", return_value=True):
            for bad in ["//evil.com", "https://evil.com", "/\\evil.com"]:
                r = self.c.post("/login", data={"email": self.app.ALLOWED_EMAIL, "password": "x", "next": bad})
                self.assertEqual(r.headers["Location"], "/", bad)

    def test_settings_shows_the_shared_default_searches(self):
        import outreach_pipeline as op
        with open(op.DEFAULT_SEARCH_QUERIES_FILE, encoding="utf-8") as f:
            self.assertEqual(self.app.DEFAULT_QUERIES_TEXT, f.read())
        self.app.parse_queries(self.app.DEFAULT_QUERIES_TEXT, ["devops", "software"])  # valid as shipped

    def test_next_send_is_a_weekday_morning(self):
        for start in range(14):
            now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc) + timedelta(hours=12 * start)
            run = self.app.next_send_run(now)
            self.assertLess(run.weekday(), 5)
            self.assertEqual((run.hour, run.minute), (7, 23))
            self.assertGreater(run, now)

    def test_dashboard_schedule_matches_workflows(self):
        with open(os.path.join(ROOT, ".github", "workflows", "send-pipeline.yml")) as f:
            self.assertIn('cron: "23 7 * * 1-5"', f.read())
        with open(os.path.join(ROOT, ".github", "workflows", "scrape-pipeline.yml")) as f:
            self.assertIn('cron: "17 */6 * * *"', f.read())
        self.assertEqual((self.app.SEND_HOUR_UTC, self.app.SEND_MINUTE, self.app.SCRAPE_MINUTE), (7, 23, 17))


if __name__ == "__main__":
    unittest.main()
