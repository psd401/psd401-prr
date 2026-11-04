# ABOUTME: Tests for the in-app settings store — read resolution, fail-closed
# ABOUTME: defaults, the reasoned write path, append-only history, and import.
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import CommandError, call_command
from django.db import DatabaseError
from django.test import TestCase, override_settings

from apps.api.identity import StaffRole, apply_staff_roles
from apps.audit.models import AuditLog
from apps.configuration import runtime, services, values
from apps.configuration.models import AppSetting, AppSettingChange
from apps.configuration.registry import (
    CATEGORY_ORDER,
    SECRET_KEYS,
    SETTING_KEYS,
    SETTINGS,
    SETTINGS_BY_KEY,
    dependents_of,
)

#: Every key that has a row in ``AppSetting``. A secret's plaintext is never
#: stored there, never returned by ``all_values``, and never written into a
#: history row, so a count of the settings store is a count of these.
STORED_KEYS = tuple(key for key in SETTING_KEYS if key not in SECRET_KEYS)


#: Django's test runner forces ``DEBUG`` off, so a test that enables Gmail
#: has to supply the keyless impersonation target a deployment would have.
#: Phase C moved it into the store, where a key with no stored row still
#: resolves through Django settings -- so overriding it here keeps working
#: and now describes the pre-import bootstrap rather than the environment.
GMAIL_IDENTITY = override_settings(
    PRR_GMAIL_IMPERSONATION_TARGET="prr-gmail@example.iam.gserviceaccount.com"
)

PHASE_A_SWITCHES = {
    "PRR_AI_ADVISORY_ENABLED",
    "PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED",
    "PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED",
    "PRR_CORRESPONDENCE_OUTBOUND_ENABLED",
    "PRR_CORRESPONDENCE_AUTOMATED_OUTBOUND_ENABLED",
    "PRR_GMAIL_ENABLED",
    "PRR_GMAIL_INBOUND_POLL_ENABLED",
    "PRR_GMAIL_INBOUND_ARCHIVE_PROCESSED",
    "PRR_STAFF_NOTIFICATIONS_ENABLED",
    "PRR_CLOUDWATCH_METRICS_ENABLED",
    "AUTO_ACK_SAFETY_NET",
}


class RegistryTests(TestCase):
    def test_every_phase_a_switch_is_still_registered(self):
        self.assertEqual(PHASE_A_SWITCHES - set(SETTING_KEYS), set())

    def test_every_boolean_switch_defaults_to_the_safe_value(self):
        """Off, for every switch. A limit's safe default is a different
        question -- it is the value the shipped configuration has always
        used, asserted per key in ``test_registry_bounds``."""
        for spec in SETTINGS:
            if spec.type != values.BOOLEAN:
                continue
            self.assertFalse(spec.default, spec.key)

    def test_every_switch_carries_consequence_text_and_a_known_category(self):
        for spec in SETTINGS:
            self.assertGreaterEqual(len(spec.consequence), 40, spec.key)
            self.assertIn(spec.category, CATEGORY_ORDER, spec.key)

    def test_the_safety_net_consequence_names_strict_liability(self):
        consequence = SETTINGS_BY_KEY["AUTO_ACK_SAFETY_NET"].consequence.lower()
        self.assertIn("strict liability", consequence)
        self.assertIn("42.56.520", consequence)

    def test_dependencies_name_registered_keys_only(self):
        for spec in SETTINGS:
            for required in spec.requires:
                self.assertIn(required, SETTINGS_BY_KEY, spec.key)

    def test_automatic_advisory_switches_require_advisory(self):
        for key in (
            "PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED",
            "PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED",
            "PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED",
            "PRR_AI_AUTOMATIC_INTAKE_CLASSIFICATION_ENABLED",
            "PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED",
        ):
            self.assertIn("PRR_AI_ADVISORY_ENABLED", SETTINGS_BY_KEY[key].requires)
        self.assertEqual(
            set(dependents_of("PRR_AI_ADVISORY_ENABLED")),
            {
                "PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED",
                "PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED",
                # Its own switch, not a sub-setting of the intent one: intent
                # is the kind whose completed proposal can start an unattended
                # Vault count, and a title starts nothing.
                "PRR_AI_AUTOMATIC_CASE_TITLES_ENABLED",
                # Likewise its own switch: a label starts nothing either.
                "PRR_AI_AUTOMATIC_INTAKE_CLASSIFICATION_ENABLED",
                # Drafts a letter for an officer to approve; sends nothing.
                "PRR_AI_AUTOMATIC_ESTIMATE_LETTER_ENABLED",
            },
        )


class RuntimeReadTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_stored_value_wins_over_the_environment(self):
        AppSetting.objects.create(key="PRR_GMAIL_ENABLED", value=False)
        with override_settings(PRR_GMAIL_ENABLED=True):
            self.assertFalse(runtime.get("PRR_GMAIL_ENABLED"))

    def test_missing_row_falls_back_to_the_deployed_environment_value(self):
        self.assertFalse(
            AppSetting.objects.filter(key="PRR_GMAIL_ENABLED").exists()
        )
        with override_settings(PRR_GMAIL_ENABLED=True):
            self.assertTrue(runtime.get("PRR_GMAIL_ENABLED"))

    def test_unreadable_store_fails_closed_to_the_safe_default(self):
        with patch.object(
            runtime, "_stored_value", side_effect=DatabaseError("no table")
        ):
            with override_settings(PRR_CORRESPONDENCE_OUTBOUND_ENABLED=True):
                self.assertFalse(
                    runtime.get("PRR_CORRESPONDENCE_OUTBOUND_ENABLED")
                )

    def test_a_caller_that_must_not_mistake_failure_for_off_can_ask_to_raise(self):
        # The statutory safety net cannot treat "store unreadable" as
        # "switch off": that would skip a day-5 acknowledgment silently.
        with patch.object(
            runtime, "_stored_value", side_effect=DatabaseError("no table")
        ):
            with self.assertRaises(DatabaseError):
                runtime.get("AUTO_ACK_SAFETY_NET", raise_on_store_error=True)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(KeyError):
            runtime.get("PRR_NOT_A_MANAGED_SETTING")

    def test_all_values_covers_the_registry(self):
        self.assertEqual(set(runtime.all_values()), set(STORED_KEYS))

    @GMAIL_IDENTITY
    def test_a_change_takes_effect_without_a_restart(self):
        admin = get_user_model().objects.create_user(
            "admin", "admin@psd401.net", "pw", is_staff=True
        )
        AppSetting.objects.create(key="PRR_GMAIL_ENABLED", value=False)
        runtime.invalidate_all()
        self.assertFalse(runtime.get("PRR_GMAIL_ENABLED"))
        services.set_value(
            "PRR_GMAIL_ENABLED",
            True,
            actor=admin,
            reason="District approved Gmail transport for production cutover.",
        )
        self.assertTrue(runtime.get("PRR_GMAIL_ENABLED"))

    @patch.object(runtime, "CACHE_TTL_SECONDS", 5)
    def test_a_stored_read_is_served_from_cache_without_another_query(self):
        # One of the two tests that is about the cache rather than about a
        # value, so it opts back into the TTL config/test_settings.py zeroes.
        AppSetting.objects.create(key="PRR_GMAIL_ENABLED", value=True)
        runtime.invalidate_all()
        self.assertTrue(runtime.get("PRR_GMAIL_ENABLED"))
        with self.assertNumQueries(0):
            self.assertTrue(runtime.get("PRR_GMAIL_ENABLED"))
        runtime.invalidate_all()


@GMAIL_IDENTITY
class SetValueTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = get_user_model().objects.create_user(
            "admin", "admin@psd401.net", "pw", is_staff=True
        )

    def _set(self, key, value, reason="District decision recorded in writing."):
        return services.set_value(key, value, actor=self.admin, reason=reason)

    def _approve_advisory_model(self):
        """The provider, region, and model advisory AI may not be enabled without.

        They live in the store now, so a test arranges them the way an
        administrator would rather than by overriding a Django setting.
        """
        AppSetting.objects.create(key="PRR_AI_PROVIDER", value="bedrock")
        AppSetting.objects.create(key="PRR_AI_AWS_REGION", value="us-west-2")
        AppSetting.objects.create(
            key="PRR_AI_MODEL", value="us.amazon.nova-2-lite-v1:0"
        )
        runtime.invalidate_all()

    def test_the_service_itself_refuses_an_actor_without_admin_authority(self):
        # Authority is re-checked inside the one write door, not only at the
        # API, so a shell or a future caller cannot bypass it.
        officer = get_user_model().objects.create_user(
            "officer", "officer@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(officer, {StaffRole.RECORDS_OFFICER})
        with self.assertRaises(PermissionDenied):
            services.set_value(
                "AUTO_ACK_SAFETY_NET",
                True,
                actor=officer,
                reason="Turning the safety net on myself.",
            )
        self.assertFalse(AppSetting.objects.exists())
        self.assertFalse(AppSettingChange.objects.exists())

    def test_the_service_refuses_a_deactivated_administrator(self):
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            self._set("PRR_GMAIL_ENABLED", True)
        self.assertFalse(AppSetting.objects.exists())

    def test_a_change_records_history_and_audit(self):
        self._set("PRR_GMAIL_ENABLED", True, reason="Cutover approved 2026-09-23.")
        change = AppSettingChange.objects.get(key="PRR_GMAIL_ENABLED")
        self.assertEqual(change.actor, self.admin)
        self.assertEqual(change.new_value, True)
        self.assertEqual(change.source, AppSettingChange.Source.ADMIN)
        self.assertEqual(change.reason, "Cutover approved 2026-09-23.")
        self.assertTrue(
            AuditLog.objects.filter(
                action="configuration.setting_changed",
                target="PRR_GMAIL_ENABLED",
            ).exists()
        )

    def test_history_is_append_only(self):
        self._set("PRR_GMAIL_ENABLED", True)
        change = AppSettingChange.objects.get(key="PRR_GMAIL_ENABLED")
        with self.assertRaises(ValidationError):
            change.reason = "rewritten"
            change.save()
        with self.assertRaises(ValidationError):
            change.delete()
        with self.assertRaises(ValidationError):
            AppSettingChange.objects.all().delete()
        with self.assertRaises(ValidationError):
            AppSettingChange.objects.all().update(reason="rewritten")

    def test_a_short_reason_is_rejected_and_nothing_changes(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_ENABLED", True, reason="because")
        self.assertFalse(AppSetting.objects.exists())
        self.assertFalse(AppSettingChange.objects.exists())

    def test_a_blank_reason_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_ENABLED", True, reason="   ")

    def test_an_overlong_reason_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_ENABLED", True, reason="x" * 2001)

    def test_an_unattributed_change_is_rejected(self):
        with self.assertRaises(ValidationError):
            services.set_value(
                "PRR_GMAIL_ENABLED",
                True,
                actor=None,
                reason="District decision recorded in writing.",
            )

    def test_a_non_boolean_value_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_ENABLED", "true")

    def test_an_unknown_key_is_rejected(self):
        with self.assertRaises(KeyError):
            self._set("PRR_NOT_A_MANAGED_SETTING", True)

    def test_a_no_op_change_is_rejected(self):
        self._set("PRR_GMAIL_ENABLED", True)
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_ENABLED", True)

    def test_enabling_a_dependent_without_its_parent_is_rejected(self):
        self._approve_advisory_model()
        with self.assertRaises(ValidationError) as caught:
            self._set("PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED", True)
        self.assertIn("Advisory AI", str(caught.exception))

    def test_enabling_a_dependent_after_its_parent_succeeds(self):
        self._approve_advisory_model()
        self._set("PRR_AI_ADVISORY_ENABLED", True)
        self._set("PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED", True)
        self.assertTrue(runtime.get("PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED"))

    def test_a_parent_may_always_be_turned_off_and_the_dependent_goes_inert(self):
        self._approve_advisory_model()
        self._set("PRR_AI_ADVISORY_ENABLED", True)
        self._set("PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED", True)
        self._set("PRR_AI_ADVISORY_ENABLED", False)
        self.assertFalse(runtime.get("PRR_AI_ADVISORY_ENABLED"))
        entry = AuditLog.objects.filter(
            action="configuration.setting_changed",
            target="PRR_AI_ADVISORY_ENABLED",
        ).first()
        self.assertIn(
            "PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED",
            entry.detail["now_inert"],
        )

    def test_enabling_advisory_without_a_model_is_rejected(self):
        # The registry default for the model is blank, which is the state a
        # district that has never chosen one is in.
        with self.assertRaises(ValidationError) as caught:
            self._set("PRR_AI_ADVISORY_ENABLED", True)
        self.assertIn("advisory model", str(caught.exception))

    def test_enabling_advisory_without_a_region_is_rejected(self):
        AppSetting.objects.create(
            key="PRR_AI_MODEL", value="us.amazon.nova-2-lite-v1:0"
        )
        runtime.invalidate_all()
        with self.assertRaises(ValidationError) as caught:
            self._set("PRR_AI_ADVISORY_ENABLED", True)
        self.assertIn("region", str(caught.exception))

    def test_blanking_the_model_while_advisory_is_on_is_allowed(self):
        # The same asymmetry a parent switch has: a prerequisite that stops
        # being true never blocks the edit and never turns the switch off.
        # Advisory AI simply goes inert, which its own consumer enforces.
        self._approve_advisory_model()
        self._set("PRR_AI_ADVISORY_ENABLED", True)
        self._set("PRR_AI_MODEL", "")
        self.assertEqual(runtime.get("PRR_AI_MODEL"), "")
        self.assertTrue(runtime.get("PRR_AI_ADVISORY_ENABLED"))

    @override_settings(PRR_GMAIL_IMPERSONATION_TARGET="")
    def test_enabling_gmail_without_an_impersonation_target_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_ENABLED", True)
        self.assertFalse(AppSetting.objects.filter(key="PRR_GMAIL_ENABLED").exists())

    def test_the_mailbox_cannot_be_blanked(self):
        # The transport prerequisite is belt; this is braces. A mailbox is
        # not optional, so the type refuses an empty one outright rather
        # than letting Gmail be enabled against nothing.
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_MAILBOX", "")
        self.assertEqual(
            runtime.get("PRR_GMAIL_MAILBOX"), "publicrecords@psd401.net"
        )

    def test_enabling_staff_alerts_without_a_recipient_is_rejected(self):
        AppSetting.objects.create(
            key="PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS", value=[]
        )
        runtime.invalidate_all()
        self._set("PRR_GMAIL_ENABLED", True)
        with self.assertRaises(ValidationError):
            self._set("PRR_STAFF_NOTIFICATIONS_ENABLED", True)

    @override_settings(PRR_CLOUDWATCH_REGION="")
    def test_enabling_metrics_without_a_region_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_CLOUDWATCH_METRICS_ENABLED", True)

    def test_turning_a_switch_off_needs_no_prerequisite(self):
        AppSetting.objects.create(key="PRR_CLOUDWATCH_METRICS_ENABLED", value=True)
        runtime.invalidate_all()
        with override_settings(PRR_CLOUDWATCH_REGION=""):
            self._set("PRR_CLOUDWATCH_METRICS_ENABLED", False)
        self.assertFalse(runtime.get("PRR_CLOUDWATCH_METRICS_ENABLED"))


class ImportEnvironmentSettingsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @override_settings(PRR_GMAIL_ENABLED=True, AUTO_ACK_SAFETY_NET=True)
    def test_the_import_preserves_every_deployed_value(self):
        call_command("import_environment_settings")
        self.assertTrue(
            AppSetting.objects.get(key="PRR_GMAIL_ENABLED").value
        )
        self.assertTrue(
            AppSetting.objects.get(key="AUTO_ACK_SAFETY_NET").value
        )
        self.assertEqual(AppSetting.objects.count(), len(STORED_KEYS))
        self.assertTrue(runtime.get("PRR_GMAIL_ENABLED"))

    @override_settings(PRR_GMAIL_ENABLED=True)
    def test_the_import_is_attributed_to_a_system_import(self):
        call_command("import_environment_settings")
        change = AppSettingChange.objects.get(key="PRR_GMAIL_ENABLED")
        self.assertIsNone(change.actor)
        self.assertEqual(change.source, AppSettingChange.Source.SYSTEM_IMPORT)
        self.assertIsNone(change.old_value)

    @GMAIL_IDENTITY
    def test_the_import_never_overwrites_an_administrator_decision(self):
        admin = get_user_model().objects.create_user(
            "admin", "admin@psd401.net", "pw", is_staff=True
        )
        services.set_value(
            "PRR_GMAIL_ENABLED",
            True,
            actor=admin,
            reason="District approved Gmail transport for production.",
        )
        with override_settings(PRR_GMAIL_ENABLED=False):
            call_command("import_environment_settings")
        self.assertTrue(AppSetting.objects.get(key="PRR_GMAIL_ENABLED").value)
        self.assertEqual(
            AppSettingChange.objects.filter(key="PRR_GMAIL_ENABLED").count(), 1
        )

    @override_settings(PRR_GMAIL_ENABLED=True)
    def test_the_import_is_idempotent(self):
        call_command("import_environment_settings")
        call_command("import_environment_settings")
        self.assertEqual(AppSettingChange.objects.count(), len(STORED_KEYS))

    @override_settings(
        PRR_AI_ADVISORY_ENABLED=False,
        PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED=True,
    )
    def test_the_import_warns_about_a_dependent_imported_on_without_its_parent(self):
        # The import preserves what the deployment was doing, even an
        # inconsistent pair -- but it says so, so the drift is noticed on the
        # deploy rather than only on the Settings page.
        with self.assertLogs("apps.configuration", level="WARNING") as logs:
            call_command("import_environment_settings")
        self.assertTrue(
            AppSetting.objects.get(key="PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED").value
        )
        self.assertIn("PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED", "\n".join(logs.output))

    @override_settings(
        PRR_GMAIL_ENABLED=True,
        PRR_STAFF_NOTIFICATIONS_ENABLED=True,
        PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS=(),
    )
    def test_the_import_warns_about_a_switch_imported_on_without_its_prerequisite(self):
        with self.assertLogs("apps.configuration", level="WARNING") as logs:
            call_command("import_environment_settings")
        self.assertIn("PRR_STAFF_NOTIFICATIONS_ENABLED", "\n".join(logs.output))

    def test_an_invalid_deployed_value_fails_the_import_and_stores_nothing(self):
        # Storing the default instead would be permanent: once the row exists
        # the environment is never consulted again. Failing the deploy keeps
        # the running release and names the key to fix.
        with patch.dict("os.environ", {"PRR_AI_TIMEOUT_SECONDS": "99999"}):
            with self.assertRaisesMessage(CommandError, "PRR_AI_TIMEOUT_SECONDS"):
                call_command("import_environment_settings")
        self.assertEqual(AppSetting.objects.count(), 0)

    @override_settings(AUTO_ACK_SAFETY_NET="sometimes")
    def test_an_invalid_django_setting_fails_the_import(self):
        with self.assertRaisesMessage(CommandError, "AUTO_ACK_SAFETY_NET"):
            call_command("import_environment_settings")

    def test_an_invalid_value_for_an_already_stored_key_is_ignored(self):
        AppSetting.objects.create(key="PRR_AI_TIMEOUT_SECONDS", value=45)
        with patch.dict("os.environ", {"PRR_AI_TIMEOUT_SECONDS": "99999"}):
            call_command("import_environment_settings")
        self.assertEqual(
            AppSetting.objects.get(key="PRR_AI_TIMEOUT_SECONDS").value, 45
        )

    def test_a_consistent_import_warns_about_nothing(self):
        with self.assertNoLogs("apps.configuration.import", level="WARNING"):
            call_command("import_environment_settings")

    def test_a_later_deploy_does_not_warn_about_an_administrators_parent_off(self):
        # Turning a parent off under an enabled dependent is allowed by
        # design; only values this run imported are checked.
        with override_settings(
            PRR_AI_ADVISORY_ENABLED=True,
            PRR_AI_AUTOMATIC_RECORD_REVIEW_ENABLED=True,
        ):
            call_command("import_environment_settings")
        admin = get_user_model().objects.create_user(
            "admin", "admin@psd401.net", "pw", is_staff=True
        )
        services.set_value(
            "PRR_AI_ADVISORY_ENABLED",
            False,
            actor=admin,
            reason="Pausing every provider call during an incident.",
        )
        with self.assertNoLogs("apps.configuration.import", level="WARNING"):
            call_command("import_environment_settings")


class WritePathFreshnessTests(TestCase):
    """The write path must not decide anything from a cached copy."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = get_user_model().objects.create_user(
            "admin", "admin@psd401.net", "pw", is_staff=True
        )

    @patch.object(runtime, "CACHE_TTL_SECONDS", 5)
    def test_a_dependent_cannot_be_enabled_from_a_stale_parent_read(self):
        # The other cache test: it needs a real TTL so there is a stale copy
        # for the write path to refuse to trust.
        AppSetting.objects.create(key="PRR_AI_AWS_REGION", value="us-west-2")
        AppSetting.objects.create(
            key="PRR_AI_MODEL", value="us.amazon.nova-2-lite-v1:0"
        )
        runtime.invalidate_all()
        services.set_value(
            "PRR_AI_ADVISORY_ENABLED",
            True,
            actor=self.admin,
            reason="Enabling advisory for the counsel-approved pilot.",
        )
        # Warm the cache, then turn the parent off behind that cached copy the
        # way another process would.
        self.assertTrue(runtime.get("PRR_AI_ADVISORY_ENABLED"))
        AppSetting.objects.filter(key="PRR_AI_ADVISORY_ENABLED").update(
            value=False
        )
        self.assertTrue(runtime.get("PRR_AI_ADVISORY_ENABLED"))

        with self.assertRaises(ValidationError):
            services.set_value(
                "PRR_AI_AUTOMATIC_REQUEST_INTENT_ENABLED",
                True,
                actor=self.admin,
                reason="Officers asked for intent proposals at intake.",
            )
