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
CV_PATH = "YramAsherTettehAbotsi_resume.pdf"

# Prefix for generated cover letter filenames (no spaces).
FILE_SLUG = "Asher_Yram_Tetteh-Abotsi"

# --- Application tracks --------------------------------------------------
# One pitch per kind of role applied for. A queue row carries a track, so the
# same campaign can approach one company about DevOps work and another about
# backend engineering without either letter reading like a generic template.
#
# Every claim below must be supported by the CV - the letter and the CV are
# read side by side, and a highlight the resume cannot back is worse than no
# highlight at all.
TRACKS = {
    "devops": {
        "role_title": "Cloud & DevOps Engineer",
        "subject_area": "Cloud & DevOps Engineering",
        "interest_area": "Cloud and DevOps Engineering",
        "closing_skills": "cloud engineering skills, automation focus, and adaptability",
        "credentials": (
            "a Computer Science graduate of KNUST and a Cloud Engineer who designs "
            "and automates AWS infrastructure"
        ),
        "highlights": [
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
        ],
    },

    "software": {
        "role_title": "Software Engineer",
        "subject_area": "Software Engineering",
        "interest_area": "Software Engineering",
        "closing_skills": "backend engineering skills, product focus, and adaptability",
        "credentials": (
            "a Computer Science graduate of KNUST and a software engineer who builds "
            "backend services and full-stack products"
        ),
        "highlights": [
            "Building an AI assistant backend in NestJS and TypeScript, integrating the "
            "Anthropic Claude SDK for intent detection and policy-driven customer "
            "conversations, deployed to AWS ECS Fargate.",

            "Designing a microservices school management system in Node.js with dedicated "
            "auth, user, school, communication, and notification services behind an API "
            "gateway, orchestrated with Docker Compose.",

            "Developing a React and Vite frontend that generates client-facing PDF "
            "proposals, and refactoring a legacy Python codebase to cut redundancy 50% "
            "while improving readability.",

            "Working across Java, Python, TypeScript, C++, and SQL/NoSQL, with the CI/CD "
            "and containerisation background to take a service from commit to production "
            "without handing it off.",
        ],
    },

    "sysadmin": {
        "role_title": "IT Systems Administrator",
        "subject_area": "IT and Systems Administration",
        "interest_area": "IT and Systems Administration",
        "closing_skills": "systems administration skills, security focus, and adaptability",
        "credentials": (
            "a Computer Science graduate of KNUST and an infrastructure engineer who "
            "administers and secures cloud and network systems"
        ),
        "highlights": [
            "Provisioning and configuring 10+ AWS services including EC2, S3, IAM, and "
            "CloudFormation, designing secure VPCs and IAM roles that cut potential "
            "security misconfigurations by 30%.",

            "Applying Cisco networking fundamentals (subnetting, routing) to production "
            "cloud topologies, alongside day-to-day Linux and Bash administration.",

            "Running monitoring and alerting across Prometheus, Grafana, Alertmanager, and "
            "Jaeger, with GuardDuty threat detection, so faults surface before users "
            "report them.",

            "Building and testing disaster recovery with cross-region RDS failover and "
            "immutable backups, achieving a recovery time objective of 30 minutes or less.",
        ],
    },
}

DEFAULT_TRACK = "devops"


def get_track(track=None):
    """Look up a track, falling back to the default rather than raising."""
    return TRACKS.get((track or DEFAULT_TRACK).strip().lower(), TRACKS[DEFAULT_TRACK])


# Backwards-compatible module-level values: anything that has not been made
# track-aware keeps working and gets the default track's pitch.
CREDENTIALS = TRACKS[DEFAULT_TRACK]["credentials"]
EMAIL_HIGHLIGHTS = TRACKS[DEFAULT_TRACK]["highlights"]
EMAIL_SUBJECT = f"{TRACKS[DEFAULT_TRACK]['subject_area']} Application - {FULL_NAME}"


def subject_for(track=None):
    return f"{get_track(track)['subject_area']} Application - {FULL_NAME}"


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


def build_outreach_body(company_name, track=None):
    """
    The cold email body. The follow-up quotes this verbatim, so both messages
    are generated from one template and can never disagree.
    """
    t = get_track(track)
    highlights = "\n".join(f"- {h}" for h in t["highlights"])
    return (
        f"Dear Recruiting Team at {company_name},\n\n"
        f"I hope this email finds you well.\n\n"
        f"My name is {FULL_NAME}. I am {t['credentials']}.\n\n"
        f"I am writing to express my strong interest in {t['interest_area']} "
        f"opportunities at {company_name}. To share details on how my background aligns "
        f"with your work, I have attached my CV and a tailored cover letter.\n\n"
        f"Some highlights of my work include:\n"
        f"{highlights}\n\n"
        f"I would welcome the opportunity to speak briefly with a member of your team or "
        f"your engineering leads about how my skills and experience can "
        f"add value to {company_name}.\n\n"
        f"Thank you for your time and consideration.\n\n"
        f"{signature_block()}"
    )


# What the follow-up offers to talk through, per track.
_FOLLOWUP_EXPERTISE = {
    "devops": "Infrastructure as Code, CI/CD automation, and cloud cost optimisation",
    "software": "backend service design, API development, and shipping features end to end",
    "sysadmin": "cloud and network administration, monitoring, and security hardening",
}


def build_followup_body(company_name, recipient_name, original_date, track=None):
    """The 6-day check-in, with the original message quoted underneath."""
    t = get_track(track)
    key = (track or DEFAULT_TRACK).strip().lower()
    expertise = _FOLLOWUP_EXPERTISE.get(key, _FOLLOWUP_EXPERTISE[DEFAULT_TRACK])
    return (
        f"Dear {recipient_name},\n\n"
        f"I hope this email finds you well.\n\n"
        f"I am writing to follow up briefly on my application for {t['interest_area']} "
        f"opportunities at {company_name}, sent last week.\n\n"
        f"I remain very interested in the work your team is doing, and wanted to check "
        f"whether there are any updates, or whether you would be open to a brief "
        f"10-minute call. I would be glad to share how my experience with "
        f"{expertise} can support your team's goals.\n\n"
        f"I have attached my CV again for your convenience. Please let me know if there "
        f"is any other information I can provide.\n\n"
        f"Thank you for your time and consideration.\n\n"
        f"{signature_block()}"
        f"\n\n---\n"
        f"Original Message:\n"
        f"From: {EMAIL}\n"
        f"Sent: {original_date}\n"
        f"Subject: {subject_for(track)}\n\n"
        f"{build_outreach_body(company_name, track)}"
    )
