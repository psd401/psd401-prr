# ABOUTME: Publishes PII-free Gmail inbound poll health to AWS CloudWatch.
# ABOUTME: Telemetry failure never changes durable mailbox-processing behavior.
from __future__ import annotations

import logging

import boto3
from botocore.config import Config
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError

from apps.configuration import runtime
from apps.integrations.gmail import client as gmail_client

logger = logging.getLogger(__name__)
NAMESPACE = "PSD401/PRR"
CLOUDWATCH_CLIENT_CONFIG = Config(
    connect_timeout=2,
    read_timeout=5,
    retries={"mode": "standard", "total_max_attempts": 2},
)
SAFE_GMAIL_FAILURE_CATEGORIES = frozenset(
    {
        "authentication_failed",
        "message_rejected",
        "permanent_provider_failure",
        "provider_unavailable",
        "rate_limited",
        "temporary_provider_failure",
    }
)


def _failure_category(failure: Exception) -> str:
    if isinstance(failure, gmail_client.GmailUnavailable):
        if failure.category in SAFE_GMAIL_FAILURE_CATEGORIES:
            return failure.category
        return "provider_failure"
    if isinstance(failure, PermissionDenied):
        return "permission_denied"
    if isinstance(failure, ValidationError):
        return "validation_error"
    if isinstance(failure, DatabaseError):
        return "database_error"
    return "internal_error"


def _metric(
    name: str,
    value: float,
    *,
    unit: str,
    dimensions: list[dict[str, str]],
) -> dict:
    return {
        "MetricName": name,
        "Dimensions": dimensions,
        "Value": value,
        "Unit": unit,
    }


def publish_inbound_poll_metrics(
    *,
    result: dict[str, object] | None = None,
    failure: Exception | None = None,
    duration_seconds: float,
) -> bool:
    """Publish one poll outcome without message or mailbox identifiers."""
    if not runtime.get("PRR_CLOUDWATCH_METRICS_ENABLED"):
        return False
    if (result is None) == (failure is None):
        raise ValueError("Exactly one Gmail poll result or failure is required.")

    environment = str(settings.PRR_ENVIRONMENT)
    dimensions = [{"Name": "Environment", "Value": environment}]
    inbound_enabled = bool(runtime.get("PRR_GMAIL_INBOUND_POLL_ENABLED"))
    status = str(result.get("status", "")) if result is not None else "failed"
    # A running scheduler with intake disabled is operationally unhealthy in
    # production: otherwise an accidental kill-switch change would look green.
    healthy = failure is None and status == "ok"
    metrics = [
        _metric(
            "GmailInboundPollHealth",
            int(healthy),
            unit="Count",
            dimensions=dimensions,
        ),
        _metric(
            "GmailInboundPollEnabled",
            int(inbound_enabled),
            unit="Count",
            dimensions=dimensions,
        ),
        _metric(
            "GmailInboundPollDuration",
            max(0.0, float(duration_seconds)),
            unit="Seconds",
            dimensions=dimensions,
        ),
    ]
    if result is not None and status == "ok":
        for name, field in (
            ("GmailInboundMessagesReceived", "received"),
            ("GmailInboundMessagesDeduplicated", "deduplicated"),
            ("GmailInboundLabelFailures", "label_failures"),
        ):
            metrics.append(
                _metric(
                    name,
                    int(result.get(field, 0)),
                    unit="Count",
                    dimensions=dimensions,
                )
            )
    if failure is not None:
        metrics.append(
            _metric(
                "GmailInboundPollFailure",
                1,
                unit="Count",
                dimensions=[
                    *dimensions,
                    {
                        "Name": "FailureCategory",
                        "Value": _failure_category(failure),
                    },
                ],
            )
        )

    try:
        client = boto3.client(
            "cloudwatch",
            region_name=runtime.get("PRR_CLOUDWATCH_REGION"),
            config=CLOUDWATCH_CLIENT_CONFIG,
        )
        client.put_metric_data(Namespace=NAMESPACE, MetricData=metrics)
    except Exception as exc:  # noqa: BLE001 - telemetry never breaks delivery
        logger.error(
            "CloudWatch Gmail inbound telemetry publish failed",
            extra={"telemetry_failure_type": type(exc).__name__},
        )
        return False
    return True
