"""Seed the exemption taxonomy. Starter set of the exemptions a school
district cites most — expand with counsel; full list lives in RCW 42.56.230–.480
plus 'other statute' exemptions incorporated via RCW 42.56.070(1)."""
from django.core.management.base import BaseCommand

from apps.records.models import ExemptionCitation

SEED = [
    # (citation, short name, second_review)
    ("RCW 42.56.230(1)", "Personal info in student files", True),
    ("RCW 42.56.230(3)", "Personal info — public employees (privacy)", False),
    ("RCW 42.56.250(3)", "Employee personal identifying information (addresses, DOB, etc.)", False),
    ("RCW 42.56.250(2)", "Applicant/employee examination materials", False),
    ("RCW 42.56.290", "Attorney work product / controversy records", True),
    ("RCW 42.56.240(1)", "Investigative records — law enforcement nondisclosure", True),
    ("RCW 42.56.420", "Security — vulnerability assessments, emergency response plans", False),
    ("RCW 42.56.590", "Personal health information / data breach", True),
    ("RCW 5.60.060", "Privileged communications (other-statute exemption)", True),
    ("FERPA 20 U.S.C. §1232g", "Education records — personally identifiable student info", True),
    ("RCW 28A.605.030", "Student education records (state analog)", True),
    ("RCW 13.50.100", "Juvenile records", True),
    ("HIPAA 45 C.F.R. Part 164", "Protected health information (where applicable)", True),
]


class Command(BaseCommand):
    help = "Seed/refresh the exemption citation taxonomy."

    def handle(self, *args, **opts):
        created = 0
        for citation, short_name, second in SEED:
            _, was_created = ExemptionCitation.objects.update_or_create(
                citation=citation,
                defaults={"short_name": short_name, "requires_second_review": second, "active": True},
            )
            created += was_created
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(SEED)} exemptions ({created} new). "
            "REMINDER: have district counsel review before production use."
        ))
