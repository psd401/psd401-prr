# ABOUTME: Phase B guarantees — typed bounds on the save path, the four
# ABOUTME: ordering relations, restart-free rates, and no key left in settings.
"""The bounds that used to crash a container now refuse a save.

Every assertion here used to live somewhere else: in ``config/settings.py``
as an ``ImproperlyConfigured`` raised at process start, or in a test that
imported settings in a subprocess to watch it happen. The guarantee did not
weaken -- an out-of-range value is still refused -- but it is enforced where
an accountable human meets it, with the limit in the sentence, instead of by
a crash-looping container nobody can see without shell access.

The last class is the one that keeps this phase honest: it fails if a moved
key ever reappears as a ``settings.PRR_*`` read.
"""

import ast
import os
import pathlib
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.test import TestCase, override_settings

from apps.api.identity import StaffRole, apply_staff_roles
from apps.configuration import prerequisites, runtime, services, values
from apps.configuration.models import AppSetting
from apps.configuration.registry import (
    RELATIONS,
    SETTINGS,
    SETTINGS_BY_KEY,
)

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[3]


class _SaveTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = get_user_model().objects.create_user(
            "admin", "admin@psd401.net", "pw", is_staff=True
        )
        apply_staff_roles(self.admin, {StaffRole.ADMIN})

    def _set(self, key, value, reason="District decision recorded in writing."):
        return services.set_value(
            key, value, actor=self.admin, reason=reason
        )


class BoundedValueTests(_SaveTestCase):
    """The numeric bounds ``config/settings.py`` enforced at import."""

    #: (key, a value below the floor, a value above the ceiling). These are
    #: the exact bounds the settings module used to raise on, so the table
    #: doubles as the record of what moved.
    OUT_OF_RANGE = (
        ("PRR_OBJECT_STORE_CONNECT_TIMEOUT_SECONDS", 0, 31),
        ("PRR_OBJECT_STORE_READ_TIMEOUT_SECONDS", 0, 121),
        ("PRR_OBJECT_STORE_MAX_ATTEMPTS", 0, 6),
        ("PRR_GMAIL_TIMEOUT_SECONDS", 0, 121),
        ("PRR_GMAIL_INBOUND_LOOKBACK_DAYS", 0, 31),
        ("PRR_GMAIL_INBOUND_STALE_SECONDS", 119, 3601),
        ("PRR_STAFF_NOTIFICATION_SUPPRESSION_SECONDS", 59, 604801),
        ("PRR_CORRESPONDENCE_STUCK_SECONDS", 299, 86401),
        ("PRR_CORRESPONDENCE_LEASE_SECONDS", 29, 3601),
        ("PRR_CORRESPONDENCE_INBOUND_MAX_HEADER_LINE_BYTES", 77, 999),
        ("PRR_CORRESPONDENCE_INBOUND_MAX_MIME_DEPTH", 0, 31),
        ("PRR_CORRESPONDENCE_ATTACHMENT_MAX_COUNT", 0, 11),
        ("PRR_PERSONAL_TOKEN_MAX_LIFETIME_DAYS", 0, 366),
        ("PRR_PERSONAL_TOKEN_MAX_ACTIVE", 0, 26),
        ("PRR_PERSONAL_TOKEN_LAST_USED_WINDOW_SECONDS", -1, 3601),
        ("PRR_INSTALLMENT_DOWNLOAD_LIFETIME_DAYS", 0, 31),
        ("PRR_INSTALLMENT_ACCESS_CODE_MIN_LENGTH", 5, 65),
        ("PRR_INSTALLMENT_MAX_ITEMS", 0, 50001),
        ("PRR_GOOGLE_GROUP_RECHECK_SECONDS", 59, 86401),
        ("PRR_AI_TIMEOUT_SECONDS", 0, 121),
        ("PRR_AI_MAX_INPUT_CHARACTERS", 999, 250001),
        ("PRR_RECORD_PREVIEW_DPI", 71, 301),
        ("PRR_REDACTION_DPI", 95, 601),
        ("CAMERA_RETENTION_DAYS", 0, 3651),
    )

    def test_a_value_outside_its_bounds_is_refused_with_the_limit(self):
        for key, below, above in self.OUT_OF_RANGE:
            spec = SETTINGS_BY_KEY[key]
            for value in (below, above):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValidationError) as caught:
                        self._set(key, value)
                    message = " ".join(caught.exception.messages)
                    self.assertIn(str(spec.minimum), message)
                    self.assertIn(str(spec.maximum), message)
                    self.assertFalse(
                        AppSetting.objects.filter(key=key).exists()
                    )

    def test_a_value_at_each_bound_is_accepted(self):
        for key, _below, _above in self.OUT_OF_RANGE:
            spec = SETTINGS_BY_KEY[key]
            with self.subTest(key=key):
                self._set(key, spec.minimum)
                self.assertEqual(runtime.get(key), spec.minimum)

    def test_a_number_is_not_a_switch_and_a_switch_is_not_a_number(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_AI_TIMEOUT_SECONDS", True)
        with self.assertRaises(ValidationError):
            self._set("PRR_AI_TIMEOUT_SECONDS", "30")


class ShapedValueTests(_SaveTestCase):
    """Rates, labels, domains, addresses, and the one approved provider."""

    def test_a_provider_is_matched_regardless_of_case(self):
        # The environment parser this replaced lowercased the provider, so a
        # deployed "Bedrock" was valid and must stay valid.
        spec = SETTINGS_BY_KEY["PRR_AI_PROVIDER"]
        provider = values.coerce(spec, " Bedrock ")
        self.assertIsNone(
            prerequisites.advisory_model_configured(
                lambda key: {
                    "PRR_AI_PROVIDER": provider,
                    "PRR_AI_AWS_REGION": "us-west-2",
                    "PRR_AI_MODEL": "anthropic.claude-model",
                }[key]
            )
        )

    def test_a_throttle_rate_must_be_a_rate(self):
        for value in ("", "many/hour", "10", "10/fortnight", "0/hour"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self._set("PRR_STAFF_LOGIN_RATE", value)

    def test_a_staff_rate_may_not_use_a_daily_bucket(self):
        # A day's allowance spendable in one second is not a brake. The
        # public form's own rate is the one place a daily bucket is right,
        # because it bounds a day's total intake rather than a burst.
        with self.assertRaises(ValidationError):
            self._set("PRR_STAFF_LOGIN_RATE", "100/day")
        self._set("PRR_PUBLIC_INTAKE_GLOBAL_RATE", "250/day")
        self.assertEqual(runtime.get("PRR_PUBLIC_INTAKE_GLOBAL_RATE"), "250/day")

    def test_a_rate_above_the_ceiling_is_refused(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_STAFF_LOGIN_RATE", "10001/hour")

    def test_the_processed_label_rejects_unsafe_characters(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_INBOUND_PROCESSED_LABEL", "unsafe label")
        self._set("PRR_GMAIL_INBOUND_PROCESSED_LABEL", "PRR/Done")
        self.assertEqual(
            runtime.get("PRR_GMAIL_INBOUND_PROCESSED_LABEL"), "PRR/Done"
        )

    def test_a_domain_list_takes_exact_dns_domains_only(self):
        for value in ("example.com/path", "not a domain", "localhost", "*.com"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self._set("PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS", value)

    def test_a_domain_written_as_an_at_suffix_is_accepted_and_normalised(self):
        # "@psd401.net" can only mean psd401.net, and the Google-group domain
        # parser has always read it that way.
        self._set("PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS", "@nextrequest.com")
        self.assertEqual(
            runtime.get("PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS"),
            ("nextrequest.com",),
        )

    def test_a_domain_list_is_normalised_and_de_duplicated(self):
        self._set(
            "PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS",
            " NextRequest.com , nextrequest.com ,example.org",
        )
        self.assertEqual(
            runtime.get("PRR_EMAIL_INTAKE_BLOCKED_SENDER_DOMAINS"),
            ("nextrequest.com", "example.org"),
        )

    def test_a_domain_list_survives_the_store_round_trip_as_a_tuple(self):
        self._set("PRR_DISTRICT_EMAIL_DOMAINS", ["psd401.net", "psd401.org"])
        runtime.invalidate_all()
        self.assertEqual(
            runtime.get("PRR_DISTRICT_EMAIL_DOMAINS"),
            ("psd401.net", "psd401.org"),
        )

    def test_the_google_group_domains_may_not_be_emptied(self):
        # Every group mapping is validated against this list, so an empty one
        # would silently invalidate every role group the district has.
        with self.assertRaises(ValidationError):
            self._set("PRR_GOOGLE_GROUP_ALLOWED_DOMAINS", [])

    def test_an_address_list_takes_addresses_only(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS", "not-an-email")
        self._set(
            "PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS",
            "Records.Officer@psd401.net, dana@psd401.net",
        )
        self.assertEqual(
            runtime.get("PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS"),
            ("records.officer@psd401.net", "dana@psd401.net"),
        )

    def test_the_advisory_provider_accepts_only_the_approved_one(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_AI_PROVIDER", "openai")

    def test_the_advisory_region_must_look_like_an_aws_region(self):
        with self.assertRaises(ValidationError):
            self._set("PRR_AI_AWS_REGION", "US West 2")
        self._set("PRR_AI_AWS_REGION", "us-west-2")
        self.assertEqual(runtime.get("PRR_AI_AWS_REGION"), "us-west-2")

    def test_the_mailbox_stays_within_the_provider_account_column(self):
        # api.E017's second assertion, now enforced where it can refuse.
        long_local_part = "a" * 95
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_MAILBOX", f"{long_local_part}@psd401.net")
        with self.assertRaises(ValidationError):
            self._set("PRR_GMAIL_MAILBOX", "not-a-mailbox")


class RelationTests(_SaveTestCase):
    """The four ordering constraints, checked from either side."""

    def test_a_combined_ceiling_may_not_fall_below_its_per_item_ceiling(self):
        for relation in RELATIONS:
            with self.subTest(relation=relation.greater):
                lesser_value = runtime.get(relation.lesser)
                with self.assertRaises(ValidationError) as caught:
                    self._set(relation.greater, lesser_value - 1)
                self.assertIn(
                    relation.message, " ".join(caught.exception.messages)
                )

    def test_a_per_item_ceiling_may_not_rise_above_its_combined_ceiling(self):
        for relation in RELATIONS:
            with self.subTest(relation=relation.lesser):
                greater_value = runtime.get(relation.greater)
                spec = SETTINGS_BY_KEY[relation.lesser]
                candidate = min(greater_value + 1, spec.maximum)
                if candidate <= runtime.get(relation.lesser):
                    # The per-item ceiling is already pinned at its own
                    # maximum below the combined one; there is no value that
                    # could violate this relation from this side.
                    continue
                with self.assertRaises(ValidationError):
                    self._set(relation.lesser, candidate)

    def test_the_staleness_window_may_not_be_shorter_than_the_recheck(self):
        # Named explicitly because the failure it prevents is not a refused
        # upload but every staff session ending on its first recheck.
        with self.assertRaises(ValidationError):
            self._set("PRR_GOOGLE_GROUP_MAX_STALE_SECONDS", 600)
        self._set("PRR_GOOGLE_GROUP_RECHECK_SECONDS", 300)
        self._set("PRR_GOOGLE_GROUP_MAX_STALE_SECONDS", 600)
        self.assertEqual(runtime.get("PRR_GOOGLE_GROUP_MAX_STALE_SECONDS"), 600)


class ReplyAddressRuleTests(_SaveTestCase):
    """The local part and the domain are only meaningful together."""

    def test_a_local_part_that_cannot_form_an_address_is_refused(self):
        for value in ("", ".leading", "trailing.", "double..dot", "a" * 28):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self._set("PRR_CORRESPONDENCE_REPLY_LOCAL_PART", value)

    def test_any_accepted_pair_forms_a_deliverable_tokenised_address(self):  # no-assert: validate_email raises on a bad address
        # No cross-setting rule is needed: each part's own type already
        # guarantees the combination. 27 + "+" + a 36-character token is the
        # 64-character local-part limit, and a domain must end in a real TLD.
        local = values.coerce(
            SETTINGS_BY_KEY["PRR_CORRESPONDENCE_REPLY_LOCAL_PART"], "a.b-c_d" + "e" * 20
        )
        for domain in ("psd401.net", "a.io", "x-1.example.k12.wa.us"):
            with self.subTest(domain=domain):
                accepted = values.coerce(
                    SETTINGS_BY_KEY["PRR_CORRESPONDENCE_REPLY_DOMAIN"], domain
                )
                validate_email(
                    f"{local}+00000000-0000-0000-0000-000000000000@{accepted}"
                )

    def test_a_domain_without_a_real_top_level_label_is_refused(self):
        for value in ("psd401.n", "psd401.-net", "localhost"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self._set("PRR_CORRESPONDENCE_REPLY_DOMAIN", value)

    def test_a_valid_pair_is_accepted_and_read_back(self):
        self._set("PRR_CORRESPONDENCE_REPLY_LOCAL_PART", "records")
        self._set("PRR_CORRESPONDENCE_REPLY_DOMAIN", "psd401.org")
        self.assertEqual(
            runtime.get("PRR_CORRESPONDENCE_REPLY_LOCAL_PART"), "records"
        )
        self.assertEqual(
            runtime.get("PRR_CORRESPONDENCE_REPLY_DOMAIN"), "psd401.org"
        )


class RestartFreeTests(_SaveTestCase):
    """A change reaches the code that acts on it without a redeploy."""

    def test_a_throttle_reads_its_rate_per_request(self):
        from apps.api.throttles import StaffLoginThrottle

        self.assertEqual(StaffLoginThrottle().get_rate(), "10/min")
        self._set("PRR_STAFF_LOGIN_RATE", "30/min")
        self.assertEqual(StaffLoginThrottle().get_rate(), "30/min")
        # DRF derives the window from the rate in __init__, so a fresh
        # throttle is what makes the new rate actually apply.
        self.assertEqual(StaffLoginThrottle().num_requests, 30)

    def test_the_camera_retention_window_is_read_per_triage(self):
        from apps.records import triage

        source = ast.parse(pathlib.Path(triage.__file__).read_text())
        reads = [
            node
            for node in ast.walk(source)
            if isinstance(node, ast.Attribute)
            and node.attr == "CAMERA_RETENTION_DAYS"
        ]
        self.assertEqual(reads, [], "triage must read the retention window "
                         "from the settings store, not from Django settings.")
        self._set("CAMERA_RETENTION_DAYS", 45)
        self.assertEqual(runtime.get("CAMERA_RETENTION_DAYS"), 45)


def _settings_names(tree):
    """Every local name bound to ``django.conf.settings`` in a module."""
    names = {"settings"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "django.conf":
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "settings"
            )
    return names


def _is_settings(node, names):
    if isinstance(node, ast.Name):
        return node.id in names
    # django.conf.settings, spelled out.
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "settings"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "conf"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "django"
    )


def _settings_reads(tree, managed):
    """``(line, key)`` for every managed key read from Django settings."""
    names = _settings_names(tree)
    findings = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in managed
            and _is_settings(node.value, names)
        ):
            findings.append((node.lineno, node.attr))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and _is_settings(node.args[0], names)
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in managed
        ):
            findings.append((node.lineno, node.args[1].value))
    return sorted(findings)


def _assigned_names(tree):
    """Every name assigned anywhere in a module, inside blocks included."""
    assigned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        assigned.update(
            target.id for target in targets if isinstance(target, ast.Name)
        )
    return assigned


def _debug_only_assigned_names(tree):
    """Names assigned only under ``if DEBUG:``, never at module level.

    The distinction is the whole point of the Phase C exception: a
    development seed is invisible to a deployed instance, and a module-level
    assignment is not.
    """
    debug_guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.Name) and test.id == "DEBUG"):
            continue
        for inner in node.body:
            debug_guarded.update(_assigned_names(ast.Module(body=[inner], type_ignores=[])))
    return debug_guarded - _module_level_assigned_names(tree)


def _module_level_assigned_names(tree):
    assigned = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        assigned.update(
            target.id for target in targets if isinstance(target, ast.Name)
        )
    return assigned


class NoManagedSettingIsReadFromDjangoSettingsTests(TestCase):
    """A moved key read through ``settings.`` is a bug for this phase.

    A ``settings.PRR_X`` read is evaluated against whatever the environment
    produced at process start. That is exactly the restart-bound behaviour
    the settings store replaced: the value an administrator saved would not
    reach the code, and nothing would say so. An AST scan is the only check
    that keeps catching it as the codebase grows.

    ``config/settings.py`` is excluded because a key it still defines is a
    bootstrap seed, not a read -- and the companion test below asserts that
    no Phase B key is left there either way.
    """

    SCANNED = ("apps", "config", "integrations")

    def _managed_reads(self):
        managed = set(SETTINGS_BY_KEY)
        findings = []
        for package in self.SCANNED:
            for path in (REPOSITORY_ROOT / package).rglob("*.py"):
                relative = path.relative_to(REPOSITORY_ROOT)
                if "tests" in relative.parts or relative.name.startswith("test_"):
                    continue
                if relative.parts[:2] == ("apps", "configuration"):
                    continue
                tree = ast.parse(path.read_text())
                findings.extend(
                    f"{relative}:{line} {key}"
                    for line, key in _settings_reads(tree, managed)
                )
        return findings

    def test_the_scan_sees_through_an_aliased_settings_import(self):
        tree = ast.parse(
            "from django.conf import settings as conf_settings\n"
            "import django.conf\n"
            "a = conf_settings.PRR_AI_TIMEOUT_SECONDS\n"
            "b = getattr(conf_settings, 'PRR_AI_MODEL', '')\n"
            "c = django.conf.settings.PRR_GMAIL_MAILBOX\n"
        )
        self.assertEqual(
            [finding for _line, finding in _settings_reads(tree, set(SETTINGS_BY_KEY))],
            ["PRR_AI_TIMEOUT_SECONDS", "PRR_AI_MODEL", "PRR_GMAIL_MAILBOX"],
        )

    def test_the_leftover_scan_sees_an_assignment_inside_a_block(self):
        tree = ast.parse(
            "if DEBUG:\n"
            "    PRR_AI_TIMEOUT_SECONDS = 5\n"
            "else:\n"
            "    PRR_AI_MODEL: str = ''\n"
        )
        self.assertEqual(
            _assigned_names(tree), {"PRR_AI_TIMEOUT_SECONDS", "PRR_AI_MODEL"}
        )

    def test_no_production_module_reads_a_managed_setting_from_settings(self):
        self.assertEqual(
            self._managed_reads(),
            [],
            "These modules read an application-managed setting from Django "
            "settings, which is fixed at process start. Read it through "
            "apps.configuration.runtime.get(key) instead.",
        )

    def test_no_module_resolves_a_setting_at_import_time(self):
        """A module-level constant is the bug this phase replaced.

        ``runtime.get`` in a module body, a class body, or a default argument
        is evaluated once per process. It would look like a store read and
        behave like the environment variable it replaced: an administrator's
        change would never reach it, and nothing would say so. The store's
        whole promise is that a change lands without a restart.
        """
        findings = []
        for package in self.SCANNED:
            for path in (REPOSITORY_ROOT / package).rglob("*.py"):
                relative = path.relative_to(REPOSITORY_ROOT)
                if "tests" in relative.parts or relative.name.startswith("test_"):
                    continue
                tree = ast.parse(path.read_text())
                eager = list(tree.body)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        eager.extend(node.body)
                    if isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        eager.extend(node.args.defaults)
                        eager.extend(
                            default
                            for default in node.args.kw_defaults
                            if default is not None
                        )
                for statement in eager:
                    if isinstance(
                        statement,
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                    ):
                        continue
                    for inner in ast.walk(statement):
                        if (
                            isinstance(inner, ast.Call)
                            and isinstance(inner.func, ast.Attribute)
                            and inner.func.attr == "get"
                            and isinstance(inner.func.value, ast.Name)
                            and inner.func.value.id == "runtime"
                        ):
                            findings.append(f"{relative}:{inner.lineno}")  # noqa: PERF401 - reads clearer as a loop
        self.assertEqual(
            sorted(set(findings)),
            [],
            "These resolve an application-managed setting at import time, so "
            "an administrator's change would never reach them. Move the read "
            "inside the function that needs it.",
        )

    def test_no_phase_b_key_is_still_defined_in_the_settings_module(self):
        """A key with a Django settings attribute has two lifetimes.

        ``runtime.bootstrap_value`` deliberately prefers a Django settings
        attribute, so a Phase B key left in ``config/settings.py`` would keep
        resolving through the environment on any deployment that had not yet
        imported -- a second source of truth with a different lifetime. The
        eleven Phase A switches are the documented exception: they stay as
        the bootstrap seed their own phase defined.

        Phase C adds two more, and they are an exception of a different kind:
        they are assigned only inside ``if DEBUG:``. A deployed instance runs
        with ``DEBUG`` false and never gains the attribute, so it has exactly
        one source of truth; what the branch preserves is the old
        DEBUG-derived default (local sign-in, admin route open) for a
        developer's machine, which is the only place it ever applied.
        """
        from apps.configuration.tests.test_settings_store import (
            PHASE_A_SWITCHES,
        )

        development_only = {"PRR_AUTH_MODE", "PRR_ENABLE_ADMIN"}
        source = ast.parse((REPOSITORY_ROOT / "config" / "settings.py").read_text())
        assigned = _assigned_names(source)
        leftovers = sorted(
            (set(SETTINGS_BY_KEY) & assigned) - PHASE_A_SWITCHES - development_only
        )
        self.assertEqual(leftovers, [])
        self.assertEqual(
            sorted(development_only - _debug_only_assigned_names(source)),
            [],
            "These may be assigned only inside `if DEBUG:`; a deployed "
            "instance must resolve them from the settings store alone.",
        )


class SafeDefaultTests(TestCase):
    """A read that cannot reach the store still answers, and answers safely."""

    def test_every_default_is_a_value_the_save_path_would_accept(self):  # no-assert: coerce raises on a rejected default
        # The registry asserts this at import too; asserting it here means a
        # broken registry fails a test rather than every process at once.
        for spec in SETTINGS:
            with self.subTest(key=spec.key):
                values.coerce(spec, spec.default)

    @override_settings(PRR_GMAIL_MAILBOX="ignored@psd401.net")
    def test_an_unreadable_stored_value_falls_back_to_the_default(self):
        # An old row whose bound has since tightened, for instance. The read
        # must not raise in front of a requester.
        AppSetting.objects.create(key="PRR_AI_TIMEOUT_SECONDS", value=99999)
        runtime.invalidate_all()
        self.assertEqual(runtime.get("PRR_AI_TIMEOUT_SECONDS"), 30)

    def test_an_invalid_environment_bootstrap_falls_back_to_the_default(self):
        spec = SETTINGS_BY_KEY["PRR_AI_TIMEOUT_SECONDS"]
        with override_settings(PRR_AI_TIMEOUT_SECONDS="not-a-number"):
            self.assertEqual(runtime.bootstrap_value(spec), 30)


class ChainedBootstrapTests(TestCase):
    """A key that used to default from another variable still does, once.

    Before Phase B several settings had no value of their own on most
    deployments: they inherited one from a parent variable. The one-time
    import has to reproduce that inheritance, or a deployment that only ever
    set the parent silently loses its value the moment the row is written.
    """

    def _bootstrap(self, key, environ, **django_settings):
        spec = SETTINGS_BY_KEY[key]
        with mock.patch.dict(os.environ, {}):
            for name in (key, *spec.bootstrap_from):
                os.environ.pop(name, None)
            os.environ.update(environ)
            with override_settings(**django_settings):
                return runtime.bootstrap_value(spec)

    def test_district_domains_inherit_the_hosted_domain(self):
        self.assertEqual(
            self._bootstrap(
                "PRR_DISTRICT_EMAIL_DOMAINS",
                {},
                PRR_GOOGLE_HOSTED_DOMAIN="example.org",
            ),
            ("example.org",),
        )

    def test_group_domains_inherit_the_hosted_domain(self):
        self.assertEqual(
            self._bootstrap(
                "PRR_GOOGLE_GROUP_ALLOWED_DOMAINS",
                {},
                PRR_GOOGLE_HOSTED_DOMAIN="example.org",
            ),
            ("example.org",),
        )

    def test_custodian_domains_inherit_the_district_domains(self):
        self.assertEqual(
            self._bootstrap(
                "PRR_CUSTODIAN_ACCOUNT_DOMAINS",
                {"PRR_DISTRICT_EMAIL_DOMAINS": "example.org, staff.example.org"},
            ),
            ("example.org", "staff.example.org"),
        )

    def test_records_officers_inherit_the_officer_address(self):
        self.assertEqual(
            self._bootstrap(
                "PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS",
                {},
                PRA_OFFICER_EMAIL="officer@psd401.net",
            ),
            ("officer@psd401.net",),
        )

    def test_advisory_region_inherits_the_default_aws_region(self):
        self.assertEqual(
            self._bootstrap(
                "PRR_AI_AWS_REGION", {"AWS_DEFAULT_REGION": "us-west-2"}
            ),
            "us-west-2",
        )

    def test_correspondence_limits_inherit_narrower_intake_limits(self):
        for suffix, narrower in (
            ("MAX_COUNT", 3),
            ("MAX_BYTES", 10 * 1024 * 1024),
            ("TOTAL_MAX_BYTES", 20 * 1024 * 1024),
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    self._bootstrap(
                        f"PRR_CORRESPONDENCE_ATTACHMENT_{suffix}",
                        {f"PRR_INTAKE_ATTACHMENT_{suffix}": str(narrower)},
                    ),
                    narrower,
                )

    def test_an_inherited_value_outside_the_bounds_is_not_inherited(self):
        # The old correspondence limits refused an intake limit above the
        # version 1 contract; the import settles on the shipped default.
        self.assertEqual(
            self._bootstrap(
                "PRR_CORRESPONDENCE_ATTACHMENT_MAX_BYTES",
                {"PRR_INTAKE_ATTACHMENT_MAX_BYTES": str(60 * 1024 * 1024)},
            ),
            25 * 1024 * 1024,
        )

    def test_the_key_itself_wins_over_its_parent(self):
        self.assertEqual(
            self._bootstrap(
                "PRR_AI_AWS_REGION",
                {"PRR_AI_AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-west-2"},
            ),
            "us-east-1",
        )
