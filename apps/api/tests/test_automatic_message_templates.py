"""Staff API for inspecting and activating automatic-message templates."""

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.api.identity import StaffRole, apply_staff_roles
from apps.correspondence.models import AutomaticMessageTemplate

ACK_BODY = """Dear {{ requester_name }},

The District received {{ request_number }} on {{ received_date }}.
You requested {{ request_description }}.
Under RCW 42.56.520, records are estimated for {{ estimated_completion }}.
Contact {{ pra_officer_email }}.
"""


class AutomaticMessageTemplateApiTests(APITestCase):
    def setUp(self):
        self.officer = get_user_model().objects.create_user(
            "template-api-officer",
            "template-api-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = get_user_model().objects.create_user(
            "template-api-reviewer",
            "template-api-reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.url = "/api/staff/automatic-message-templates/"

    def test_catalog_exposes_current_builtin_templates_and_tokens(self):
        self.client.force_authenticate(self.reviewer)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        rows = {row["kind"]: row for row in response.data["templates"]}
        self.assertEqual(rows["acknowledgment"]["active"]["source"], "built_in")
        self.assertIn("estimated_completion", rows["acknowledgment"]["allowed_tokens"])
        # The estimate token is offered but not compelled: the district's
        # counsel-approved wording states the RCW 42.56.520 estimate as a
        # duration rather than a date. What the letter cannot lose is the
        # statute itself, asserted below.
        self.assertEqual(
            rows["acknowledgment"]["required_tokens"],
            ["request_number"],
        )
        self.assertIn("RCW 42.56.520", rows["acknowledgment"]["active"]["body_template"])

    def test_officer_creates_previews_and_activates_a_new_version(self):
        self.client.force_authenticate(self.officer)
        created = self.client.post(
            self.url,
            {
                "kind": "acknowledgment",
                "subject_template": "{{ request_number }} — District acknowledgment",
                "body_template": ACK_BODY + "\nCUSTOM FOOTER\n",
                "change_reason": "Use the reviewed district acknowledgment language.",
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        template_id = created.data["id"]
        self.assertEqual(created.data["state"], "draft")

        preview = self.client.post(
            f"{self.url}{template_id}/preview/",
            {},
            format="json",
        )
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertIn("PRR-2026-0001", preview.data["subject"])
        self.assertIn("CUSTOM FOOTER", preview.data["body"])

        activated = self.client.post(
            f"{self.url}{template_id}/activate/",
            {
                "expected_version": created.data["revision"],
                "confirmed": True,
                "reason": "Activate the reviewed district acknowledgment language.",
            },
            format="json",
        )
        self.assertEqual(activated.status_code, 200, activated.data)
        self.assertEqual(activated.data["state"], "active")
        self.assertTrue(
            AutomaticMessageTemplate.objects.filter(
                pk=template_id,
                state=AutomaticMessageTemplate.State.ACTIVE,
            ).exists()
        )

    def test_reviewer_cannot_change_or_activate_templates(self):
        self.client.force_authenticate(self.reviewer)
        response = self.client.post(
            self.url,
            {
                "kind": "acknowledgment",
                "subject_template": "{{ request_number }} — Acknowledgment",
                "body_template": ACK_BODY,
                "change_reason": "Attempt a template change without officer authority.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)


class NamedDenialTemplateApiTests(APITestCase):
    """A denial can be several letters (bot request, full exemption), so it
    carries named templates beside the district default."""

    def setUp(self):
        user_model = get_user_model()
        self.officer = user_model.objects.create_user(
            "named-denial-officer",
            "named-denial-officer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.reviewer = user_model.objects.create_user(
            "named-denial-reviewer",
            "named-denial-reviewer@psd401.net",
            "pw",
            is_staff=True,
        )
        apply_staff_roles(self.reviewer, {StaffRole.REVIEWER})
        self.url = "/api/staff/automatic-message-templates/"

    DENIAL_BODY = (
        "To Whom It May Concern:\n\n"
        "Request {{ request_number }} is denied under RCW 42.56.080(3).\n"
    )

    def test_catalog_says_which_kinds_take_names_and_drops_starting_point(self):
        self.client.force_authenticate(self.reviewer)

        rows = {
            row["kind"]: row
            for row in self.client.get(self.url).data["templates"]
        }

        self.assertTrue(rows["denial"]["named"])
        self.assertTrue(rows["general"]["named"])
        self.assertFalse(rows["acknowledgment"]["named"])
        self.assertEqual(rows["denial"]["label"], "Denial")
        for row in rows.values():
            self.assertNotIn("starting point", row["label"].lower())
            self.assertNotIn("starting point", row["group"].lower())

    def test_officer_publishes_a_named_denial_beside_the_default(self):
        self.client.force_authenticate(self.officer)
        created = self.client.post(
            self.url,
            {
                "kind": "denial",
                "name": "Denial - bot generated",
                "subject_template": "Public Records Request {{ request_number }} — Request Closed",
                "body_template": self.DENIAL_BODY,
                "change_reason": "Bot-request denial wording under RCW 42.56.080(3).",
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["name"], "Denial - bot generated")

        activated = self.client.post(
            f"{self.url}{created.data['id']}/activate/",
            {
                "expected_version": created.data["revision"],
                "confirmed": True,
                "reason": "Activate the reviewed bot-request denial wording.",
            },
            format="json",
        )
        self.assertEqual(activated.status_code, 200, activated.data)

        rows = {
            row["kind"]: row
            for row in self.client.get(self.url).data["templates"]
        }
        # The default denial is untouched; the named one sits beside it.
        self.assertEqual(rows["denial"]["active"]["source"], "built_in")
        self.assertEqual(
            [row["name"] for row in rows["denial"]["named_active"]],
            ["Denial - bot generated"],
        )
        self.assertEqual(
            rows["denial"]["names"],
            ["Denial - bot generated"],
        )
