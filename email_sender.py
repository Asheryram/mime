import smtplib
import ssl
import os
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
from config import config
import applicant


class EmailSender:
    def __init__(self):
        self.sender_email = config.get("SENDER_EMAIL")
        # Google displays app passwords in four space-separated blocks; SMTP AUTH
        # only accepts the 16 characters unbroken, so strip whitespace here
        # rather than depending on how it was pasted into .env.
        self.password = (config.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
        if not self.sender_email or not self.password:
            raise ValueError("SENDER_EMAIL or GMAIL_APP_PASSWORD is missing in configuration.")

    def send_outreach_email(self, recipient_email, company_name, cover_letter_path, cv_path,
                            role_title=applicant.ROLE_TITLE):
        """
        Sends an outreach email with the CV and cover letter attached.

        Returns the message's Message-ID on success, or None on failure. The
        caller should persist that ID so the follow-up can thread against it.
        """
        if not recipient_email:
            print("[EmailSender] Error: Recipient email is empty.")
            return None

        # Generated up front so it can be stored and referenced by the follow-up.
        message_id = make_msgid(domain=self.sender_email.split("@")[-1])

        msg = EmailMessage()
        msg["From"] = self.sender_email
        msg["To"] = recipient_email
        msg["Subject"] = applicant.EMAIL_SUBJECT
        msg["Message-ID"] = message_id
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(applicant.build_outreach_body(company_name))

        # Attach Cover Letter
        if os.path.exists(cover_letter_path):
            with open(cover_letter_path, "rb") as f:
                file_data = f.read()
                file_name = os.path.basename(cover_letter_path)
            msg.add_attachment(file_data, maintype="application", subtype="pdf", filename=file_name)
        else:
            print(f"[EmailSender] Error: Cover letter path {cover_letter_path} does not exist.")
            return None

        # Attach CV
        if os.path.exists(cv_path):
            with open(cv_path, "rb") as f:
                file_data = f.read()
                file_name = os.path.basename(cv_path)
            msg.add_attachment(file_data, maintype="application", subtype="pdf", filename=file_name)
        else:
            print(f"[EmailSender] Error: CV path {cv_path} does not exist.")
            return None

        # Send email
        context = ssl.create_default_context()
        try:
            print(f"[EmailSender] Connecting to SMTP server to email {recipient_email}...")
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
                smtp.login(self.sender_email, self.password)
                smtp.sendmail(self.sender_email, recipient_email, msg.as_string())
            print(f"[EmailSender] Successfully sent email to {recipient_email}")
            return message_id
        except Exception as e:
            print(f"[EmailSender] SMTP Error sending to {recipient_email}: {e}")
            return None
