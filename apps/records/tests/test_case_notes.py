"""Internal case notes: append-only, scoped, audited, and never sent.

Staff working a case need a running record of what they did and what they
were told. Until now that lived in email, which means the next person to open
the case could not see it.

Two properties carry the weight here, and both are asserted against the real
production paths rather than described:

1. **A note never reaches the requester.** No letter, production manifest, or
   export reads ``CaseNote``. The guard below walks the actual correspondence
   and production modules rather than trusting a docstring.
2. **A note is never rewritten or erased.** It is evidence of what somebody
   knew and when, so the ORM refuses, and so does the database.

"Internal" is not "secret": a note about a public records request is very
likely itself a disclosable public record. What is guaranteed is that it is
not part of *this* case's response to *this* requester.
"""

import ast
import inspect
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.api.identity import apply_staff_roles
from apps.api.models import StaffRole
from apps.audit.models import AuditLog
from apps.records import services
from apps.records.models import (
    CASE_NOTE_MAX_LENGTH,
    CaseNote,
    MatterKind,
    PublicRecordsRequest,
    Requester,
)


def make_case(**kw):
    requester = Requester.objects.create(email="requester@example.org")
    defaults = {
        "requester": requester,
        "description": "Every email about the stadium project",
    }
    defaults.update(kw)
    return PublicRecordsRequest.objects.create(**defaults)


class AddCaseNoteTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "note-officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = User.objects.create_user(
            "note-reviewer", "reviewer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.outsider = User.objects.create_user(
            "note-outsider", "outsider@example.org", "pw", is_staff=False
        )
        self.prr = make_case()

    def test_a_note_is_written_under_its_authors_name(self):
        note = services.add_case_note(
            self.prr,
            actor=self.officer,
            body="Called the transportation custodian; video pulled Tuesday.",
        )

        self.assertEqual(note.request, self.prr)
        self.assertEqual(note.author, self.officer)
        self.assertEqual(
            note.body,
            "Called the transportation custodian; video pulled Tuesday.",
        )

    def test_every_workflow_role_may_write_one(self):
        note = services.add_case_note(
            self.prr, actor=self.reviewer, body="Second review finished."
        )
        self.assertEqual(note.author, self.reviewer)

    def test_a_non_staff_identity_may_not(self):
        with self.assertRaises(PermissionDenied):
            services.add_case_note(
                self.prr, actor=self.outsider, body="Should not land."
            )
        self.assertFalse(CaseNote.objects.exists())

    def test_an_empty_note_is_refused(self):
        for body in ("", "   ", "\n\t "):
            with self.subTest(body=repr(body)):
                with self.assertRaises(ValidationError):
                    services.add_case_note(
                        self.prr, actor=self.officer, body=body
                    )
        self.assertFalse(CaseNote.objects.exists())

    def test_an_overlong_note_is_refused(self):
        with self.assertRaises(ValidationError):
            services.add_case_note(
                self.prr,
                actor=self.officer,
                body="x" * (CASE_NOTE_MAX_LENGTH + 1),
            )

    def test_a_note_is_audited_without_its_text(self):
        services.add_case_note(
            self.prr,
            actor=self.officer,
            body="The requester's lawyer called about the September video.",
        )

        entry = AuditLog.objects.get(action="case_note.added")
        self.assertEqual(entry.target, self.prr.number)
        self.assertEqual(entry.actor, self.officer)
        # The audit log is read by people who are not necessarily authorized
        # to read the case, so it records the shape and not the words.
        self.assertNotIn("lawyer", str(entry.detail))
        self.assertEqual(entry.detail["body_length"], 56)

    def test_notes_apply_to_every_matter_kind(self):
        for kind in MatterKind.values:
            with self.subTest(kind=kind):
                case = make_case(matter_kind=kind)
                note = services.add_case_note(
                    case, actor=self.officer, body=f"Working the {kind} matter."
                )
                self.assertEqual(note.request, case)

    def test_the_case_itself_is_untouched(self):
        before = PublicRecordsRequest.objects.values().get(pk=self.prr.pk)

        services.add_case_note(
            self.prr, actor=self.officer, body="Spoke to the custodian."
        )

        after = PublicRecordsRequest.objects.values().get(pk=self.prr.pk)
        self.assertEqual(before, after)
        self.assertEqual(self.prr.events.count(), 0)
        self.assertEqual(self.prr.correspondence.count(), 0)


class CaseNoteAppendOnlyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.officer = User.objects.create_user(
            "append-officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.prr = make_case()
        self.note = services.add_case_note(
            self.prr, actor=self.officer, body="The first thing I was told."
        )

    def test_the_orm_refuses_to_rewrite_one(self):
        self.note.body = "Something else entirely"
        with self.assertRaises(ValidationError):
            self.note.save()

    def test_the_orm_refuses_to_delete_one(self):
        with self.assertRaises(ValidationError):
            self.note.delete()

    def test_the_orm_refuses_a_queryset_update_or_delete(self):
        with self.assertRaises(ValidationError):
            CaseNote.objects.filter(pk=self.note.pk).update(body="rewritten")
        with self.assertRaises(ValidationError):
            CaseNote.objects.filter(pk=self.note.pk).delete()

    def test_the_database_refuses_even_when_the_orm_is_bypassed(self):
        """The ORM guard is a convenience; the trigger is the guarantee.

        No WHERE clause on purpose. The table holds exactly this one note, and
        matching a UUID primary key in raw SQL is backend-specific enough
        (SQLite stores it as undashed hex) that a mistyped predicate would
        match nothing and let this test pass without a trigger existing.
        """
        self.assertEqual(CaseNote.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            with transaction.get_connection().cursor() as cursor:
                cursor.execute(
                    "UPDATE records_casenote SET body = %s", ["rewritten"]
                )
        with self.assertRaises(IntegrityError), transaction.atomic():
            with transaction.get_connection().cursor() as cursor:
                cursor.execute("DELETE FROM records_casenote")
        self.note.refresh_from_db()
        self.assertEqual(self.note.body, "The first thing I was told.")

    def test_a_correction_is_another_note(self):
        correction = services.add_case_note(
            self.prr,
            actor=self.officer,
            body="Correcting the note above: it was Wednesday, not Tuesday.",
        )
        self.assertEqual(
            list(CaseNote.objects.values_list("pk", flat=True)),
            [self.note.pk, correction.pk],
        )


class CaseNoteStaysInternalTests(TestCase):
    """A note must not be reachable from anything a requester receives."""

    def test_no_requester_facing_module_reads_case_notes(self):
        """The guard walks the real modules rather than trusting a docstring.

        These are the paths by which bytes leave the District: the letter
        templates' render inputs, the correspondence drafting and delivery
        services, the exemption log and production manifest, and the
        installment release. If ``CaseNote`` or ``case_notes`` ever appears in
        one of them, an internal note has become part of a response.
        """
        base = Path(settings.BASE_DIR)
        requester_facing = [
            base / "apps" / "records" / "letters.py",
            base / "apps" / "correspondence" / "services.py",
            base / "apps" / "records" / "exemption_logs.py",
        ]
        for extra in (
            base / "apps" / "records" / "installments.py",
            base / "apps" / "records" / "productions.py",
            base / "apps" / "records" / "manifests.py",
        ):
            if extra.exists():
                requester_facing.append(extra)  # noqa: PERF401 - reads clearer as a loop
        checked = 0
        for path in requester_facing:
            if not path.exists():
                continue
            checked += 1
            source = path.read_text()
            with self.subTest(path=path.name):
                self.assertNotIn("CaseNote", source)
                self.assertNotIn("case_notes", source)
        # A guard that silently checked nothing would be worse than none.
        self.assertGreaterEqual(checked, 2)

    def test_no_correspondence_template_renders_a_note(self):
        templates = Path(settings.BASE_DIR) / "templates" / "correspondence"
        rendered = list(templates.rglob("*"))
        self.assertTrue(rendered)
        for path in rendered:
            if not path.is_file():
                continue
            with self.subTest(path=path.name):
                self.assertNotIn("case_note", path.read_text())

    def test_the_note_model_is_not_reachable_from_a_correspondence_context(self):
        """``_create_draft`` is the single choke point every outbound letter
        passes through (ADR 0004). Nothing it calls may read a note."""
        from apps.correspondence import services as correspondence_services

        tree = ast.parse(
            inspect.getsource(correspondence_services._create_draft)
        )
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(tree)
            if isinstance(node, (ast.Attribute, ast.Name))
        }
        self.assertNotIn("case_notes", names)
        self.assertNotIn("CaseNote", names)
