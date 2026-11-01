"""The date a case is answerable by, after the first response has gone out.

The RCW 42.56.520 five-business-day date answers exactly one question: has the
District made its *initial* response yet. Once that response is delivered the
statutory date is spent, and a list that keeps showing it is asserting a
deadline the District no longer owes -- the bug this module fixes.

What replaces it is deliberately not statutory. For a public records request
the honest answer is the date the District promised the requester
(``estimated_completion``), and where no estimate was ever recorded, a
district working target of 60 **calendar** days from the first response. The
target is an internal service level; it is labelled as one everywhere it is
shown, and nothing here changes what RCW 42.56.520 requires or when the
watchdogs escalate.

Which matter kind each rule belongs to (ADR 0004, AGENTS.md rule 13):

* ``public_records`` -- statutory, then estimate, then district target.
* ``ferpa_parent`` -- the 34 CFR 99.10(b) access date, always.
* ``litigation_discovery`` -- none. There is no requester and no clock.
* any closed or terminal case, of any kind -- none. Nothing is owed.

The two implementations (the Python service and the queryset annotation that
sorts by it) are asserted to agree here, case by case, because a column that
shows one date and sorts by another is worse than either alone.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.records.models import (
    Channel,
    InitialResponseAttestation,
    LegacyImportBatch,
    LegacyRequestImport,
    MatterKind,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)
from apps.records.response_due import (
    DISTRICT_TARGET_CALENDAR_DAYS,
    NEXT_DUE_ASCENDING,
    DueBasis,
    annotate_next_response_due,
    next_response_due,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific(date_text, hour=9):
    """A Pacific wall-clock moment on the given day, stored as UTC."""
    day = dt.date.fromisoformat(date_text)
    return dt.datetime(day.year, day.month, day.day, hour, tzinfo=PACIFIC)


class NextResponseDueTests(TestCase):
    """One fixture per rule, each asserted through both implementations."""

    def setUp(self):
        self.requester = Requester.objects.create(
            email="requester@example.org", name="Jane Q. Public"
        )

    def make(self, **kw):
        defaults = {
            "requester": self.requester,
            "description": "Every email about the stadium project",
            "channel": Channel.PORTAL,
            "received_at": pacific("2026-09-22"),
        }
        defaults.update(kw)
        return PublicRecordsRequest.objects.create(**defaults)

    def annotated(self, prr):
        """The same answer, read back off the ordering annotation."""
        row = (
            annotate_next_response_due(PublicRecordsRequest.objects.all())
            .filter(pk=prr.pk)
            .values("_next_due", "_next_due_basis")
            .get()
        )
        basis = row["_next_due_basis"] or None
        due = row["_next_due"]
        # The district-target branch is date arithmetic, which SQLite hands
        # back as text and PostgreSQL as a timestamp. The ordering only needs
        # it to compare correctly; this assertion needs it to mean the same
        # calendar day the service named.
        if isinstance(due, str):
            due = dt.date.fromisoformat(due[:10])
        elif isinstance(due, dt.datetime):
            due = due.date()
        return due, basis

    def assertDue(self, prr, expected_date, expected_basis):
        answer = next_response_due(prr)
        self.assertEqual(answer.date, expected_date)
        self.assertEqual(answer.basis, expected_basis)
        self.assertEqual(
            self.annotated(prr),
            (expected_date, expected_basis),
            "the ordering annotation disagrees with the service",
        )

    # -- public records, before the first response ------------------------

    def test_unanswered_public_records_request_shows_the_statutory_date(self):
        prr = self.make(statutory_response_due=dt.date(2026, 9, 29))
        self.assertFalse(prr.initial_response_delivered)
        self.assertDue(prr, dt.date(2026, 9, 29), DueBasis.STATUTORY)

    # -- public records, after the first response -------------------------

    def test_acknowledged_with_an_estimate_shows_the_promised_date(self):
        prr = self.make(
            status=RequestStatus.ACKNOWLEDGED,
            statutory_response_due=dt.date(2026, 9, 29),
            acknowledgment_sent_at=pacific("2026-09-24"),
            estimated_completion=dt.date(2026, 10, 30),
        )
        self.assertDue(prr, dt.date(2026, 10, 30), DueBasis.ESTIMATE)

    def test_acknowledged_without_an_estimate_falls_to_the_district_target(self):
        # 2026-09-22 + 60 calendar days. Not the statutory date, which was
        # 2026-09-29 and is spent the moment the acknowledgment is delivered.
        prr = self.make(
            status=RequestStatus.ACKNOWLEDGED,
            statutory_response_due=dt.date(2026, 9, 29),
            acknowledgment_sent_at=pacific("2026-09-22", hour=16),
        )
        self.assertDue(prr, dt.date(2026, 11, 21), DueBasis.DISTRICT_TARGET)

    def test_the_district_target_crosses_a_month_boundary(self):
        prr = self.make(
            status=RequestStatus.ACKNOWLEDGED,
            acknowledgment_sent_at=pacific("2026-10-15"),
        )
        self.assertDue(prr, dt.date(2026, 12, 14), DueBasis.DISTRICT_TARGET)

    def test_the_district_target_counts_calendar_days_through_wa_holidays(self):
        # 2026-11-20 + 60 runs through Thanksgiving (2026-11-26), Christmas,
        # and New Year's Day. Calendar days count all three, which is the
        # whole point of not reusing the business-day engine here.
        prr = self.make(
            status=RequestStatus.GATHERING,
            acknowledgment_sent_at=pacific("2026-11-20"),
        )
        self.assertDue(prr, dt.date(2027, 1, 19), DueBasis.DISTRICT_TARGET)

    def test_the_district_target_counts_calendar_days_through_juneteenth(self):
        # Juneteenth 2026-06-19 and Memorial Day both fall inside this window.
        prr = self.make(
            status=RequestStatus.REVIEW,
            acknowledgment_sent_at=pacific("2026-05-01"),
        )
        self.assertDue(prr, dt.date(2026, 6, 30), DueBasis.DISTRICT_TARGET)

    def test_a_late_evening_acknowledgment_counts_from_its_pacific_day(self):
        # 23:30 Pacific on 2026-10-15 is 2026-10-16 in UTC. The target is a
        # wall-clock concept, so it runs from the Pacific day.
        prr = self.make(
            status=RequestStatus.ACKNOWLEDGED,
            acknowledgment_sent_at=pacific("2026-10-15", hour=23),
        )
        self.assertDue(prr, dt.date(2026, 12, 14), DueBasis.DISTRICT_TARGET)

    def test_a_clarification_status_event_is_a_delivered_first_response(self):
        prr = self.make(statutory_response_due=dt.date(2026, 9, 29))
        StatusEvent.objects.create(
            request=prr,
            from_status=RequestStatus.RECEIVED,
            to_status=RequestStatus.CLARIFICATION,
            note="Asked which schools",
            occurred_at=pacific("2026-09-25"),
        )
        prr.refresh_from_db()
        self.assertTrue(prr.initial_response_delivered)
        self.assertDue(prr, dt.date(2026, 11, 24), DueBasis.DISTRICT_TARGET)

    def test_an_external_initial_response_attestation_starts_the_target(self):
        officer = get_user_model().objects.create_user(
            username="officer", password="x"
        )
        prr = self.make(statutory_response_due=dt.date(2026, 9, 29))
        InitialResponseAttestation.objects.create(
            request=prr,
            sent_via=InitialResponseAttestation.SentVia.NEXTREQUEST,
            responded_on=dt.date(2026, 9, 23),
            evidence="NextRequest export row 42 shows the response sent.",
            attested_by=officer,
        )
        prr.refresh_from_db()
        self.assertDue(prr, dt.date(2026, 11, 22), DueBasis.DISTRICT_TARGET)

    def test_an_undated_attestation_yields_no_date_rather_than_a_guess(self):
        officer = get_user_model().objects.create_user(
            username="officer2", password="x"
        )
        prr = self.make(statutory_response_due=dt.date(2026, 9, 29))
        InitialResponseAttestation.objects.create(
            request=prr,
            sent_via=InitialResponseAttestation.SentVia.MAIL,
            responded_on=None,
            evidence="Officer states the letter preceded the cutover.",
            attested_by=officer,
        )
        prr.refresh_from_db()
        self.assertTrue(prr.initial_response_delivered)
        # The statutory date is spent and no delivery date is known. Inventing
        # one from the row's own creation timestamp would put a target on the
        # case that nobody ever promised.
        self.assertDue(prr, None, None)

    def test_a_legacy_initial_response_starts_the_target(self):
        prr = self.make(statutory_response_due=dt.date(2026, 9, 29))
        importer = get_user_model().objects.create_user(
            username="importer", password="x"
        )
        batch = LegacyImportBatch.objects.create(
            source_sha256="a" * 64,
            mapping_sha256="b" * 64,
            plan_sha256="c" * 64,
            row_count=1,
            mapping_snapshot={},
            imported_by=importer,
        )
        LegacyRequestImport.objects.create(
            batch=batch,
            request=prr,
            source_id="nr-25-42",
            source_number="25-42",
            source_status="closed",
            source_row_sha256="d" * 64,
            legacy_initial_response_at=pacific("2026-09-23"),
        )
        prr.refresh_from_db()
        self.assertDue(prr, dt.date(2026, 11, 22), DueBasis.DISTRICT_TARGET)

    # -- the other matter kinds -------------------------------------------

    def test_a_ferpa_matter_always_shows_its_access_date(self):
        prr = self.make(
            matter_kind=MatterKind.FERPA_PARENT,
            statutory_response_due=None,
            ferpa_access_due=dt.date(2026, 11, 6),
        )
        self.assertDue(prr, dt.date(2026, 11, 6), DueBasis.FERPA)

    def test_a_ferpa_matter_with_an_estimate_still_shows_its_access_date(self):
        prr = self.make(
            matter_kind=MatterKind.FERPA_PARENT,
            status=RequestStatus.GATHERING,
            ferpa_access_due=dt.date(2026, 11, 6),
            estimated_completion=dt.date(2026, 10, 1),
        )
        self.assertDue(prr, dt.date(2026, 11, 6), DueBasis.FERPA)

    def test_a_litigation_matter_owes_no_response_date(self):
        prr = self.make(
            matter_kind=MatterKind.LITIGATION_DISCOVERY,
            statutory_response_due=None,
            estimated_completion=dt.date(2026, 10, 1),
        )
        self.assertDue(prr, None, None)

    # -- closed cases ------------------------------------------------------

    def test_a_closed_case_owes_nothing(self):
        prr = self.make(
            status=RequestStatus.FULFILLED,
            statutory_response_due=dt.date(2026, 9, 29),
            estimated_completion=dt.date(2026, 10, 30),
            closed_at=timezone.now(),
        )
        self.assertDue(prr, None, None)

    def test_a_denied_case_owes_nothing(self):
        prr = self.make(
            status=RequestStatus.DENIED,
            statutory_response_due=dt.date(2026, 9, 29),
        )
        self.assertDue(prr, None, None)

    def test_a_closed_ferpa_matter_owes_nothing(self):
        prr = self.make(
            matter_kind=MatterKind.FERPA_PARENT,
            status=RequestStatus.CLOSED_WITHDRAWN,
            ferpa_access_due=dt.date(2026, 11, 6),
        )
        self.assertDue(prr, None, None)

    # -- the window itself -------------------------------------------------

    def test_the_district_target_window_is_sixty_calendar_days(self):
        self.assertEqual(DISTRICT_TARGET_CALENDAR_DAYS, 60)


class NextResponseDueOrderingTests(TestCase):
    """Sorting by the column must order by what the column shows."""

    def test_soonest_due_first_across_every_basis(self):
        requester = Requester.objects.create(email="r@example.org", name="R")

        def make(number_hint, **kw):
            defaults = {
                "requester": requester,
                "description": f"Request {number_hint}",
                "received_at": pacific("2026-09-01"),
            }
            defaults.update(kw)
            return PublicRecordsRequest.objects.create(**defaults)

        statutory = make("statutory", statutory_response_due=dt.date(2026, 9, 8))
        ferpa = make(
            "ferpa",
            matter_kind=MatterKind.FERPA_PARENT,
            ferpa_access_due=dt.date(2026, 10, 16),
        )
        estimate = make(
            "estimate",
            status=RequestStatus.ACKNOWLEDGED,
            acknowledgment_sent_at=pacific("2026-09-03"),
            estimated_completion=dt.date(2026, 9, 30),
        )
        target = make(
            "target",
            status=RequestStatus.ACKNOWLEDGED,
            acknowledgment_sent_at=pacific("2026-09-03"),
        )  # 2026-11-02
        litigation = make(
            "litigation",
            matter_kind=MatterKind.LITIGATION_DISCOVERY,
            estimated_completion=dt.date(2026, 9, 2),
        )

        ordered = list(
            annotate_next_response_due(PublicRecordsRequest.objects.all())
            .order_by(*NEXT_DUE_ASCENDING)
            .values_list("pk", flat=True)
        )
        # A litigation matter has no response date at all and sorts last, even
        # though its estimate is the earliest date in the fixture: the column
        # orders by what the column shows.
        self.assertEqual(
            ordered,
            [statutory.pk, estimate.pk, ferpa.pk, target.pk, litigation.pk],
        )
