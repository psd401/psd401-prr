# ABOUTME: The named prerequisites the save path runs — what must already be
# ABOUTME: true before a switch may be turned on.
"""What must be true before a switch may be turned on.

Prerequisites answer "may this switch be turned *on* right now?". They
run on enable only. Phase A's asymmetry holds: a prerequisite that stops
being true later never blocks anything and never turns a switch off. The
switch goes inert, because every consumer requires the underlying value as
well as the switch. Blanking the advisory model while advisory AI is on is
allowed and makes advisory AI refuse to run; it is not a reason to refuse
the edit.

Before Phase B these questions were answered at settings import from
environment variables (``PRR_GMAIL_TRANSPORT_CONFIGURED``,
``PRR_AI_ADVISORY_MODEL_CONFIGURED``). The values they asked about now live
in the settings store, so the questions are asked of the store instead.
Phase C moved the last two holdouts -- the keyless impersonation target and
the CloudWatch region -- so every question here is now asked of the store,
through ``resolve``, and answered about the world the pending save would
create rather than the one the process started in.

Every function takes ``resolve(key)``, which returns the value a key will
have once the change being validated is applied, and returns an error
sentence or ``None``.
"""

import re

from django.conf import settings as django_settings

_AWS_REGION = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", flags=re.ASCII)
_BEDROCK_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", flags=re.ASCII)


def gmail_transport_configured(resolve):
    """A mailbox to use, and an identity allowed to act as it.

    ``DEBUG`` stands in for the impersonation target in local development,
    where the Gmail client is never really called.
    """
    if not resolve("PRR_GMAIL_MAILBOX"):
        return "Set the district mailbox before enabling Gmail transport."
    if django_settings.DEBUG:
        return None
    if not resolve("PRR_GMAIL_IMPERSONATION_TARGET"):
        return (
            "This deployment has no keyless Gmail impersonation target "
            "configured, so the application cannot reach the mailbox."
        )
    return None


def staff_notification_recipients_configured(resolve):
    if not resolve("PRR_STAFF_NOTIFICATION_RECORDS_OFFICERS"):
        return (
            "List at least one records-officer notification address before "
            "enabling staff alerts."
        )
    return None


def advisory_model_configured(resolve):
    """An approved provider, a real region, and a model identifier."""
    if resolve("PRR_AI_PROVIDER").lower() != "bedrock":
        return "Advisory AI is approved only for the Bedrock provider."
    region = resolve("PRR_AI_AWS_REGION")
    if not region or _AWS_REGION.fullmatch(region) is None:
        return "Set the advisory model's AWS region before enabling advisory AI."
    model = resolve("PRR_AI_MODEL")
    if not model or len(model) > 255 or _BEDROCK_MODEL.fullmatch(model) is None:
        return "Set the approved advisory model before enabling advisory AI."
    return None


def cloudwatch_region_configured(resolve):
    if not resolve("PRR_CLOUDWATCH_REGION"):
        return (
            "Configure the CloudWatch region before enabling metric "
            "publication."
        )
    return None


#: Prerequisites a switch's enable is checked against.
PREREQUISITES = {
    "gmail_transport_configured": gmail_transport_configured,
    "staff_notification_recipients_configured": (
        staff_notification_recipients_configured
    ),
    "advisory_model_configured": advisory_model_configured,
    "cloudwatch_region_configured": cloudwatch_region_configured,
}
