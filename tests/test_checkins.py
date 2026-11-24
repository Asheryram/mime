"""
"Not now, maybe later" replies: record the outcome, get reminded on the date
you chose with a drafted check-in, and send it only when you say so.

Run from the repo root:  python3 -m unittest discover tests
"""
import importlib
import os
import sys
import unittest
from datetime import date, datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("FLASK_SECRET_KEY", "test")

from test_roles import InTempDir, FakeStateRepo, FakeSMTP, write_queue, write_file  # noqa: E402
from test_inbox_check import FakeIMAP, mail, ME  # noqa: E402


class TextTests(unittest.TestCase):
    def test_preview_matches_what_is_sent(self):
        import applicant, app as webapp
        for key in applicant.TRACKS:
            for month in ("November 2026", ""):
                with self.subTest(role=key, month=month):
                    real = applicant.build_checkin_body("Nexa Labs", key, month).replace(applicant.FULL_NAME, "<your name>")
                    pv = webapp.checkin_preview(applicant.TRACKS[key], "Nexa Labs", month)
                    self.assertTrue(real.startswith(pv["body"]), "preview drifted from build_checkin_body")
                    self.assertEqual(pv["subject"], applicant.checkin_subject(key).replace(applicant.FULL_NAME, "<your name>"))

    def test_reads_as_a_continuation(self):
        import applicant
        body = applicant.build_checkin_body("Nexa Labs", "software", "November 2026")
        self.assertTrue(body.startswith("Dear Nexa Labs team,\n\nIn November 2026 you kindly replied"))
        self.assertIn("My current CV is attached.", body)
        self.assertEqual(body.count("\n- "), 2)

    def test_add_months_clamps(self):
        import app as webapp
        self.assertEqual(webapp.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(webapp.add_months(date(2026, 11, 23), 3), date(2027, 2, 23))


def later_row(**k):
    r = {"company_name": "Nexa Labs", "website": "https://nexalabs.com", "email": "info@nexalabs.com",
         "status": "Sent", "track": "devops", "replied": "yes", "date_sent": "2026-11-19 07:30:00",
         "outcome": "later", "outcome_date": "2026-11-23", "checkback_date": "2027-02-23"}
    r.update(k)
    return r


class PipelineTests(InTempDir):
    def setUp(self):
        super().setUp()
        self.applicant, self.op, self.fp = self.reload_pipeline()
        import smtp_client
        self.smtp = FakeSMTP(); self.smtp.install(smtp_client)
        self.op.time.sleep = self.fp.time.sleep = lambda s: None
        write_file(self.applicant.cv_path_for("devops"), b"%PDF-devops")

    def test_due_logic(self):
        self.assertFalse(self.op.checkin_due(later_row(), today="2027-02-22"))
        self.assertTrue(self.op.checkin_due(later_row(), today="2027-02-23"))
        self.assertFalse(self.op.checkin_due(later_row(checkback_status="skipped"), today="2027-03-01"))
        self.assertFalse(self.op.checkin_due(later_row(outcome="declined"), today="2027-03-01"))

    def test_nothing_is_sent_without_your_ok(self):
        # Due, but not approved: the daily run, follow-ups and check-in sender all leave it alone.
        write_queue(self.op, [later_row(checkback_date="2020-01-01")])
        self.op.run_send_checkins()
        self.op.run_send_emails()
        self.fp.run_followups(dry_run=False)
        self.assertEqual(self.smtp.sent, [])

    def test_approved_checkin_sends_cv_only(self):
        write_queue(self.op, [later_row(checkback_date="2020-01-01", checkback_status="queued")])
        self.op.run_send_checkins()
        to, msg = self.smtp.sent[0]
        self.assertEqual(to, "info@nexalabs.com")
        self.assertEqual({p.get_filename() for p in msg.iter_attachments()}, {self.applicant.CV_ATTACHMENT_NAME})
        self.assertIn("In November 2026 you kindly replied", msg.get_body(preferencelist=("plain",)).get_content())
        row = self.op.load_queue()[0]
        self.assertEqual(row["checkback_status"], "sent")
        self.assertTrue(row["checkback_message_id"])

    def test_missing_cv_fails_clearly(self):
        write_queue(self.op, [later_row(track="software", checkback_status="queued")])
        self.op.run_send_checkins()
        self.assertEqual(self.smtp.sent, [])
        self.assertIn("no CV", self.op.load_queue()[0]["checkback_note"])

    def test_review_email_reminds_once_with_a_link_to_the_draft(self):
        os.environ["DASHBOARD_URL"] = "https://dash.example.com"
        try:
            write_queue(self.op, [later_row(checkback_date="2020-01-01", review_notified="yes")])
            self.op.run_notify_needs_review()
            self.op.run_notify_needs_review()
        finally:
            del os.environ["DASHBOARD_URL"]
        self.assertEqual(len(self.smtp.sent), 1, "reminded once, not every scrape")
        msg = self.smtp.sent[0][1]
        self.assertEqual(msg["Subject"], "1 company needs you: 1 to check back with")
        html = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn("https://dash.example.com/companies/nexa-labs#reply", html)
        self.assertIn("Time to check back", html)

    def test_reply_to_the_checkin_is_detected(self):
        import inbox_check
        q = [later_row(checkback_status="sent", checkback_message_id="<ci-1@gmail.com>",
                       checkback_sent_date="2027-02-23 08:00:00", checkback_replied="", bounced="")]
        fake = FakeIMAP([mail("Founder <ama@nexalabs.com>", subject="Re: Checking in",
                              date="Wed, 24 Feb 2027 10:00:00 +0000", in_reply_to="<ci-1@gmail.com>")])
        replied, _ = inbox_check.check_inbox(q, ME, "pw", imap_factory=fake)
        self.assertEqual(replied, ["Nexa Labs (check-in)"])
        self.assertEqual(q[0]["checkback_replied"], "yes")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        import state_repo, app as webapp
        importlib.reload(state_repo); importlib.reload(webapp)
        self.sr, self.app = state_repo, webapp
        self.fake = FakeStateRepo(state_repo)
        self.fake.files[state_repo.DEFAULT_TRACKS["devops"]["cv_path"]] = b"%PDF"
        self.set_row(later_row(outcome="", outcome_date="", checkback_date="", replied=""))
        self.dispatched = []
        state_repo.dispatch_workflow = lambda wf, inputs=None: self.dispatched.append((wf, inputs))
        state_repo.pipeline_busy = lambda wfs: self.busy
        self.busy = False
        self.c = webapp.app.test_client()
        with self.c.session_transaction() as s:
            s["authed"] = True
        self.now = mock.patch.object(webapp, "utcnow", return_value=datetime(2026, 11, 23, 10, 0))
        self.now.start(); self.addCleanup(self.now.stop)

    def set_row(self, r):
        F = self.sr.QUEUE_FIELDS
        full = {f: "" for f in F}; full.update(r)
        self.fake.files["outreach_queue.csv"] = (",".join(F) + "\n" + ",".join(full[f] for f in F) + "\n").encode()

    def row(self):
        return self.sr._parse_queue(self.fake.files["outreach_queue.csv"].decode())[0]

    def post(self, **form):
        return self.c.post("/companies/nexa-labs/reply", data=form, follow_redirects=True)

    def test_record_later_in_three_months(self):
        r = self.post(action="save", outcome="later", checkback_in="3")
        self.assertIn("Tue 23 Feb 2027", r.data.decode())
        row = self.row()
        self.assertEqual((row["outcome"], row["checkback_date"], row["replied"]), ("later", "2027-02-23", "yes"))
        page = self.c.get("/companies/nexa-labs").data.decode()
        self.assertIn("You'll be reminded to check back on", page)
        self.assertNotIn('href="/companies/nexa-labs"', self.c.get("/attention").data.decode().split("var ITEMS")[0])

    def test_pick_a_date(self):
        self.post(action="save", outcome="later", checkback_in="date", checkback_date="2027-01-10")
        self.assertEqual(self.row()["checkback_date"], "2027-01-10")
        r = self.post(action="save", outcome="later", checkback_in="date", checkback_date="2026-11-01")
        self.assertIn("Pick a date after today", r.data.decode())

    def test_due_checkin_shows_draft_and_sends_only_when_pressed(self):
        self.set_row(later_row(checkback_date="2026-11-23"))
        att = self.c.get("/attention").data.decode()
        self.assertIn('"reason": "checkback"', att)
        page = self.c.get("/companies/nexa-labs").data.decode()
        self.assertIn("Time to check back.", page)
        self.assertIn("On 23 Nov 2026 they said", page)
        self.assertNotIn("Don't email this one</button>", page, "an emailed company can't be rejected from here")
        self.assertIn("In November 2026 you kindly replied", page)
        self.assertEqual(self.dispatched, [])
        self.post(action="send_now")
        self.assertEqual(self.row()["checkback_status"], "queued")
        self.assertEqual(self.dispatched, [("send-pipeline.yml", {"mode": "referrals"})])

    def test_snooze_and_skip(self):
        self.set_row(later_row(checkback_date="2026-11-20", checkback_notified="yes"))
        self.post(action="snooze")
        row = self.row()
        self.assertEqual((row["checkback_date"], row["checkback_notified"]), ("2026-12-23", ""))
        self.set_row(later_row(checkback_date="2026-11-20"))
        self.post(action="skip")
        self.assertEqual(self.row()["checkback_status"], "skipped")
        self.assertNotIn('"reason": "checkback"', self.c.get("/attention").data.decode())

    def test_cannot_send_before_it_is_due(self):
        self.set_row(later_row(checkback_date="2027-02-23"))
        r = self.post(action="send_now")
        self.assertIn("no check-in due", r.data.decode())
        self.assertEqual(self.dispatched, [])

    def test_drop_in_needs_you_never_rejects_an_emailed_company(self):
        self.set_row(later_row(checkback_date="2026-11-20"))
        self.c.post("/attention/bulk", json={"action": "drop", "slugs": ["nexa-labs"]})
        row = self.row()
        self.assertEqual((row["status"], row["checkback_status"]), ("Sent", "skipped"))

    def test_interview_and_declined(self):
        self.post(action="save", outcome="interview")
        self.assertEqual((self.row()["outcome"], self.row()["checkback_date"]), ("interview", ""))
        self.assertIn(">interview<", self.c.get("/sent?show=replied").data.decode().replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
