"""
Referrals: a company replies "send your application to X", and from its page
you send it there with the CV, the cover letter, or both.

Run from the repo root:  python3 -m unittest discover tests
"""
import importlib
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))
os.environ.setdefault("FLASK_SECRET_KEY", "test")

from test_roles import InTempDir, FakeStateRepo, FakeSMTP, write_queue, write_file  # noqa: E402
from test_inbox_check import FakeIMAP, mail, ME  # noqa: E402


def attachments(msg):
    return {p.get_filename(): p.get_payload(decode=True) for p in msg.iter_attachments()}


class PreviewParityTests(unittest.TestCase):
    def test_dashboard_preview_matches_what_is_sent(self):
        import applicant, app as webapp
        for key in applicant.TRACKS:
            for who in ("Agyeiwaa", ""):
                for attach in ("both", "cv", "letter"):
                    with self.subTest(role=key, who=who, attach=attach):
                        real = applicant.build_referral_body("MTN Ghana", key, who, attach)
                        real = real.replace(applicant.FULL_NAME, "<your name>")
                        pv = webapp.referral_preview(applicant.TRACKS[key], "MTN Ghana", who, attach)
                        self.assertTrue(real.startswith(pv["body"]), "preview drifted from build_referral_body")
                        self.assertEqual(pv["subject"],
                                         applicant.referral_subject(key).replace(applicant.FULL_NAME, "<your name>"))

    def test_text(self):
        import applicant
        body = applicant.build_referral_body("MTN Ghana", "devops", "Agyeiwaa", "cv")
        self.assertTrue(body.startswith("Dear MTN Ghana Recruitment Team,\n\nAgyeiwaa from your team kindly suggested"))
        self.assertIn("My CV is attached.", body)
        self.assertNotIn("cover letter", body.split("Some highlights")[1].split("Best regards")[0].lower())
        self.assertEqual(body.count("\n- "), 3, "top 3 highlights only")
        self.assertIn("I was asked to send", applicant.build_referral_body("X", "devops", "", "both"))


class SendTests(InTempDir):
    def setUp(self):
        super().setUp()
        self.applicant, self.op, _ = self.reload_pipeline()
        import smtp_client
        self.smtp = FakeSMTP(); self.smtp.install(smtp_client)
        write_file(self.applicant.cv_path_for("devops"), b"%PDF-devops")

    def row(self, **k):
        r = {"company_name": "MTN Ghana", "email": "customercare.gh@mtn.com", "status": "Sent", "track": "devops",
             "replied": "yes", "referral_email": "MTNGhanaRecruitment@mtn.com", "referral_by": "Agyeiwaa",
             "referral_attach": "both", "referral_status": "queued", "company_type": "general_startup"}
        r.update(k)
        return r

    def test_each_attachment_choice(self):
        expected = {"both": {"letter", "cv"}, "cv": {"cv"}, "letter": {"letter"}}
        for attach, want in expected.items():
            with self.subTest(attach=attach):
                self.smtp.sent.clear()
                write_queue(self.op, [self.row(referral_attach=attach)])
                self.op.run_send_referrals()
                to, msg = self.smtp.sent[0]
                self.assertEqual(to, "MTNGhanaRecruitment@mtn.com")
                files = attachments(msg)
                got = set()
                if self.applicant.CV_ATTACHMENT_NAME in files:
                    got.add("cv")
                    self.assertEqual(files[self.applicant.CV_ATTACHMENT_NAME], b"%PDF-devops")
                if any(n != self.applicant.CV_ATTACHMENT_NAME for n in files):
                    got.add("letter")
                self.assertEqual(got, want)
                self.assertEqual(msg["Subject"], self.applicant.referral_subject("devops"))
                row = self.op.load_queue()[0]
                self.assertEqual(row["referral_status"], "sent")
                self.assertTrue(row["referral_message_id"].startswith("<"))
                self.assertEqual(row["status"], "Sent", "the company row itself is untouched")

    def test_drafts_are_never_sent(self):
        write_queue(self.op, [self.row(referral_status="draft")])
        self.op.run_send_referrals()
        self.assertEqual(self.smtp.sent, [])

    def test_missing_cv_fails_clearly_instead_of_sending_without_it(self):
        write_queue(self.op, [self.row(track="software")])
        self.op.run_send_referrals()
        self.assertEqual(self.smtp.sent, [])
        row = self.op.load_queue()[0]
        self.assertEqual(row["referral_status"], "failed")
        self.assertIn("no CV", row["referral_note"])

    def test_letter_only_needs_no_cv(self):
        write_queue(self.op, [self.row(track="software", referral_attach="letter")])
        self.op.run_send_referrals()
        self.assertEqual(len(self.smtp.sent), 1)

    def test_not_counted_in_the_daily_send_or_followed_up(self):
        import followup_pipeline as fp
        fp.time.sleep = self.op.time.sleep = lambda s: None
        write_queue(self.op, [self.row(date_sent="2020-01-01 09:00:00", replied="yes")])
        self.op.run_send_referrals()
        self.op.run_send_emails()
        fp.run_followups(dry_run=False)
        self.assertEqual(len(self.smtp.sent), 1)


class InboxTests(InTempDir):
    def test_reply_to_the_referral_is_detected(self):
        applicant, op, _ = self.reload_pipeline()
        import inbox_check
        q = [{"company_name": "MTN Ghana", "email": "customercare.gh@mtn.com", "status": "Sent", "replied": "yes",
              "bounced": "", "referral_status": "sent", "referral_email": "MTNGhanaRecruitment@mtn.com",
              "referral_message_id": "<ref-1@gmail.com>", "referral_date": "2026-11-20 17:00:00",
              "referral_replied": ""}]
        fake = FakeIMAP([mail("Talent <talent.acquisition@mtn.com>", subject="Re: Application",
                              date="Mon, 23 Nov 2026 09:00:00 +0000", in_reply_to="<ref-1@gmail.com>")])
        replied, _ = inbox_check.check_inbox(q, ME, "pw", imap_factory=fake)
        self.assertEqual(replied, ["MTN Ghana (referral)"])
        self.assertEqual(q[0]["referral_replied"], "yes")
        self.assertIn("talent.acquisition@mtn.com", q[0]["referral_note"])

    def test_referral_bounce_is_recorded(self):
        import inbox_check
        q = [{"company_name": "MTN Ghana", "email": "x@mtn.com", "status": "Sent", "replied": "yes", "bounced": "",
              "referral_status": "sent", "referral_email": "wrong@mtn.com", "referral_message_id": "<ref-2@gmail.com>",
              "referral_date": "2026-11-20 17:00:00", "referral_replied": ""}]
        fake = FakeIMAP([mail("mailer-daemon@googlemail.com", subject="Delivery failure",
                              date="Fri, 20 Nov 2026 17:01:00 +0000", extra="X-Failed-Recipients: wrong@mtn.com\r\n")])
        inbox_check.check_inbox(q, ME, "pw", imap_factory=fake)
        self.assertEqual(q[0]["referral_status"], "bounced")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        import state_repo, app as webapp
        importlib.reload(state_repo); importlib.reload(webapp)
        self.sr, self.app = state_repo, webapp
        self.fake = FakeStateRepo(state_repo)
        self.fake.files[state_repo.DEFAULT_TRACKS["devops"]["cv_path"]] = b"%PDF"
        F = state_repo.QUEUE_FIELDS
        row = {f: "" for f in F}
        row.update({"company_name": "MTN Ghana", "website": "https://mtn.com.gh", "email": "customercare.gh@mtn.com",
                    "status": "Sent", "track": "devops", "date_sent": "2026-11-19 07:30:00"})
        self.fake.files["outreach_queue.csv"] = (",".join(F) + "\n" + ",".join(row[f] for f in F) + "\n").encode()
        self.dispatched = []
        state_repo.dispatch_workflow = lambda wf, inputs=None: self.dispatched.append((wf, inputs))
        self.c = webapp.app.test_client()
        with self.c.session_transaction() as s:
            s["authed"] = True

    def row(self):
        return self.sr._parse_queue(self.fake.files["outreach_queue.csv"].decode())[0]

    def post(self, **form):
        return self.c.post("/companies/mtn-ghana/referral", data=form, follow_redirects=True)

    def test_panel_shows_on_sent_companies_with_both_recommended(self):
        page = self.c.get("/companies/mtn-ghana").data.decode()
        self.assertIn("They referred me", page)
        self.assertIn('value="both" checked', page)
        self.assertIn("Recommended", page)

    def test_preview_then_send_now(self):
        r = self.post(action="preview", referral_email="MTNGhanaRecruitment@mtn.com", referral_by="Agyeiwaa",
                      track="devops", referral_attach="both")
        page = r.data.decode()
        self.assertIn("Agyeiwaa from your team kindly suggested", page)
        self.assertIn("Send now", page)
        row = self.row()
        self.assertEqual((row["referral_status"], row["replied"]), ("draft", "yes"),
                         "draft is never sent; their referral counts as a reply so no follow-up chases them")
        self.assertEqual(self.dispatched, [])

        r = self.post(action="send_now")
        self.assertEqual(self.row()["referral_status"], "queued")
        self.assertEqual(self.dispatched, [("send-pipeline.yml", {"mode": "referrals"})])
        self.assertIn("Sending to MTNGhanaRecruitment@mtn.com now", r.data.decode())

    def test_queue_for_next_batch_does_not_start_a_run(self):
        self.post(action="preview", referral_email="a@mtn.com", track="devops", referral_attach="cv")
        r = self.post(action="queue")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Queued: goes to a@mtn.com with the next send", r.data.decode())
        self.assertEqual(self.row()["referral_status"], "queued")
        self.assertEqual(self.dispatched, [])

    def test_send_now_falls_back_to_next_batch_if_github_refuses(self):
        def refuse(wf, inputs=None):
            raise self.sr.TokenPermissionError("x")
        self.sr.dispatch_workflow = refuse
        self.post(action="preview", referral_email="a@mtn.com", track="devops", referral_attach="both")
        r = self.post(action="send_now")
        self.assertIn("goes out with the next send", r.data.decode())
        self.assertEqual(self.row()["referral_status"], "queued")

    def test_cannot_send_without_previewing(self):
        r = self.post(action="send_now")
        self.assertIn("Preview the email first", r.data.decode())
        self.assertEqual(self.row()["referral_status"], "")
        self.assertEqual(self.dispatched, [])

    def test_validation(self):
        cases = [
            ({"referral_email": "not-an-email", "track": "devops", "referral_attach": "both"}, "Enter the address"),
            ({"referral_email": "a@mtn.com", "track": "nope", "referral_attach": "both"}, "Pick one of your roles"),
            ({"referral_email": "a@mtn.com", "track": "software", "referral_attach": "both"}, "has no CV yet"),
        ]
        for form, msg in cases:
            with self.subTest(msg=msg):
                r = self.post(action="preview", **form)
                self.assertIn(msg, r.data.decode())
                self.assertEqual(self.row()["referral_status"], "")
        # Letter only is fine without a CV.
        self.post(action="preview", referral_email="a@mtn.com", track="software", referral_attach="letter")
        self.assertEqual(self.row()["referral_status"], "draft")

    def test_cancel_before_it_goes(self):
        self.post(action="preview", referral_email="a@mtn.com", track="devops", referral_attach="both")
        self.post(action="queue")
        self.post(action="cancel")
        self.assertEqual(self.row()["referral_status"], "")

    def test_sent_state_shows_and_allows_another(self):
        q = self.row()
        q.update({"referral_status": "sent", "referral_email": "a@mtn.com", "referral_date": "2026-11-20 17:00:00",
                  "referral_replied": "yes"})
        F = self.sr.QUEUE_FIELDS
        self.fake.files["outreach_queue.csv"] = (",".join(F) + "\n" + ",".join(q[f] for f in F) + "\n").encode()
        page = self.c.get("/companies/mtn-ghana").data.decode()
        self.assertIn("Sent to <b>a@mtn.com</b>", page)
        self.assertIn("They replied.", page)
        self.assertIn("Send to another address", page)


class WorkflowTests(unittest.TestCase):
    def test_referrals_mode_sends_only_referrals(self):
        import yaml
        with open(os.path.join(ROOT, ".github", "workflows", "send-pipeline.yml")) as f:
            wf = yaml.safe_load(f)
        steps = {s.get("name"): s for s in wf["jobs"]["run"]["steps"]}
        for name in ("Auto-approve clean leads", "Generate cover letters", "Send approved applications",
                     "Send eligible follow-ups"):
            self.assertIn("inputs.mode != 'referrals'", steps[name].get("if", ""), name)
        for name in ("Mark replies and bounces from the inbox", "Send queued referrals"):
            # Run in both modes; skipped only when one invited user's run was asked for.
            self.assertEqual(steps[name].get("if"), "${{ !inputs.tenant }}", name)
        names = list(steps)
        self.assertLess(names.index("Mark replies and bounces from the inbox"), names.index("Send queued referrals"))
        on = wf.get("on") or wf.get(True)
        self.assertEqual(on["workflow_dispatch"]["inputs"]["mode"]["options"], ["full", "referrals"])


if __name__ == "__main__":
    unittest.main()
