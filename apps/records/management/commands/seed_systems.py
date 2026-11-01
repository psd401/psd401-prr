"""Pre-seed the PSD401 record-system registry — every box checked.

District's remaining work per system (tracked by needs_verification flag):
  1. Assign a person/email to the custodian job title.
  2. Verify retention windows (esp. BUS VIDEO — vendor unknown from public info).
  3. Turn each search_checklist into a formal SOP.
Sourced from PSD401's public web presence, July 2026. Re-runnable (upserts by
slug; district edits to custodian/retention fields are preserved on re-run).
"""
from django.core.management.base import BaseCommand

from apps.records.models import RecordSystem

SYSTEMS = [
    {
        "slug": "google-workspace", "name": "Google Workspace (Gmail/Drive/Chat)",
        "category": "Email & documents", "vendor": "Google",
        "integration": RecordSystem.Integration.VAULT,
        "custodian_title": "Director of Technology",
        "retention_days": None, "retention_note": "Vault holds preserve indefinitely once placed.",
        "urgent_hold": False, "triage_keywords": [],   # officers always scope Vault searches
        "search_checklist": (
            "1. Identify custodians (staff emails) and search terms/date range.\n"
            "2. Create Vault matter + holds via the PRR system (automated).\n"
            "3. Run mail + Drive exports; confirm ingest into review queue.\n"
            "4. Check Google Chat/Spaces if named in the request."),
    },
    {
        "slug": "avigilon-alta", "name": "Building Security Cameras (Avigilon Alta)",
        "category": "Video surveillance", "vendor": "Motorola Solutions / Avigilon",
        "integration": RecordSystem.Integration.AVIGILON,
        "custodian_title": "Director of Safety and Security",
        "retention_days": 10, "retention_note": "Rolling 10-day retention; saved clips persist.",
        "urgent_hold": True,
        "triage_keywords": [],   # handled by the dedicated footage triage engine
        "search_checklist": (
            "1. Automated hold fires at intake (all cameras, window clamped to retention).\n"
            "2. Review held cameras; exclude irrelevant ones WITH reasons.\n"
            "3. Check incident-hold clips and previously exported clips.\n"
            "4. Search email/Drive for shared copies of footage."),
    },
    {
        "slug": "bus-video", "name": "School Bus Video/Audio Surveillance",
        "category": "Video surveillance", "vendor": "⚠️ VERIFY — vendor not public",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Transportation Supervisor",
        "retention_days": None,
        "retention_note": "⚠️ VERIFY retention window — bus DVRs commonly overwrite in DAYS. "
                       "Until verified, every matching request is treated as a retention race.",
        "urgent_hold": True,
        "triage_keywords": ["bus video", "bus footage", "bus camera", "on the bus",
                         "school bus", "bus incident", "bus driver"],
        "search_checklist": (
            "1. URGENT: identify bus number/route and date-time from the request "
            "(BusRight route data can resolve which bus).\n"
            "2. Pull/preserve the DVR or cloud clip for the window IMMEDIATELY — "
            "do not wait for full request review.\n"
            "3. Check for incident saves already made by Transportation.\n"
            "4. Check driver incident reports and Safety Pass scans for the trip.\n"
            "5. Log preservation date/time — the gap between request receipt and "
            "pull is discoverable."),
    },
    {
        "slug": "powerschool-sis", "name": "PowerSchool SIS (+ Registration)",
        "category": "Student information", "vendor": "PowerSchool",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Executive Director of Student Services / District Registrar",
        "retention_days": None, "retention_note": "Student records per WA LGS retention schedule.",
        "urgent_hold": False,
        "triage_keywords": ["powerschool", "student records", "grades", "attendance",
                         "discipline", "enrollment", "transcript"],
        "search_checklist": (
            "1. Determine FERPA path: parent/eligible-student request (FERPA access) "
            "vs third-party PRA request (heavy redaction/withholding likely).\n"
            "2. Export the specific records (attendance, discipline log, enrollment).\n"
            "3. Route ALL third-party student-record productions to second review.\n"
            "4. Note: 2024 PowerSchool breach correspondence may itself be responsive "
            "to breach-related requests."),
    },
    {
        "slug": "schoology", "name": "Schoology LMS",
        "category": "LMS / classroom messaging", "vendor": "PowerSchool",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Director of Digital Learning",
        "retention_days": None, "retention_note": "Course content + messages retained in platform.",
        "urgent_hold": False,
        "triage_keywords": ["schoology", "course messages", "class messages",
                         "assignment", "lms"],
        "search_checklist": (
            "1. Admin-export messages between named staff/students for the date range.\n"
            "2. Pull course materials/announcements if requested.\n"
            "3. FERPA screen anything exposing other students (rosters, comment threads).\n"
            "4. Confirm with vendor whether deleted-message recovery is possible if "
            "the window predates current data."),
    },
    {
        "slug": "qmlativ", "name": "Skyward Qmlativ (HR / Finance / Payroll)",
        "category": "Business systems", "vendor": "Skyward (WSIPC)",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Executive Director of Business Services",
        "retention_days": None, "retention_note": "Financial/personnel records per WA LGS schedule.",
        "urgent_hold": False,
        "triage_keywords": ["personnel file", "payroll", "salary", "invoice", "contract",
                         "purchase order", "expenditure", "budget", "vendor payment",
                         "hr file", "qmlativ", "skyward"],
        "search_checklist": (
            "1. Identify record type: personnel file, payroll register, AP/vendor, contract.\n"
            "2. Export from the relevant Qmlativ module; include approval trails if asked.\n"
            "3. Apply employee-privacy exemptions (RCW 42.56.250) — DOB, addresses, "
            "SSNs are categorically redacted.\n"
            "4. For personnel investigations, coordinate with counsel before production."),
    },
    {
        "slug": "parentsquare", "name": "ParentSquare (Family Communications)",
        "category": "Mass & direct messaging", "vendor": "ParentSquare",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Director of Communications",
        "retention_days": None, "retention_note": "Posts and DMs retained in platform.",
        "urgent_hold": False,
        "triage_keywords": ["parentsquare", "parent square", "newsletter", "announcement",
                         "message to parents", "communication to families"],
        "search_checklist": (
            "1. Admin-search posts AND direct messages for named staff, date range.\n"
            "2. Export threads including delivery metadata if requested.\n"
            "3. Remember staff↔parent DMs about district business are public records "
            "even when conversational in tone."),
    },
    {
        "slug": "busright", "name": "BusRight (Routing & GPS)",
        "category": "Transportation data", "vendor": "BusRight",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Transportation Supervisor",
        "retention_days": None, "retention_note": "⚠️ VERIFY GPS-history retention with vendor.",
        "urgent_hold": False,
        "triage_keywords": ["bus route", "bus stop", "gps", "bus tracking", "bus location"],
        "search_checklist": (
            "1. Export route definition and stop times for the date in question.\n"
            "2. Pull GPS breadcrumb/history for the specific bus and window.\n"
            "3. Redact other students' stop-level data as needed (FERPA/privacy)."),
    },
    {
        "slug": "safety-pass", "name": "Safety Pass (Bus Ridership Scans)",
        "category": "Transportation data", "vendor": "Safety Pass",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Transportation Supervisor",
        "retention_days": None, "retention_note": "⚠️ VERIFY scan-log retention.",
        "urgent_hold": False,
        "triage_keywords": ["safety pass", "ridership", "rode the bus", "boarded the bus"],
        "search_checklist": (
            "1. Pull scan logs for the bus/route/date.\n"
            "2. FERPA screen: logs identify other student riders — third-party "
            "requests need heavy redaction."),
    },
    {
        "slug": "classlink", "name": "ClassLink (SSO / Rostering / Analytics)",
        "category": "Identity & analytics", "vendor": "ClassLink",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Director of Technology",
        "retention_days": None, "retention_note": "Usage analytics retained in platform.",
        "urgent_hold": False,
        "triage_keywords": ["classlink", "app usage", "login records", "sign-on records"],
        "search_checklist": (
            "1. Export the requested usage/analytics report.\n"
            "2. FERPA screen student-level usage data for third-party requests."),
    },
    {
        "slug": "schoolinks", "name": "SchooLinks (College & Career)",
        "category": "Student planning", "vendor": "SchooLinks",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Executive Director of Teaching and Learning",
        "retention_days": None, "retention_note": "",
        "urgent_hold": False,
        "triage_keywords": ["schoolinks", "high school and beyond"],
        "search_checklist": (
            "1. Export the student's plan/assessment records (usually FERPA-path).\n"
            "2. Third-party requests: expect near-total exemption."),
    },
    # ── Gap-analysis additions (custodian islands outside the tech stack) ──
    {
        "slug": "personal-devices", "name": "Personal Devices / Text Messages",
        "category": "Communications (off-system)", "vendor": "n/a — employee-held",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Every employee (coordinated by Public Records Officer)",
        "retention_days": None,
        "retention_note": "⚠️ HIGHEST-RISK GAP: Nissen v. Pierce County — texts about "
                       "district business on PERSONAL phones are public records.",
        "urgent_hold": False,
        "triage_keywords": ["text message", "text messages", "texts", "sms",
                         "cell phone", "personal phone", "personal email"],
        "search_checklist": (
            "1. Identify which employees plausibly texted about the subject.\n"
            "2. Issue the personal-device search directive (see "
            "templates/sops/personal-device-search.md).\n"
            "3. Collect responsive screenshots/exports + a signed affidavit of "
            "adequate search from each employee (Nissen procedure).\n"
            "4. Log each employee's response (responsive / none / no reply)."),
    },
    {
        "slug": "board-records", "name": "Board Records (BoardDocs + member communications)",
        "category": "Governance", "vendor": "Diligent BoardDocs (confirmed)",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Board Secretary / Executive Assistant to the Superintendent",
        "retention_days": None,
        "retention_note": "⚠️ Board members using personal email for district business "
                       "creates records outside Vault — policy fix recommended.",
        "urgent_hold": False,
        "triage_keywords": ["school board", "board member", "board meeting",
                         "board minutes", "board agenda", "executive session",
                         "boarddocs"],
        "search_checklist": (
            "1. Pull agendas/minutes/packets from BoardDocs for the range.\n"
            "2. Vault-search board members' DISTRICT accounts.\n"
            "3. Personal-account directive to board members (Nissen procedure) "
            "if the request plausibly reaches personal email/texts.\n"
            "4. Executive-session records: coordinate with counsel before any "
            "production decision."),
    },
    {
        "slug": "athletics", "name": "Athletics (coaches' comms, registration, boosters)",
        "category": "Athletics", "vendor": "⚠️ VERIFY registration platform + team apps in use",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Athletic Directors (per high school)",
        "retention_days": None,
        "retention_note": "Coach↔player/parent comms often run through personal phones "
                       "and team apps (GameChanger/Hudl/TeamSnap) — Nissen exposure.",
        "urgent_hold": False,
        "triage_keywords": ["coach", "athletics", "athletic", "booster", "varsity",
                         "tryout", "playing time", "team roster", "hudl",
                         "gamechanger", "teamsnap"],
        "search_checklist": (
            "1. Vault-search the coach's district account.\n"
            "2. Personal-device/team-app directive to coaching staff.\n"
            "3. Registration-platform export for the season if requested.\n"
            "4. Booster club records: determine district custody vs separate "
            "entity with counsel."),
    },
    {
        "slug": "special-education", "name": "Special Education (IEP system, due process)",
        "category": "Student services", "vendor": "⚠️ VERIFY IEP platform",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Executive Director of Student Services",
        "retention_days": None, "retention_note": "IDEA + WA LGS retention; FERPA throughout.",
        "urgent_hold": False,
        "triage_keywords": ["iep", "504 plan", "special education", "due process",
                         "evaluation report", "sped"],
        "search_checklist": (
            "1. FERPA path check (parent vs third party).\n"
            "2. Export IEP/evaluation records from the IEP platform.\n"
            "3. Due-process/complaint files: coordinate with counsel.\n"
            "4. Second review mandatory."),
    },
    {
        "slug": "health-services", "name": "Health Services (nurse records)",
        "category": "Student services", "vendor": "⚠️ VERIFY health-records module",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Lead Nurse / Executive Director of Student Services",
        "retention_days": None, "retention_note": "FERPA + health-privacy overlays.",
        "urgent_hold": False,
        "triage_keywords": ["nurse", "medication", "immunization", "health room",
                         "health records", "injury report"],
        "search_checklist": (
            "1. Pull nurse/medication/injury logs for the student & window.\n"
            "2. Third-party requests: near-total exemption expected — draft "
            "withholding log.\n3. Second review mandatory."),
    },
    {
        "slug": "nutrition-services", "name": "Nutrition Services (POS, meal applications)",
        "category": "Operations", "vendor": "⚠️ VERIFY POS/payment platform",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Director of Nutrition Services",
        "retention_days": None,
        "retention_note": "Free/reduced-meal applications are FEDERALLY protected "
                       "(7 CFR 245.6) — categorically withheld.",
        "urgent_hold": False,
        "triage_keywords": ["lunch", "meal", "nutrition", "food service",
                         "free and reduced", "cafeteria"],
        "search_checklist": (
            "1. Identify record type: menus/contracts (releasable) vs account/"
            "application data (protected).\n"
            "2. Export from POS/back office as applicable.\n"
            "3. NEVER produce free/reduced status or applications."),
    },
    {
        "slug": "hr-hiring", "name": "HR / Employee Support Services (hiring, subs, investigations)",
        "category": "Personnel", "vendor": "⚠️ VERIFY applicant-tracking + absence systems",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Executive Director of Employee Support Services",
        "retention_days": None, "retention_note": "RCW 42.56.250 exemptions apply heavily.",
        "urgent_hold": False,
        "triage_keywords": ["applicant", "hiring", "interview", "job posting",
                         "substitute", "reference check", "personnel investigation"],
        "search_checklist": (
            "1. Identify record set: posting/applicant pool, interview notes, "
            "sub/absence logs, investigation file.\n"
            "2. Applicant identities are generally exempt (RCW 42.56.250(2)); "
            "finalists differ — check with counsel.\n"
            "3. Open investigations: coordinate with counsel before production."),
    },
    {
        "slug": "capital-projects", "name": "Capital Projects & Maintenance (bids, contracts, work orders)",
        "category": "Facilities", "vendor": "⚠️ VERIFY work-order system",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Director of Capital Projects / Director of Maintenance",
        "retention_days": None,
        "retention_note": "Bond-project records attract heavy PRA traffic; bids sealed "
                       "until award, then public.",
        "urgent_hold": False,
        "triage_keywords": ["bid", "construction", "change order", "bond project",
                         "work order", "contractor", "architect", "inspection report"],
        "search_checklist": (
            "1. Pull contract/bid/change-order files for the project.\n"
            "2. Work-order history export for the site if requested.\n"
            "3. Verify bid-opening status before releasing bid contents."),
    },
    {
        "slug": "safety-security", "name": "Safety & Security (Report a Concern, HIB, SRO, drills)",
        "category": "Safety", "vendor": "⚠️ VERIFY tip-line platform",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Director of Safety and Security",
        "retention_days": None,
        "retention_note": "HIB investigations involve minors — FERPA + witness privacy.",
        "urgent_hold": False,
        "triage_keywords": ["report a concern", "hib", "bullying", "harassment",
                         "tip line", "sro", "school resource officer",
                         "safety drill", "lockdown"],
        "search_checklist": (
            "1. Pull the concern/tip record and investigation file.\n"
            "2. Redact reporter/witness identities per exemptions.\n"
            "3. SRO records: check the law-enforcement MOU — some records are "
            "the agency's, not the district's (say so, don't silently omit).\n"
            "4. Second review mandatory for anything involving students."),
    },
    {
        "slug": "legal-claims", "name": "Legal / Claims / Settlements",
        "category": "Legal & risk", "vendor": "WA risk pool (verify which)",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Executive Director of Business Services",
        "retention_days": None,
        "retention_note": "Settlement agreements are PUBLIC records in WA — "
                       "confidentiality clauses do not defeat the PRA.",
        "urgent_hold": False,
        "triage_keywords": ["tort claim", "settlement", "lawsuit", "litigation",
                         "legal claim", "attorney fees"],
        "search_checklist": (
            "1. Pull claim files/settlement agreements for the matter.\n"
            "2. Attorney-client privileged material: exemption-log with counsel; "
            "the agreement itself is generally releasable.\n"
            "3. Coordinate every production in this category with counsel."),
    },
    {
        "slug": "phone-voip", "name": "Phone System (call logs, voicemail)",
        "category": "Communications", "vendor": "District VoIP platform",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Director of Technology",
        "retention_days": None, "retention_note": "⚠️ VERIFY voicemail retention window.",
        "urgent_hold": False,
        "triage_keywords": ["voicemail", "call log", "phone records", "phone call"],
        "search_checklist": (
            "1. Export call detail records for the extension/window.\n"
            "2. Retrieve voicemails if still within retention."),
    },
    {
        "slug": "social-media", "name": "District & School Social Media",
        "category": "Communications", "vendor": "Meta/X/etc. — ⚠️ VERIFY archiving tool",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "Director of Communications",
        "retention_days": None,
        "retention_note": "Posts AND comments (including deleted ones) are records; "
                       "an archiving tool is strongly recommended if absent.",
        "urgent_hold": False,
        "triage_keywords": ["facebook", "instagram", "twitter", "social media",
                         "district page", "school page"],
        "search_checklist": (
            "1. Export posts/comments for the account & window (archiving tool "
            "or platform export).\n"
            "2. Include comment moderation/deletion history if requested.\n"
            "3. Inventory school-level and team-level accounts — not just the "
            "district flagship."),
    },
    {
        "slug": "kghp-radio", "name": "KGHP Radio (FCC-licensed station)",
        "category": "Broadcast", "vendor": "District-operated",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "KGHP Station Manager",
        "retention_days": None,
        "retention_note": "FCC public inspection file has its OWN federal access regime "
                       "alongside the PRA.",
        "urgent_hold": False,
        "triage_keywords": ["kghp", "radio", "broadcast"],
        "search_checklist": (
            "1. Pull broadcast logs / program records for the window.\n"
            "2. FCC public-inspection-file items follow FCC rules — produce per "
            "both regimes."),
    },
    {
        "slug": "paper-records", "name": "Paper Records (personnel archives, cumulative folders)",
        "category": "Physical", "vendor": "n/a",
        "integration": RecordSystem.Integration.MANUAL,
        "custodian_title": "School office managers / HR records clerk",
        "retention_days": None, "retention_note": "WA LGS retention schedules govern.",
        "urgent_hold": False,
        "triage_keywords": ["cumulative folder", "paper file", "physical records",
                         "archived records"],
        "search_checklist": (
            "1. Check school-site cumulative folders and district archives.\n"
            "2. Scan responsive pages into the request file (OCR pipeline).\n"
            "3. Include paper in EVERY adequate-search attestation, even when "
            "the request sounds digital."),
    },
]


class Command(BaseCommand):
    help = "Seed/refresh the district record-system registry."

    def handle(self, *args, **opts):
        created = 0
        for spec in SYSTEMS:
            slug = spec.pop("slug")
            obj, was_created = RecordSystem.objects.get_or_create(slug=slug, defaults=spec)
            if was_created:
                created += 1
            else:
                # Preserve district-entered custodian/retention edits; refresh
                # only checklists and keywords from the seed.
                obj.triage_keywords = spec["triage_keywords"]
                obj.search_checklist = spec["search_checklist"]
                obj.save(update_fields=["triage_keywords", "search_checklist"])
            spec["slug"] = slug
        self.stdout.write(self.style.SUCCESS(
            f"Registry: {len(SYSTEMS)} systems ({created} new). District to-do: "
            "assign custodian emails, verify ⚠️ retention windows (BUS VIDEO first), "
            "formalize checklists into SOPs, then clear needs_verification."))
