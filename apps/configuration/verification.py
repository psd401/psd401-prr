# ABOUTME: The live check a credential or identity gets when it is saved, and
# ABOUTME: the "saved, unverified" record that is written when it fails.
"""Saved is not the same as working, and the page should say which.

A district administrator who types a Vault service account has no way to
tell a correct address from a plausible one. Refusing the save would not
help — the value already in place is the one that does not work, so a
refusal keeps the broken configuration and teaches nothing. What helps is
saving it and then saying, in the same breath, that it did not authenticate.

Nothing downstream consults this record to decide whether to act. Every
consumer already fails closed on a credential it cannot use: the Gmail and
Vault clients raise their own unavailable errors, the group reader raises
``GoogleDirectoryUnavailable``, and a blank identity is refused before a
call is made. This module exists to put that same fact in front of a person
before a requester's deadline is the thing that reveals it.

Each check is bounded, best-effort, and swallows everything: a check that
raised would turn a saved setting into a failed save, which is the outcome
it was written to avoid. The recorded detail is the sanitized sentence the
integration already produces, never a provider traceback, because it is
shown in the staff UI and kept in the database.
"""

import logging

logger = logging.getLogger(__name__)

#: How the outcome of an unreachable check is recorded. "Unverified" is not
#: "broken": an air-gapped developer machine cannot reach Google either.
UNVERIFIED = False
VERIFIED = True


def _gmail_identity(_value) -> tuple[bool, str]:
    from apps.configuration import runtime

    if not runtime.get("PRR_GMAIL_IMPERSONATION_TARGET"):
        return UNVERIFIED, "No Gmail service identity is configured."
    from apps.integrations.gmail import client as gmail_client

    try:
        # The existing read-only verifier: credentials, the pinned subject,
        # the granted scopes, and the mailbox profile. It never sends.
        gmail_client.verify_configuration()
    except Exception as exc:  # noqa: BLE001 - every failure is "unverified"
        return UNVERIFIED, _sanitize(exc)
    return VERIFIED, ""


def _vault_identity(_value) -> tuple[bool, str]:
    from apps.configuration import runtime

    if not runtime.get("PRR_VAULT_IMPERSONATION_TARGET"):
        return UNVERIFIED, "No Vault service identity is configured."
    if not runtime.get("PRR_VAULT_ADMIN_USER"):
        return UNVERIFIED, "No Vault administrator account is configured."
    from apps.integrations.vault import client as vault_client

    try:
        vault_client.vault_credentials()
    except Exception as exc:  # noqa: BLE001
        return UNVERIFIED, _sanitize(exc)
    return VERIFIED, ""


def _google_group_reader(_value) -> tuple[bool, str]:
    from apps.api.identity import GoogleGroupDirectory

    try:
        GoogleGroupDirectory()._api()
    except Exception as exc:  # noqa: BLE001
        return UNVERIFIED, _sanitize(exc)
    return VERIFIED, ""


def _malware_scanner(_value) -> tuple[bool, str]:
    from apps.configuration import runtime

    if not runtime.get("CLAMAV_HOST"):
        return UNVERIFIED, "No malware scanner is configured."
    from apps.secure_files import AttachmentScanUnavailable, probe_clamav

    try:
        # A PING and nothing else: no file is sent and nothing is scanned.
        probe_clamav()
    except AttachmentScanUnavailable:
        return UNVERIFIED, (
            "The ClamAV scanner did not answer at that address; uploads "
            "stay quarantined until it does."
        )
    except Exception as exc:  # noqa: BLE001
        return UNVERIFIED, _sanitize(exc)
    return VERIFIED, ""


def _alta_api(_value) -> tuple[bool, str]:
    from apps.configuration import runtime

    if not runtime.get("ALTA_API_BASE"):
        return UNVERIFIED, "No Alta address is configured."
    if not runtime.get("ALTA_API_USERNAME"):
        return UNVERIFIED, "No Alta service account is configured."
    if not runtime.secret_is_set("ALTA_API_PASSWORD"):
        return UNVERIFIED, "No Alta service-account password is set."
    from apps.integrations.avigilon import client as alta

    try:
        # Sign in and read the device groups: one small GET, nothing written.
        alta.list_device_groups()
    except Exception as exc:  # noqa: BLE001
        return UNVERIFIED, _sanitize(exc)
    return VERIFIED, ""


def _sanitize(exc: Exception) -> str:
    """One readable sentence, with nothing a provider put in the exception.

    The integrations raise their own sanitized errors — ``GmailUnavailable``,
    ``VaultUnavailable``, ``GoogleDirectoryUnavailable``, ``AltaError`` — and
    those messages are written for staff. Anything else is reported by type
    alone rather than by a message that may carry a request body or a token.
    """
    from apps.api.identity import GoogleDirectoryUnavailable
    from apps.integrations.avigilon.client import AltaError
    from apps.integrations.gmail.client import GmailUnavailable
    from apps.integrations.vault.client import VaultUnavailable

    if isinstance(
        exc, (GmailUnavailable, VaultUnavailable, GoogleDirectoryUnavailable, AltaError)
    ):
        return str(exc)[:500]
    return (
        "The credential did not authenticate "
        f"({type(exc).__name__}). Check it with the district's Google "
        "administrator."
    )


#: Keys whose save triggers a live check, and the check it triggers. A key
#: that is not listed is simply never marked verified or unverified.
CHECKS = {
    "PRR_GMAIL_IMPERSONATION_TARGET": _gmail_identity,
    "PRR_VAULT_IMPERSONATION_TARGET": _vault_identity,
    "PRR_VAULT_ADMIN_USER": _vault_identity,
    "PRR_GOOGLE_GROUP_CREDENTIALS_FILE": _google_group_reader,
    "CLAMAV_HOST": _malware_scanner,
    "CLAMAV_PORT": _malware_scanner,
    "ALTA_API_BASE": _alta_api,
    "ALTA_API_USERNAME": _alta_api,
    "ALTA_API_PASSWORD": _alta_api,
}

#: The key whose verification record a check writes. Two settings can only
#: be checked together — a Vault identity is meaningless without the account
#: it acts as — so they share one record rather than each claiming to have
#: been verified alone.
RECORDS_UNDER = {
    "PRR_GMAIL_IMPERSONATION_TARGET": "PRR_GMAIL_IMPERSONATION_TARGET",
    "PRR_VAULT_IMPERSONATION_TARGET": "PRR_VAULT_IMPERSONATION_TARGET",
    "PRR_VAULT_ADMIN_USER": "PRR_VAULT_IMPERSONATION_TARGET",
    "PRR_GOOGLE_GROUP_CREDENTIALS_FILE": "PRR_GOOGLE_GROUP_CREDENTIALS_FILE",
    "CLAMAV_HOST": "CLAMAV_HOST",
    "CLAMAV_PORT": "CLAMAV_HOST",
    "ALTA_API_BASE": "ALTA_API_BASE",
    "ALTA_API_USERNAME": "ALTA_API_BASE",
    "ALTA_API_PASSWORD": "ALTA_API_BASE",
}


def verify(key: str, value=None) -> None:
    """Run ``key``'s live check, if it has one, and write down the result."""
    check = CHECKS.get(key)
    if check is None:
        return
    from .models import SettingVerification

    try:
        ok, detail = check(value)
    except Exception:  # a broken check never breaks a save
        logger.exception("The live check for %s could not run.", key)
        ok, detail = UNVERIFIED, "The live check could not be run."
    SettingVerification.objects.update_or_create(
        key=RECORDS_UNDER[key],
        defaults={"ok": ok, "detail": detail[:500]},
    )


def state_for(keys) -> dict:
    """The last check result per key, for the Settings page.

    Keyed by the setting an administrator changed, not by the record the
    check was written under, so a page showing the Vault account shows the
    Vault identity's verdict rather than nothing.
    """
    from .models import SettingVerification

    rows = {
        row.key: row
        for row in SettingVerification.objects.filter(
            key__in={RECORDS_UNDER[key] for key in keys if key in RECORDS_UNDER}
        )
    }
    state = {}
    for key in keys:
        record = rows.get(RECORDS_UNDER.get(key, ""))
        if record is None:
            state[key] = None
        else:
            state[key] = {
                "ok": record.ok,
                "detail": record.detail,
                "checked_at": record.checked_at,
            }
    return state
