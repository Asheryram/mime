"""
Gmail SMTP with honest errors.

smtplib's login() tries each AUTH mechanism in turn; Gmail answers a bad
password with "535 Username and Password not accepted" and then hangs up, so
the *next* mechanism fails with "Connection unexpectedly closed" and the real
cause is lost. Authenticating with PLAIN only surfaces the 535 as-is, which is
raised as GmailAuthError: a problem with the account, not the recipient, so
callers stop the run instead of retrying or marking every row Failed.
"""
import smtplib
import ssl

from config import config


class GmailAuthError(Exception):
    """Gmail refused the login itself; nothing can be sent until it's fixed."""


AUTH_HELP = (
    "Gmail rejected the login for {user} (535 Username and Password not accepted). "
    "The app password is wrong or has been revoked; Google revokes app passwords when the "
    "account password changes or 2-Step Verification is turned off. Create a new one at "
    "https://myaccount.google.com/apppasswords and update GMAIL_APP_PASSWORD everywhere it's set: "
    ".env, the GitHub Actions secret, and the dashboard's Vercel environment."
)


def credentials():
    user = config.get("SENDER_EMAIL")
    # Google shows app passwords as four space-separated blocks; SMTP wants them unbroken.
    password = (config.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not password:
        raise GmailAuthError("SENDER_EMAIL or GMAIL_APP_PASSWORD is not set.")
    return user, password


def connect():
    """An authenticated SMTP_SSL connection to Gmail, and the sender address."""
    user, password = credentials()
    smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30)
    try:
        smtp.ehlo()
        smtp.user, smtp.password = user, password
        smtp.auth("PLAIN", smtp.auth_plain)
    except smtplib.SMTPAuthenticationError as e:
        smtp.close()
        raise GmailAuthError(AUTH_HELP.format(user=user)) from e
    except Exception:
        smtp.close()
        raise
    return smtp, user


def send(msg, to_addr):
    """Send one message on a fresh connection. Raises GmailAuthError on a login failure."""
    smtp, user = connect()
    try:
        smtp.sendmail(user, to_addr, msg.as_string())
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()
