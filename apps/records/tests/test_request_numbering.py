"""Request-number assignment must survive concurrent intake: two requests
saving at the same moment may compute the same next sequence; the loser's
save must retry with a fresh number instead of surfacing an IntegrityError
to the requester."""
from unittest import mock

from django.test import TestCase

from apps.records.models import MatterKind, PublicRecordsRequest, Requester


def make_requester():
    return Requester.objects.create(email="jane@example.org")


class RequestNumberingTests(TestCase):
    def test_sequential_numbers(self):
        r = make_requester()
        first = PublicRecordsRequest.objects.create(requester=r, description="a")
        second = PublicRecordsRequest.objects.create(requester=r, description="b")
        prefix = first.number.rsplit("-", 1)[0]
        self.assertTrue(first.number.startswith("PRR-"))
        self.assertEqual(first.number[-4:], "0001")
        self.assertEqual(second.number, f"{prefix}-0002")

    def test_each_matter_kind_has_its_own_prefix_and_sequence(self):
        """A litigation or FERPA number must never read as a PRA request."""
        r = make_requester()
        prr = PublicRecordsRequest.objects.create(requester=r, description="a")
        lit = PublicRecordsRequest.objects.create(
            requester=r,
            description="b",
            matter_kind=MatterKind.LITIGATION_DISCOVERY,
        )
        ferpa = PublicRecordsRequest.objects.create(
            requester=r,
            description="c",
            matter_kind=MatterKind.FERPA_PARENT,
        )

        self.assertTrue(prr.number.startswith("PRR-"))
        self.assertTrue(lit.number.startswith("LIT-"))
        self.assertTrue(ferpa.number.startswith("FER-"))
        # Independent sequences: each kind starts at 0001.
        self.assertEqual(prr.number[-4:], "0001")
        self.assertEqual(lit.number[-4:], "0001")
        self.assertEqual(ferpa.number[-4:], "0001")

    def test_save_retries_on_number_collision(self):
        r = make_requester()
        existing = PublicRecordsRequest.objects.create(requester=r, description="a")

        real_next = PublicRecordsRequest._next_number
        calls = []

        def collide_once():
            calls.append(1)
            if len(calls) == 1:
                return existing.number  # simulate a concurrent intake winning
            return real_next(existing)

        with mock.patch.object(
            PublicRecordsRequest, "_next_number", side_effect=collide_once
        ):
            second = PublicRecordsRequest.objects.create(
                requester=r, description="b"
            )

        self.assertEqual(len(calls), 2, "save() must retry after a collision")
        self.assertNotEqual(second.number, existing.number)
        self.assertTrue(
            PublicRecordsRequest.objects.filter(pk=second.pk).exists()
        )
