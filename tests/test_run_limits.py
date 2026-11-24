"""Daily send caps hold across runs, and Run now can't flood Gmail or bump a scheduled run."""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from test_roles import InTempDir, FakeSMTP, write_file, write_queue  # noqa: E402


class DailyCapTests(InTempDir):
    def setUp(self):
        super().setUp()
        self.applicant, self.op, self.fu = self.reload_pipeline()
        import smtp_client
        self.fake = FakeSMTP(); self.fake.install(smtp_client)
        self.op.time.sleep = lambda s: None
        write_file(self.applicant.cv_path_for("devops"), b"%PDF-devops")
        self.op.DAILY_SEND_LIMIT = 3

    def approved(self, n, start=0):
        return [{"company_name": f"Co {i}", "email": f"a@co{i}.io", "status": "Approved", "track": "devops",
                 "company_type": "general_startup", "recipient_name": "The Team"} for i in range(start, start + n)]

    def test_second_run_same_day_only_sends_whats_left(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sent_earlier = [dict(r, status="Sent", date_sent=now) for r in self.approved(2, start=100)]
        write_queue(self.op, sent_earlier + self.approved(5))
        self.op.run_generate_letters()
        self.op.run_send_emails()
        self.assertEqual(len(self.fake.sent), 1, "2 already sent today + 1 = the cap of 3")

    def test_cap_used_up_sends_nothing(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_queue(self.op, [dict(r, status="Sent", date_sent=now) for r in self.approved(3, start=100)]
                    + self.approved(2))
        self.op.run_send_emails()
        self.assertEqual(self.fake.sent, [])

    def test_yesterdays_sends_dont_count(self):
        y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        write_queue(self.op, [dict(r, status="Sent", date_sent=y) for r in self.approved(3, start=100)]
                    + self.approved(5))
        self.op.run_generate_letters()
        self.op.run_send_emails()
        self.assertEqual(len(self.fake.sent), 3)


if __name__ == "__main__":
    unittest.main()
