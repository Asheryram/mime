import os
from datetime import datetime
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib import colors

import applicant


class CoverLetterGenerator:
    def __init__(self):
        # Palette colors matching the corporate style
        self.primary_color = colors.HexColor("#77216F")  # Purple
        self.secondary_color = colors.HexColor("#E95420")  # Orange/Warm red
        self.dark_color = colors.HexColor("#1a1a1a")
        self.mid_color = colors.HexColor("#2b2b2b")
        self.gray_color = colors.HexColor("#555555")

    def _get_styles(self):
        def S(name, font, size, **kw):
            return ParagraphStyle(name, fontName=font, fontSize=size, leading=size * 1.45, **kw)

        return {
            "name": S("N", "Helvetica-Bold", 17, textColor=self.dark_color, spaceAfter=1),
            "sub": S("Sub", "Helvetica-Bold", 10, textColor=self.primary_color, spaceAfter=2),
            "contact": S("C", "Helvetica", 8.5, textColor=self.gray_color, spaceAfter=2),
            "meta": S("M", "Helvetica", 9.5, textColor=self.gray_color, spaceAfter=2),
            "body": S("B", "Helvetica", 10, textColor=self.mid_color, alignment=TA_JUSTIFY, spaceAfter=7),
            "sign": S("Sg", "Helvetica", 10, textColor=self.mid_color, spaceAfter=0)
        }

    def get_template_paragraphs(self, company_name, role_title, company_type):
        """
        Returns paragraphs of text customized by company type.

        company_name and role_title must already be XML-escaped; the bullet
        strings below contain deliberate ReportLab markup.
        """
        paragraphs = []

        # Paragraph 1: Introduction
        p1 = (
            f"I am writing to express my strong interest in the {role_title} position at {company_name}. "
            f"As a Computer Science graduate of KNUST and a Cloud Engineer at AmaliTech, I have built my "
            f"foundation on AWS architecture, Infrastructure as Code, and CI/CD automation. I am eager to "
            f"bring my technical depth, automation-first mindset, and adaptability to your engineering team."
        )
        paragraphs.append(p1)

        # Paragraph 2 & bullet points based on company type
        if company_type == "consultancy":
            p2 = (
                "Consulting rewards engineers who can move between client environments without losing rigour. "
                "My AWS production experience transfers directly, because the automation I write is declarative "
                "and provider-agnostic:"
            )
            b1 = "<b>&bull;&#160;&#160;Cloud-Agnostic IaC:</b> I provision infrastructure as modular Terraform &ndash; 55 resources spanning VPC, EC2, Lambda, EventBridge, and SNS, with tfsec scanning and GitHub Actions CI. Moving that to Azure or GCP changes the provider schema, not the automation logic."
            b2 = "<b>&bull;&#160;&#160;Platform Trade-off Analysis:</b> I deployed the same multi-service application to both ECS Fargate and EKS to benchmark orchestrator cost and performance, packaging the Kubernetes workloads as Helm charts &ndash; so I can advise clients on platform choice with real numbers rather than preference."
            b3 = "<b>&bull;&#160;&#160;Cost &amp; Governance Advisory:</b> I ran a FinOps cost audit on an inherited AWS account, automating zombie-asset detection and budget governance through Terraform, turning a one-off clean-up into an enforced standard."
            bullets = [b1, b2, b3]

        elif company_type == "fintech":
            p2 = (
                "Transactional platforms demand reliability, provable security, and tested recoverability. "
                "I treat those as build-time requirements rather than later hardening:"
            )
            b1 = "<b>&bull;&#160;&#160;Secure Software Supply Chain:</b> I engineered a pipeline built on immutable artifacts (CodeArtifact, ECR image scanning) with security gates &ndash; GuardDuty, Trivy, and SonarQube &ndash; enforced before any code reaches production."
            b2 = "<b>&bull;&#160;&#160;Tested Disaster Recovery:</b> I built a disaster recovery lab with cross-region RDS failover, achieving a recovery time objective of 30 minutes or less under test rather than on paper."
            b3 = "<b>&bull;&#160;&#160;Shift-Left IaC Security:</b> Beyond tfsec scanning in GitHub Actions, I built an AI-assisted Terraform change reviewer (n8n with the Claude and OpenAI APIs) that summarises infrastructure diffs and flags security vulnerabilities on every pull request."
            bullets = [b1, b2, b3]

        elif company_type == "telecom" or company_type == "isp":
            p2 = (
                "Network and service providers depend on sound topology design and proactive monitoring. "
                "My work is grounded in cloud networking, Linux, and end-to-end observability:"
            )
            b1 = "<b>&bull;&#160;&#160;Network Design &amp; Isolation:</b> I design secure VPCs, subnets, routing, and IAM roles, reducing potential security misconfigurations by 30%, applying Cisco networking fundamentals (subnetting, routing) directly to cloud topologies."
            b2 = "<b>&bull;&#160;&#160;Full-Stack Observability:</b> I wire Prometheus, Grafana, Alertmanager, and Jaeger distributed tracing into the delivery pipeline itself, so latency and availability regressions surface internally before customers ever report them."
            b3 = "<b>&bull;&#160;&#160;Automated Remediation:</b> I built a self-healing AWS system using Lambda, CloudWatch, and Amazon Bedrock that detects anomalies and generates root-cause reports autonomously, removing the manual triage step from incident response."
            bullets = [b1, b2, b3]

        elif company_type == "bank":
            p2 = (
                "Financial institutions need auditable infrastructure, enforced least privilege, and recovery "
                "that has actually been exercised. My work maps closely onto those controls:"
            )
            b1 = "<b>&bull;&#160;&#160;Auditable Infrastructure:</b> Every resource I provision is defined in modular Terraform under version control with tfsec scanning in CI, so each infrastructure change is reviewable, reproducible, and attributable."
            b2 = "<b>&bull;&#160;&#160;Least Privilege &amp; Threat Detection:</b> I design scoped IAM roles and isolated VPCs that cut potential security misconfigurations by 30%, with GuardDuty threat detection and Trivy scanning enforced as pipeline gates."
            b3 = "<b>&bull;&#160;&#160;Proven Recovery:</b> I built a disaster recovery lab with cross-region RDS failover and immutable artifact storage, achieving a recovery time objective of 30 minutes or less."
            bullets = [b1, b2, b3]

        else:  # startup / general product company
            p2 = (
                "In a growing engineering team, cloud spend and engineering time are the two scarcest "
                "resources. I bring automation that protects both:"
            )
            b1 = "<b>&bull;&#160;&#160;Cost Control:</b> I cut infrastructure cost 60% through automated instance scheduling, and ran a FinOps audit that automated zombie-asset detection and budget governance in Terraform &ndash; savings that hold rather than drift back."
            b2 = "<b>&bull;&#160;&#160;Delivery Velocity:</b> I build Jenkins and GitHub Actions pipelines with Docker and Terraform that take a service from commit to running container with tests, image scanning, and observability already attached, so engineers ship without hand-holding infrastructure."
            b3 = "<b>&bull;&#160;&#160;Applied AI in Operations:</b> I built an AI-assisted Terraform reviewer (n8n with the Claude and OpenAI APIs) that flags security issues on every pull request, and a Bedrock-backed system that writes its own root-cause reports during incidents."
            bullets = [b1, b2, b3]

        paragraphs.append(p2)
        paragraphs.extend(bullets)

        # Paragraph 3: Closing
        p3 = (
            f"Thank you for your time and consideration. I would welcome the opportunity to discuss how my "
            f"cloud engineering skills, automation focus, and adaptability can support the technology "
            f"initiatives at {company_name}."
        )
        paragraphs.append(p3)

        return paragraphs

    def generate_pdf(self, company_name, role_title, company_type, recipient_name, company_address, output_path,
                     paragraphs=None):
        """
        Generates a professionally styled PDF cover letter.

        `paragraphs` overrides the sector template with body text written for one
        specific advertised role, so a letter answering a real job description
        can still carry the same letterhead as the campaign letters. Strings are
        inserted as ReportLab markup, so escape any caller-supplied text.
        """
        doc = SimpleDocTemplate(
            output_path, pagesize=A4,
            leftMargin=0.85 * inch, rightMargin=0.85 * inch,
            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        )

        styles = self._get_styles()
        story = []

        # ReportLab parses paragraphs as XML, so any ampersand or angle bracket
        # arriving from the queue CSV (e.g. "Smith & Co") must be escaped first.
        company_name = escape(company_name)
        role_title = escape(role_title)
        recipient_name_raw = recipient_name
        recipient_name = escape(recipient_name)
        company_address = escape(company_address) if company_address else ""

        # Letterhead
        story.append(Paragraph(escape(applicant.FULL_NAME).upper(), styles["name"]))
        story.append(Paragraph(escape(applicant.HEADLINE), styles["sub"]))
        story.append(Paragraph(
            f"{escape(applicant.PHONE)}&#160;&#160;|&#160;&#160;{escape(applicant.EMAIL)}&#160;&#160;|&#160;&#160;"
            f'<a href="{applicant.LINKEDIN_URL}" color="{self.primary_color.hexval()}">'
            f"{escape(applicant.LINKEDIN_LABEL)}</a>&#160;&#160;|&#160;&#160;"
            f'<a href="{applicant.GITHUB_URL}" color="{self.primary_color.hexval()}">'
            f"{escape(applicant.GITHUB_LABEL)}</a>&#160;&#160;|&#160;&#160;{escape(applicant.LOCATION)}",
            styles["contact"]
        ))

        # Orange divider
        story.append(HRFlowable(width="100%", thickness=1.5, color=self.secondary_color, spaceAfter=8, spaceBefore=4))

        # Date & Addressee
        date_str = datetime.now().strftime("%d %B %Y")
        story.append(Paragraph(date_str, styles["meta"]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(recipient_name, styles["meta"]))
        story.append(Paragraph(company_name, styles["meta"]))
        if company_address:
            story.append(Paragraph(company_address, styles["meta"]))
        story.append(Spacer(1, 8))

        # Salutation: use a first name only when the recipient is a real person.
        salutation_name = recipient_name_raw.strip()
        words = salutation_name.split()
        generic_keywords = ["team", "manager", "recruiting", "talent", "acquisition", "hr",
                            "resources", "hiring", "services", "solutions", "technology", "tech"]
        is_generic = any(k in salutation_name.lower() for k in generic_keywords)

        if is_generic:
            # The address block reads "The Recruiting and Technology Team", but
            # the salutation must not: "Dear The Recruiting Team," is not English.
            if salutation_name.lower().startswith("the "):
                salutation_name = salutation_name[4:]
        elif len(words) >= 2:
            first = words[0]
            last = words[-1]
            if first.lower() in ["mr", "mr.", "ms", "ms.", "mrs", "mrs.", "dr", "dr.", "prof", "prof."]:
                salutation_name = f"{first} {last}"
            else:
                salutation_name = first

        story.append(Paragraph(f"Dear {escape(salutation_name)},", styles["body"]))

        # Body Paragraphs
        body_paras = paragraphs or self.get_template_paragraphs(company_name, role_title, company_type)
        for p in body_paras:
            story.append(Paragraph(p, styles["body"]))

        # Sign-off
        story.append(Spacer(1, 6))
        story.append(Paragraph("Sincerely,", styles["sign"]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(f"<b>{escape(applicant.FULL_NAME)}</b>", styles["sign"]))
        story.append(Paragraph(escape(applicant.HEADLINE), styles["contact"]))

        doc.build(story)
        print(f"[Generator] Generated cover letter PDF at: {output_path}")
        return output_path
