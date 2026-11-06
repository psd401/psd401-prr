# ABOUTME: Staff API tests for the Settings surface — admin-only access, the
# ABOUTME: reason-required save path, consequence text, and change history.
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.api.identity import StaffRole, apply_staff_roles
from apps.audit.models import AuditLog
from apps.configuration import runtime
from apps.configuration.models import AppSetting, AppSettingChange
from apps.configuration.registry import SETTING_KEYS


#: Django's test runner forces ``DEBUG`` off, so enabling Gmail needs the
#: keyless impersonation target a deployment would have. Identity is the one
#: part of the transport prerequisite that stays in the environment.
@override_settings(
    PRR_GMAIL_IMPERSONATION_TARGET="prr-gmail@example.iam.gserviceaccount.com"
)
class AppSettingApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        User = get_user_model()
        self.admin = User.objects.create_user(
            "admin", "admin@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})
        self.officer = User.objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.officer, {StaffRole.RECORDS_OFFICER})
        self.client.login(username="admin", password="pw")

    # --- access -----------------------------------------------------------

    def test_anonymous_is_refused(self):
        self.client.logout()
        self.assertEqual(self.client.get("/api/staff/settings/").status_code, 403)

    def test_a_non_admin_staff_member_cannot_see_the_page(self):
        self.client.logout()
        self.client.login(username="officer", password="pw")
        self.assertEqual(self.client.get("/api/staff/settings/").status_code, 403)

    def test_a_non_admin_staff_member_cannot_change_a_setting(self):
        self.client.logout()
        self.client.login(username="officer", password="pw")
        response = self.client.patch(
            "/api/staff/settings/PRR_GMAIL_ENABLED/",
            {"value": True, "reason": "Trying to enable transport myself."},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AppSetting.objects.exists())

    def test_an_administrators_api_token_cannot_change_a_setting(self):
        # The catalog marks this change human_only: an attributed personal
        # token (hosted MCP, scripts) gets the handoff, not the change, even
        # when it belongs to an Application Admin.
        from rest_framework.authtoken.models import Token

        self.client.logout()
        token = Token.objects.create(user=self.admin)
        response = self.client.patch(
            "/api/staff/settings/AUTO_ACK_SAFETY_NET/",
            {"value": True, "reason": "Scripted enablement of the safety net."},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "human_approval_required")
        self.assertEqual(
            response.json()["handoff"]["capability_id"], "app_settings.update"
        )
        self.assertFalse(AppSetting.objects.exists())

    # --- read -------------------------------------------------------------

    def test_list_returns_every_registered_setting_with_its_consequence(self):
        response = self.client.get("/api/staff/settings/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = {row["key"]: row for row in payload["results"]}
        self.assertEqual(set(rows), set(SETTING_KEYS))
        for row in rows.values():
            self.assertTrue(row["consequence"])
            self.assertIn(row["category"], payload["categories"])
            self.assertIn("value", row)
            self.assertIn("last_change", row)

    def test_model_settings_offer_the_granted_profiles_as_a_pick_list(self):
        rows = {
            row["key"]: row
            for row in self.client.get("/api/staff/settings/").json()["results"]
        }
        self.assertIn("us.openai.gpt-6.1-terra", rows["PRR_AI_MODEL_INTAKE"]["suggestions"])
        self.assertEqual(rows["PRR_AI_MODEL"]["suggestions"], rows["PRR_AI_MODEL_INTAKE"]["suggestions"])
        self.assertEqual(rows["AUTO_ACK_SAFETY_NET"]["suggestions"], [])

    def test_the_safety_net_row_states_the_statutory_consequence(self):
        response = self.client.get("/api/staff/settings/")
        self.assertEqual(response.status_code, 200)
        row = next(
            item
            for item in response.json()["results"]
            if item["key"] == "AUTO_ACK_SAFETY_NET"
        )
        consequence = row["consequence"].lower()
        self.assertIn("strict liability", consequence)
        self.assertIn("42.56.520", consequence)

    def test_an_unknown_key_is_not_found(self):
        response = self.client.get(
            "/api/staff/settings/DJANGO_SECRET_KEY/history/"
        )
        self.assertEqual(response.status_code, 404)

    def test_an_unknown_key_cannot_be_written(self):
        response = self.client.patch(
            "/api/staff/settings/DJANGO_SECRET_KEY/",
            {"value": True, "reason": "Attempting to write an unknown key."},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

    # --- write ------------------------------------------------------------

    def test_a_change_with_a_reason_takes_effect_and_is_recorded(self):
        response = self.client.patch(
            "/api/staff/settings/PRR_GMAIL_ENABLED/",
            {
                "value": True,
                "reason": "District approved production Gmail cutover.",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["value"])
        self.assertTrue(runtime.get("PRR_GMAIL_ENABLED"))
        change = AppSettingChange.objects.get(key="PRR_GMAIL_ENABLED")
        self.assertEqual(change.actor, self.admin)
        self.assertEqual(
            change.reason, "District approved production Gmail cutover."
        )
        self.assertTrue(
            AuditLog.objects.filter(
                action="configuration.setting_changed",
                target="PRR_GMAIL_ENABLED",
                actor=self.admin,
            ).exists()
        )

    def test_a_change_without_a_reason_is_rejected(self):
        response = self.client.patch(
            "/api/staff/settings/PRR_GMAIL_ENABLED/",
            {"value": True},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("reason", response.json())
        self.assertFalse(AppSettingChange.objects.exists())

    def test_a_change_with_a_token_reason_is_rejected(self):
        response = self.client.patch(
            "/api/staff/settings/PRR_GMAIL_ENABLED/",
            {"value": True, "reason": "ok"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AppSettingChange.objects.exists())

    def test_a_dependency_failure_is_reported_to_the_administrator(self):
        response = self.client.patch(
            "/api/staff/settings/PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED/",
            {
                "value": True,
                "reason": "Officers asked for intent proposals at intake.",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        # A refusal about the change itself, not a complaint about the value.
        self.assertIn("Advisory AI", response.json()["detail"])
        self.assertNotIn("value", response.json())
        self.assertFalse(
            runtime.get("PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED")
        )

    def test_history_lists_every_change_newest_first(self):
        for value, reason in (
            (True, "Enabling for the production cutover rehearsal."),
            (False, "Pausing transport while the mailbox is reconfigured."),
        ):
            self.client.patch(
                "/api/staff/settings/PRR_GMAIL_ENABLED/",
                {"value": value, "reason": reason},
                content_type="application/json",
            )
        response = self.client.get(
            "/api/staff/settings/PRR_GMAIL_ENABLED/history/"
        )
        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["new_value"])
        self.assertEqual(rows[0]["actor_email"], "admin@psd401.net")
        self.assertTrue(rows[1]["new_value"])

    # --- typed settings (Phase B) -----------------------------------------

    def test_every_row_carries_the_type_and_bounds_the_page_needs(self):
        """The page cannot render an editor it has not been told about.

        Sending the bounds with the row is also what lets an administrator
        see the limit before they type past it rather than after. They are
        sent only where they mean something: a minimum on a domain list would
        be noise a page might then try to honour.
        """
        response = self.client.get("/api/staff/settings/")
        rows = {row["key"]: row for row in response.json()["results"]}

        camera = rows["CAMERA_RETENTION_DAYS"]
        self.assertEqual(camera["type"], "integer")
        self.assertEqual(camera["value"], 10)
        self.assertEqual(camera["unit"], "days")
        self.assertEqual((camera["minimum"], camera["maximum"]), (1, 3650))

        domains = rows["PRR_DISTRICT_EMAIL_DOMAINS"]
        self.assertEqual(domains["type"], "domain_list")
        self.assertEqual(domains["value"], ["psd401.net"])
        self.assertIsNone(domains["minimum"])
        self.assertEqual(domains["max_items"], 50)

        rate = rows["PRR_STAFF_LOGIN_RATE"]
        self.assertEqual(rate["type"], "rate")
        self.assertEqual(rate["value"], "10/min")
        self.assertIn("min", rate["periods"])
        self.assertNotIn("day", rate["periods"])

        provider = rows["PRR_AI_PROVIDER"]
        self.assertEqual(provider["type"], "text")
        self.assertEqual(provider["value"], "bedrock")

        mailbox = rows["PRR_GMAIL_MAILBOX"]
        self.assertEqual(mailbox["type"], "email")
        self.assertEqual(mailbox["value"], "publicrecords@psd401.net")
        self.assertEqual(mailbox["max_length"], 100)

    def test_a_list_value_round_trips_as_a_list(self):
        response = self.client.patch(
            "/api/staff/settings/PRR_DISTRICT_EMAIL_DOMAINS/",
            {
                "value": ["PSD401.net", "psd401.org", "psd401.net"],
                "reason": "Adding the district's second mail domain.",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        # Normalised and de-duplicated by the store, not by the caller.
        self.assertEqual(
            response.json()["value"], ["psd401.net", "psd401.org"]
        )
        self.assertEqual(
            runtime.get("PRR_DISTRICT_EMAIL_DOMAINS"),
            ("psd401.net", "psd401.org"),
        )

    def test_a_number_outside_its_bounds_is_refused_with_the_limit(self):
        response = self.client.patch(
            "/api/staff/settings/CAMERA_RETENTION_DAYS/",
            {"value": 5000, "reason": "Trying an impossible retention window."},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("3650", response.json()["detail"])
        self.assertEqual(runtime.get("CAMERA_RETENTION_DAYS"), 10)

    def test_a_rate_is_refused_unless_it_is_a_rate(self):
        response = self.client.patch(
            "/api/staff/settings/PRR_STAFF_LOGIN_RATE/",
            {"value": "lots", "reason": "Trying to loosen the login brake."},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(runtime.get("PRR_STAFF_LOGIN_RATE"), "10/min")

    def test_an_ordering_relation_is_a_refusal_not_a_field_error(self):
        response = self.client.patch(
            "/api/staff/settings/PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES/",
            {
                "value": 1024,
                "reason": "Trying to drop the combined limit below per-file.",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("per-file", response.json()["detail"])
        self.assertNotIn("value", response.json())

    def test_a_number_change_takes_effect_and_is_historied(self):
        response = self.client.patch(
            "/api/staff/settings/CAMERA_RETENTION_DAYS/",
            {
                "value": 45,
                "reason": "Facilities confirmed the vendor keeps 45 days.",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["value"], 45)
        self.assertEqual(runtime.get("CAMERA_RETENTION_DAYS"), 45)
        change = AppSettingChange.objects.get(key="CAMERA_RETENTION_DAYS")
        self.assertEqual(change.new_value, 45)
        self.assertIsNone(change.old_value)
        self.assertEqual(change.actor, self.admin)

    # --- write-only secrets ----------------------------------------------

    SECRET_URL = "/api/staff/settings/PRR_GOOGLE_OIDC_CLIENT_SECRET/"

    def test_a_secret_row_describes_itself_without_its_value(self):
        response = self.client.patch(
            self.SECRET_URL,
            {
                "value": "GOCSPX-rotated-value",
                "reason": "Rotated the sign-in client secret with Google.",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["value"]["set"])
        self.assertTrue(body["value"]["fingerprint"])
        self.assertEqual(body["value"]["set_by"], "admin")
        self.assertNotIn("GOCSPX-rotated-value", response.content.decode())

    def test_the_list_never_carries_a_secret_value(self):
        self.client.patch(
            self.SECRET_URL,
            {
                "value": "GOCSPX-rotated-value",
                "reason": "Rotated the sign-in client secret with Google.",
            },
            content_type="application/json",
        )

        response = self.client.get("/api/staff/settings/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("GOCSPX-rotated-value", response.content.decode())
        row = next(
            item
            for item in response.json()["results"]
            if item["key"] == "PRR_GOOGLE_OIDC_CLIENT_SECRET"
        )
        self.assertEqual(row["type"], "secret")
        self.assertTrue(row["value"]["set"])

    def test_the_history_never_carries_a_secret_value(self):
        self.client.patch(
            self.SECRET_URL,
            {
                "value": "GOCSPX-rotated-value",
                "reason": "Rotated the sign-in client secret with Google.",
            },
            content_type="application/json",
        )

        response = self.client.get(f"{self.SECRET_URL}history/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("GOCSPX-rotated-value", response.content.decode())
        self.assertTrue(response.json()["results"][0]["new_value"]["set"])

    def test_an_unset_secret_says_so_rather_than_showing_a_blank_value(self):
        response = self.client.get("/api/staff/settings/")
        row = next(
            item
            for item in response.json()["results"]
            if item["key"] == "PRR_GOOGLE_OIDC_CLIENT_SECRET"
        )

        self.assertFalse(row["value"]["set"])
        self.assertEqual(row["value"]["fingerprint"], "")
        self.assertIsNone(row["default"])
