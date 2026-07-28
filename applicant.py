"""
Single source of truth for the applicant's identity and pitch.

Every personal detail lives here so the cold email, the follow-up, and the PDF
letterhead can never drift apart. To reuse this pipeline, edit this file - you
should not need to touch the pipeline scripts at all.

Phone and email are the exception: they are read from .env, which is
gitignored, so this file stays publishable without exposing a personal number
and inbox to search engines and address scrapers.
"""

from config import config

# --- Identity -----------------------------------------------------------
FULL_NAME = "Asher Yram Tetteh-Abotsi"
HEADLINE = "Cloud & DevOps Engineer"
ROLE_TITLE = "Cloud & DevOps Engineer"
LOCATION = "Takoradi, Ghana"

# Set APPLICANT_PHONE and APPLICANT_EMAIL in .env. EMAIL falls back to the
# SENDER_EMAIL already configured for SMTP, since they are normally the same
# address.
PHONE = config.get("APPLICANT_PHONE", "")
EMAIL = config.get("APPLICANT_EMAIL") or config.get("SENDER_EMAIL", "")

LINKEDIN_URL = "https://www.linkedin.com/in/asher-tetteh-abotsi"
LINKEDIN_LABEL = "linkedin.com/in/asher-tetteh-abotsi"
GITHUB_URL = "https://github.com/Asheryram"
GITHUB_LABEL = "github.com/Asheryram"

# Your master CV. Place this PDF in the repository root.
CV_PATH = "Asher_Yram_Tetteh-Abotsi_CV.pdf"

# Prefix for generated cover letter filenames (no spaces).
FILE_SLUG = "Asher_Yram_Tetteh-Abotsi"

# One-line summary of who you are, used to open the cold email.
CREDENTIALS = (
    "a Computer Science graduate of KNUST and a Cloud Engineer who designs "
    "and automates AWS infrastructure"
)

# Four strongest, most quantified achievements - these open the cold email.
EMAIL_HIGHLIGHTS = [
    "Building a self-healing AWS system for a Flask microservice using Lambda, "
    "CloudWatch, and Amazon Bedrock that detects anomalies and generates "
    "AI-driven root-cause reports without manual intervention.",

    "Provisioning 55 AWS resources through modular Terraform (VPC, EC2, Lambda, "
    "EventBridge, SNS) with tfsec security scanning and GitHub Actions CI for "
    "fully reproducible infrastructure.",

    "Cutting infrastructure cost 60% through automated instance scheduling, "
    "delivered via Jenkins CI/CD pipelines with full observability "
    "(Prometheus, Grafana, Alertmanager, Jaeger).",

    "Engineering a secure software supply chain and disaster recovery lab with "
    "immutable artifacts and cross-region RDS failover, achieving a recovery "
    "time objective of 30 minutes or less.",
]

EMAIL_SUBJECT = f"Cloud & DevOps Engineering Application - {FULL_NAME}"


def signature_block():
    """Plain-text sign-off shared by the cold email and the follow-up."""
    return (
        f"Best regards,\n\n"
        f"{FULL_NAME}\n"
        f"{LOCATION}\n"
        f"Phone: {PHONE}\n"
        f"LinkedIn: {LINKEDIN_URL}\n"
        f"GitHub: {GITHUB_URL}\n"
    )


def build_outreach_body(company_name):
    """
    The cold email body. The follow-up quotes this verbatim, so both messages
    are generated from one template and can never disagree.
    """
    highlights = "\n".join(f"- {h}" for h in EMAIL_HIGHLIGHTS)
    return (
        f"Dear Recruiting Team at {company_name},\n\n"
        f"I hope this email finds you well.\n\n"
        f"My name is {FULL_NAME}. I am {CREDENTIALS}.\n\n"
        f"I am writing to express my strong interest in Cloud and DevOps Engineering "
        f"opportunities at {company_name}. To share details on how my background aligns "
        f"with your work, I have attached my CV and a tailored cover letter.\n\n"
        f"Some highlights of my work include:\n"
        f"{highlights}\n\n"
        f"I would welcome the opportunity to speak briefly with a member of your team or "
        f"your engineering leads about how my automation skills and cloud experience can "
        f"add value to {company_name}.\n\n"
        f"Thank you for your time and consideration.\n\n"
        f"{signature_block()}"
    )


def build_followup_body(company_name, recipient_name, original_date):
    """The 6-day check-in, with the original message quoted underneath."""
    return (
        f"Dear {recipient_name},\n\n"
        f"I hope this email finds you well.\n\n"
        f"I am writing to follow up briefly on my application for Cloud and DevOps "
        f"Engineering opportunities at {company_name}, sent last week.\n\n"
        f"I remain very interested in the work your team is doing, and wanted to check "
        f"whether there are any updates, or whether you would be open to a brief "
        f"10-minute call. I would be glad to share how my experience with Infrastructure "
        f"as Code, CI/CD automation, and cloud cost optimisation can support your team's "
        f"goals.\n\n"
        f"I have attached my CV again for your convenience. Please let me know if there "
        f"is any other information I can provide.\n\n"
        f"Thank you for your time and consideration.\n\n"
        f"{signature_block()}"
        f"\n\n---\n"
        f"Original Message:\n"
        f"From: {EMAIL}\n"
        f"Sent: {original_date}\n"
        f"Subject: {EMAIL_SUBJECT}\n\n"
        f"{build_outreach_body(company_name)}"
    )
