"""ADR 0004 matter kinds — the invariants, proved against the real querysets.

Three things must be true, and each is asserted here against the production
code path rather than a re-implementation of it:

1. the day-5 safety net sees only ``public_records``;
2. the RCW 42.56.520 clock is computed, tolled, and watched only for
   ``public_records``, and a FERPA matter gets its own 45-calendar-day clock
   instead;
3. JLARC counts only ``public_records``.

Everything else here guards the edges that would let one of those three leak:
the public form, the intake mailbox, the letters each kind may send, merging,
and the immutability of the kind itself.
"""
import datetime as dt
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.api.identity import StaffRole, apply_staff_roles
from apps.correspondence import services as correspondence_services
from apps.correspondence.models import Correspondence
from apps.records import jlarc, services, tasks
from apps.records.deadlines import (
    FERPA_ACCESS_WINDOW_CALENDAR_DAYS,
    ferpa_access_due,
    statutory_response_due,
)
from apps.records.intake import submit_intake
from apps.records.models import (
    Channel,
    FerpaIdentityVerification,
    FerpaIdentityVerificationMethod,
    MatterKind,
    PublicRecordsRequest,
    Requester,
    RequestStatus,
    StatusEvent,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


def make_matter(kind=MatterKind.PUBLIC_RECORDS, **kw):
    requester = Requester.objects.create(
        email="parent@example.org", name="Jane Q. Public"
    )
    defaults = {
        "requester": requester,
        "description": "Everything about the stadium project",
        "matter_kind": kind,
    }
    defaults.update(kw)
    return PublicRecordsRequest.objects.create(**defaults)


def intake_payload(**kw):
    payload = {
        "description": "The attendance records for my child",
        "email": "parent@example.org",
        "name": "Jane Q. Public",
    }
    payload.update(kw)
    return payload


class MatterKindFieldTests(TestCase):
    def test_existing_behaviour_is_a_public_records_request(self):
        self.assertEqual(make_matter().matter_kind, MatterKind.PUBLIC_RECORDS)

    def test_kind_is_immutable_after_intake(self):
        prr = make_matter()
        prr.matter_kind = MatterKind.FERPA_PARENT
        with self.assertRaises(ValidationError):
            prr.save()

    def test_kind_is_immutable_through_update_fields(self):
        prr = make_matter()
        prr.matter_kind = MatterKind.LITIGATION_DISCOVERY
        with self.assertRaises(ValidationError):
            prr.save(update_fields=["matter_kind"])

    def test_kind_is_immutable_in_bulk(self):
        make_matter()
        with self.assertRaises(ValidationError):
            PublicRecordsRequest.objects.update(
                matter_kind=MatterKind.LITIGATION_DISCOVERY
            )

    def test_database_rejects_a_matter_kind_rewrite_behind_the_orm(self):
        """The guard is in the schema too, not only in the model."""
        prr = make_matter()
        bare_queryset = models.QuerySet(
            model=PublicRecordsRequest, using="default"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            bare_queryset.filter(pk=prr.pk).update(
                matter_kind=MatterKind.LITIGATION_DISCOVERY
            )

    def test_unrelated_saves_still_work(self):
        """The guard must not block ordinary lifecycle writes."""
        prr = make_matter()
        prr.estimated_completion = timezone.localdate()
        prr.save(update_fields=["estimated_completion"])
        prr.refresh_from_db()
        self.assertIsNotNone(prr.estimated_completion)

    def test_public_records_queryset_helper_excludes_other_kinds(self):
        public = make_matter()
        make_matter(MatterKind.LITIGATION_DISCOVERY)
        make_matter(MatterKind.FERPA_PARENT)
        self.assertEqual(
            list(PublicRecordsRequest.objects.public_records()), [public]
        )


class CaseNumberRecognitionTests(SimpleTestCase):
    """Anything that recognises a case number must know all three prefixes."""

    def test_inbound_email_matching_recognises_every_prefix(self):
        from apps.correspondence.inbound import _REQUEST_NUMBER

        for number in ("PRR-2026-0042", "LIT-2026-0003", "FER-2026-0001"):
            with self.subTest(number=number):
                self.assertEqual(
                    _REQUEST_NUMBER.findall(f"Re: my request {number}, thanks"),
                    [number],
                )

    def test_the_mcp_client_accepts_every_prefix(self):
        from integrations.mcp.client import _REQUEST_NUMBER

        for number in ("PRR-2026-0042", "LIT-2026-0003", "FER-2026-0001"):
            with self.subTest(number=number):
                self.assertIsNotNone(_REQUEST_NUMBER.match(number))
        self.assertIsNone(_REQUEST_NUMBER.match("XXX-2026-0001"))


@override_settings(CELERY_TASK_ALWAYS_EAGER=True)
class ClockInvariantTests(TestCase):
    """Invariant 2: the RCW 42.56.520 clock sees only public records."""

    def test_public_records_intake_computes_the_statutory_clock(self):
        prr = submit_intake(intake_payload(), channel=Channel.PORTAL)
        prr.refresh_from_db()
        self.assertEqual(
            prr.statutory_response_due,
            statutory_response_due(prr.received_at),
        )
        self.assertIsNone(prr.ferpa_access_due)

    def test_litigation_intake_computes_no_clock_at_all(self):
        prr = submit_intake(
            intake_payload(),
            channel=Channel.PHONE,
            matter_kind=MatterKind.LITIGATION_DISCOVERY,
        )
        prr.refresh_from_db()
        self.assertIsNone(prr.statutory_response_due)
        self.assertIsNone(prr.ferpa_access_due)

    def test_ferpa_intake_computes_45_calendar_days_and_no_rcw_clock(self):
        prr = submit_intake(
            intake_payload(),
            channel=Channel.IN_PERSON,
            matter_kind=MatterKind.FERPA_PARENT,
        )
        prr.refresh_from_db()
        self.assertIsNone(prr.statutory_response_due)
        self.assertEqual(
            prr.ferpa_access_due,
            timezone.localtime(prr.received_at).date()
            + dt.timedelta(days=FERPA_ACCESS_WINDOW_CALENDAR_DAYS),
        )

    def test_ferpa_clock_is_calendar_days_over_a_holiday_and_weekend(self):
        """34 CFR 99.10(b) counts calendar days.

        2026-06-01 is a Monday; +45 calendar days is 2026-07-16, a Thursday.
        The window spans Juneteenth (2026-06-19) and observed July 4th
        (2026-07-03) and neither moves it, which is exactly how it differs
        from the RCW 42.56.520 business-day engine.
        """
        received = timezone.make_aware(dt.datetime(2026, 6, 1, 9, 0))
        self.assertEqual(ferpa_access_due(received), dt.date(2026, 7, 16))

    def test_deadline_task_refuses_to_clock_a_non_public_records_matter(self):
        """Defence in depth: the task itself checks, whoever calls it."""
        for kind in (
            MatterKind.LITIGATION_DISCOVERY,
            MatterKind.FERPA_PARENT,
        ):
            with self.subTest(kind=kind):
                prr = make_matter(kind)
                tasks.compute_statutory_deadline(str(prr.id))
                prr.refresh_from_db()
                self.assertIsNone(prr.statutory_response_due)

    def test_clarification_tolling_never_writes_an_rcw_clock(self):
        """Leaving CLARIFICATION recomputes the five-day deadline.

        The letters that open a tolled clarification are PRA-only, so a
        non-public-records matter should never reach this branch at all. If a
        future path ever puts one there anyway, the branch must still refuse
        to invent a clock the matter never owed — so the dedicated-exit
        gate is stubbed here to reach it deliberately.
        """
        for kind in (
            MatterKind.LITIGATION_DISCOVERY,
            MatterKind.FERPA_PARENT,
        ):
            with self.subTest(kind=kind):
                prr = make_matter(kind)
                StatusEvent.objects.create(
                    request=prr,
                    to_status=RequestStatus.CLARIFICATION,
                    occurred_at=timezone.now() - dt.timedelta(days=7),
                )
                PublicRecordsRequest.objects.filter(pk=prr.pk).update(
                    status=RequestStatus.CLARIFICATION
                )
                prr.refresh_from_db()

                with mock.patch.object(
                    services, "_require_clarification_exit_evidence"
                ):
                    services.transition(prr, RequestStatus.GATHERING)

                prr.refresh_from_db()
                self.assertIsNone(prr.statutory_response_due)
                self.assertEqual(prr.tolled_business_days, 0)

    def test_clarification_tolling_still_works_for_public_records(self):
        """The same branch must keep tolling an actual RCW 42.56 request."""
        prr = make_matter(
            statutory_response_due=timezone.localdate(),
            received_at=timezone.now() - dt.timedelta(days=30),
        )
        StatusEvent.objects.create(
            request=prr,
            to_status=RequestStatus.CLARIFICATION,
            occurred_at=timezone.now() - dt.timedelta(days=14),
        )
        PublicRecordsRequest.objects.filter(pk=prr.pk).update(
            status=RequestStatus.CLARIFICATION
        )
        prr.refresh_from_db()

        with mock.patch.object(
            services, "_require_clarification_exit_evidence"
        ):
            services.transition(prr, RequestStatus.GATHERING)

        prr.refresh_from_db()
        self.assertGreater(prr.tolled_business_days, 0)
        self.assertEqual(
            prr.statutory_response_due,
            statutory_response_due(prr.received_at, prr.tolled_business_days),
        )

    def test_deadline_watchdog_skips_other_kinds(self):
        overdue = timezone.localdate() - dt.timedelta(days=3)
        make_matter(
            MatterKind.LITIGATION_DISCOVERY, statutory_response_due=overdue
        )
        make_matter(MatterKind.FERPA_PARENT, statutory_response_due=overdue)

        with mock.patch("apps.records.tasks._notify") as notify:
            tasks.deadline_watchdog()

        notify.assert_not_called()

    def test_deadline_watchdog_still_fires_for_a_public_records_request(self):
        make_matter(
            statutory_response_due=timezone.localdate() - dt.timedelta(days=3)
        )
        with mock.patch("apps.records.tasks._notify") as notify:
            tasks.deadline_watchdog()
        self.assertEqual(notify.call_count, 1)


@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    AUTO_ACK_SAFETY_NET=True,
    PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED=True,
)
class SafetyNetInvariantTests(TestCase):
    """Invariant 1: the day-5 safety net sees only public records."""

    def test_no_automated_acknowledgment_for_litigation_or_ferpa(self):
        long_past = timezone.localdate() - dt.timedelta(days=90)
        make_matter(
            MatterKind.LITIGATION_DISCOVERY, statutory_response_due=long_past
        )
        make_matter(MatterKind.FERPA_PARENT, statutory_response_due=long_past)

        with mock.patch.object(
            correspondence_services, "queue_automated_acknowledgment"
        ) as queue:
            tasks.auto_acknowledge_safety_net()

        queue.assert_not_called()
        self.assertFalse(
            Correspondence.objects.filter(
                kind=Correspondence.Kind.ACKNOWLEDGMENT
            ).exists()
        )

    def test_a_public_records_request_is_still_caught(self):
        make_matter(
            statutory_response_due=timezone.localdate() - dt.timedelta(days=1)
        )
        with mock.patch.object(
            correspondence_services, "queue_automated_acknowledgment"
        ) as queue:
            tasks.auto_acknowledge_safety_net()
        self.assertEqual(queue.call_count, 1)


class JlarcInvariantTests(TestCase):
    """Invariant 3: JLARC counts only public records."""

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            username="jlarc-officer",
            email="jlarc.officer@psd401.net",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})

    def test_report_counts_only_public_records_matters(self):
        received = dt.datetime(2025, 3, 3, 10, tzinfo=PACIFIC)
        closed = dt.datetime(2025, 3, 5, 10, tzinfo=PACIFIC)
        for kind in MatterKind.values:
            make_matter(
                kind,
                received_at=received,
                closed_at=closed,
                status=RequestStatus.FULFILLED,
            )
        jlarc.record_jlarc_annual_supplement(
            2025,
            actor=self.officer,
            methodology_notes=(
                "Year-end ledgers; only public records matters are reported."
            ),
        )

        report = jlarc.build_jlarc_report(2025)

        # Three matters exist in the period; only the RCW 42.56 one is the
        # District's business to report under RCW 40.14.026.
        self.assertEqual(report["baseline"]["received_during_period"], 1)
        self.assertEqual(report["baseline"]["total_closed_during_period"], 1)
        self.assertEqual(len(report["request_log"]), 1)
        self.assertTrue(
            report["request_log"][0]["request_number"].startswith("PRR-"),
        )

    def test_classification_refuses_a_non_public_records_matter(self):
        for kind in (
            MatterKind.LITIGATION_DISCOVERY,
            MatterKind.FERPA_PARENT,
        ):
            with self.subTest(kind=kind):
                prr = make_matter(kind)
                with self.assertRaises(ValidationError):
                    jlarc.classify_request_for_jlarc(
                        prr.pk,
                        actor=self.officer,
                        requester_type="individual",
                        fulfillment_format="electronic",
                        scanned_physical=False,
                        records_produced_description="n/a",
                        records_withheld_description="n/a",
                        reason="Confirming the kind is refused outright.",
                    )


class IntakeBoundaryTests(TestCase):
    """Nothing a requester reaches on their own may declare its own law."""

    def test_public_form_cannot_open_a_ferpa_or_litigation_matter(self):
        for kind in (
            MatterKind.LITIGATION_DISCOVERY,
            MatterKind.FERPA_PARENT,
        ):
            for channel in (Channel.PORTAL, Channel.EMAIL):
                with self.subTest(kind=kind, channel=channel):
                    with self.assertRaises(ValidationError):
                        submit_intake(
                            intake_payload(),
                            channel=channel,
                            matter_kind=kind,
                        )
        self.assertFalse(PublicRecordsRequest.objects.exists())

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValidationError):
            submit_intake(
                intake_payload(),
                channel=Channel.PHONE,
                matter_kind="whatever_counsel_called_it",
            )

    @override_settings(CELERY_TASK_ALWAYS_EAGER=True)
    def test_a_staff_channel_may_open_every_kind(self):
        for kind in MatterKind.values:
            with self.subTest(kind=kind):
                prr = submit_intake(
                    intake_payload(),
                    channel=Channel.MAIL,
                    matter_kind=kind,
                )
                self.assertEqual(prr.matter_kind, kind)


class CorrespondenceBoundaryTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            username="corr-officer",
            email="corr.officer@psd401.net",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})

    def _check(self, prr, kind):
        correspondence_services._require_kind_allows_correspondence(prr, kind)

    def test_litigation_sends_no_requester_correspondence_at_all(self):
        prr = make_matter(MatterKind.LITIGATION_DISCOVERY)
        for kind in Correspondence.Kind.values:
            with self.subTest(kind=kind):
                with self.assertRaises(ValidationError):
                    self._check(prr, kind)

    def test_ferpa_cannot_send_a_letter_that_asserts_rcw_42_56(self):
        prr = make_matter(MatterKind.FERPA_PARENT)
        for kind in sorted(
            correspondence_services.PRA_STATUTORY_CORRESPONDENCE_KINDS
        ):
            with self.subTest(kind=kind):
                with self.assertRaises(ValidationError):
                    self._check(prr, kind)

    def test_ferpa_may_still_hand_a_parent_their_records(self):  # no-assert: _check raises on a refused kind
        prr = make_matter(MatterKind.FERPA_PARENT)
        for kind in (
            Correspondence.Kind.SUBMISSION_RECEIPT,
            Correspondence.Kind.RESPONSIVE_RECORDS_NOTICE,
            Correspondence.Kind.INSTALLMENT_NOTICE,
            Correspondence.Kind.COMPLETION_CLOSURE,
            Correspondence.Kind.GENERAL,
        ):
            with self.subTest(kind=kind):
                self._check(prr, kind)  # must not raise

    def test_public_records_may_send_everything_it_could_before(self):  # no-assert: _check raises on a refused kind
        prr = make_matter()
        for kind in Correspondence.Kind.values:
            with self.subTest(kind=kind):
                self._check(prr, kind)  # must not raise


class FerpaIdentityVisibilityTests(TestCase):
    """Knowing identity was verified is operational. The note is not."""

    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            username="ferpa-read-officer",
            email="ferpa.read.officer@psd401.net",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = get_user_model().objects.create_user(
            username="ferpa-read-reviewer",
            email="ferpa.read.reviewer@psd401.net",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.prr = make_matter(MatterKind.FERPA_PARENT)
        self.note = "Parenting plan filed 2026-02-11 names the requester."
        services.record_ferpa_identity_verification(
            self.prr,
            actor=self.officer,
            method=FerpaIdentityVerificationMethod.COURT_ORDER,
            note=self.note,
        )

    def _detail(self, user):
        client = Client()
        client.force_login(user)
        response = client.get(f"/api/staff/requests/{self.prr.number}/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["ferpa_identity_verification"]

    def test_the_records_officer_reads_the_whole_record(self):
        payload = self._detail(self.officer)
        self.assertEqual(payload["note"], self.note)
        self.assertEqual(payload["verified_by"], self.officer.username)

    def test_another_workflow_role_sees_that_it_happened_but_not_the_note(self):
        payload = self._detail(self.reviewer)
        self.assertIsNone(payload["note"])
        # The fact, the method, and the accountable name still read, because
        # they are what says disclosure is allowed at all.
        self.assertEqual(
            payload["method"], FerpaIdentityVerificationMethod.COURT_ORDER
        )
        self.assertEqual(payload["verified_by"], self.officer.username)
        self.assertIsNotNone(payload["verified_at"])


class MergeBoundaryTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            username="merge-officer",
            email="merge.officer@psd401.net",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})

    def test_cases_governed_by_different_law_cannot_merge(self):
        source = make_matter(MatterKind.FERPA_PARENT)
        target = make_matter()
        with self.assertRaises(ValidationError):
            services.merge_requests(
                source=source,
                target=target,
                actor=self.officer,
                reason="These look like the same set of records to me.",
            )
        source.refresh_from_db()
        self.assertNotEqual(source.status, RequestStatus.CLOSED_DUPLICATE)


class FerpaIdentityVerificationTests(TestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            username="ferpa-officer",
            email="ferpa.officer@psd401.net",
            password="pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})

    def test_records_who_verified_what_and_how(self):
        prr = make_matter(MatterKind.FERPA_PARENT)
        services.record_ferpa_identity_verification(
            prr,
            actor=self.officer,
            method=FerpaIdentityVerificationMethod.IN_PERSON_PHOTO_ID,
            note="Washington driver licence checked at the district office.",
        )
        verification = FerpaIdentityVerification.objects.get(request=prr)
        self.assertIsNotNone(verification.verified_at)
        self.assertEqual(verification.verified_by, self.officer)
        self.assertEqual(
            verification.method,
            FerpaIdentityVerificationMethod.IN_PERSON_PHOTO_ID,
        )
        self.assertTrue(
            prr.events.filter(note__icontains="34 CFR 99.10").exists()
        )

    def test_it_starts_no_clock_and_changes_no_status(self):
        prr = make_matter(MatterKind.FERPA_PARENT)
        services.record_ferpa_identity_verification(
            prr,
            actor=self.officer,
            method=FerpaIdentityVerificationMethod.COURT_ORDER,
            note="Parenting plan filed 2026-02-11 names the requester.",
        )
        prr.refresh_from_db()
        self.assertEqual(prr.status, RequestStatus.RECEIVED)
        self.assertIsNone(prr.statutory_response_due)
        self.assertFalse(Correspondence.objects.filter(request=prr).exists())

    def test_a_thin_note_is_refused(self):
        prr = make_matter(MatterKind.FERPA_PARENT)
        with self.assertRaises(ValidationError):
            services.record_ferpa_identity_verification(
                prr,
                actor=self.officer,
                method=FerpaIdentityVerificationMethod.NOTARIZED_STATEMENT,
                note="ok",
            )

    def test_it_does_not_apply_to_other_kinds(self):
        for kind in (
            MatterKind.PUBLIC_RECORDS,
            MatterKind.LITIGATION_DISCOVERY,
        ):
            with self.subTest(kind=kind):
                prr = make_matter(kind)
                with self.assertRaises(ValidationError):
                    services.record_ferpa_identity_verification(
                        prr,
                        actor=self.officer,
                        method=(
                            FerpaIdentityVerificationMethod.IN_PERSON_PHOTO_ID
                        ),
                        note="Photo identification checked at the front desk.",
                    )

    def test_it_is_recorded_once(self):
        prr = make_matter(MatterKind.FERPA_PARENT)
        kwargs = {
            "actor": self.officer,
            "method": FerpaIdentityVerificationMethod.DISTRICT_RECORDS_MATCH,
            "note": "Guardian of record in PowerSchool for the student.",
        }
        services.record_ferpa_identity_verification(prr, **kwargs)
        prr.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.record_ferpa_identity_verification(prr, **kwargs)

    def _verified(self):
        prr = make_matter(MatterKind.FERPA_PARENT)
        services.record_ferpa_identity_verification(
            prr,
            actor=self.officer,
            method=FerpaIdentityVerificationMethod.COURT_ORDER,
            note="Parenting plan filed 2026-02-11 names the requester.",
        )
        return FerpaIdentityVerification.objects.get(request=prr)

    def test_the_verification_cannot_be_rewritten_or_erased_through_the_orm(self):
        verification = self._verified()
        verification.note = "Something else entirely, after the fact."
        with self.assertRaises(ValidationError):
            verification.save()
        with self.assertRaises(ValidationError):
            verification.delete()
        with self.assertRaises(ValidationError):
            FerpaIdentityVerification.objects.filter(
                pk=verification.pk
            ).update(note="Rewritten below the service layer.")
        with self.assertRaises(ValidationError):
            FerpaIdentityVerification.objects.filter(pk=verification.pk).delete()

    def test_the_database_refuses_a_rewrite_or_erasure_below_the_orm(self):
        """A raw queryset bypasses the append-only manager; the trigger holds."""
        verification = self._verified()
        bare = models.QuerySet(model=FerpaIdentityVerification, using="default")
        with self.assertRaises(IntegrityError), transaction.atomic():
            bare.filter(pk=verification.pk).update(
                note="Rewritten below the ORM, with no audit trail."
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            bare.filter(pk=verification.pk)._raw_delete("default")
        verification.refresh_from_db()
        self.assertEqual(
            verification.note,
            "Parenting plan filed 2026-02-11 names the requester.",
        )

    def test_the_database_requires_a_note_and_a_known_method(self):
        prr = make_matter(MatterKind.FERPA_PARENT)
        for fields in (
            {"method": FerpaIdentityVerificationMethod.COURT_ORDER, "note": ""},
            {"method": "a_hunch", "note": "Looked like the right person."},
        ):
            with self.subTest(**fields), self.assertRaises(IntegrityError), \
                    transaction.atomic():
                FerpaIdentityVerification.objects.create(
                    request=prr, verified_by=self.officer, **fields
                )
