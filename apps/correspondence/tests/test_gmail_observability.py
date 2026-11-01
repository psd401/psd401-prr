# ABOUTME: AWS-native, PII-free health telemetry for the inbound Gmail poller.
# ABOUTME: Proves fixed metric names, bounded categories, and fail-open reporting.
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from apps.integrations.gmail import client as gmail_client


@override_settings(
    PRR_CLOUDWATCH_METRICS_ENABLED=True,
    PRR_ENVIRONMENT="production",
    PRR_GMAIL_INBOUND_POLL_ENABLED=True,
    PRR_CLOUDWATCH_REGION="us-west-2",
)
class GmailInboundCloudWatchTelemetryTests(TestCase):
    def publish(self, **kwargs):
        from apps.correspondence.gmail_observability import (
            publish_inbound_poll_metrics,
        )

        return publish_inbound_poll_metrics(**kwargs)

    @patch("apps.correspondence.gmail_observability.boto3.client")
    def test_success_publishes_health_counts_and_duration_without_mailbox(self, factory):
        client = factory.return_value

        self.publish(
            result={
                "status": "ok",
                "received": 3,
                "deduplicated": 2,
                "label_failures": 1,
            },
            duration_seconds=1.25,
        )

        factory.assert_called_once()
        self.assertEqual(factory.call_args.args, ("cloudwatch",))
        self.assertEqual(factory.call_args.kwargs["region_name"], "us-west-2")
        client_config = factory.call_args.kwargs["config"]
        self.assertEqual(client_config.connect_timeout, 2)
        self.assertEqual(client_config.read_timeout, 5)
        self.assertEqual(client_config.retries["total_max_attempts"], 2)
        payload = client.put_metric_data.call_args.kwargs
        self.assertEqual(payload["Namespace"], "PSD401/PRR")
        metrics = {row["MetricName"]: row for row in payload["MetricData"]}
        self.assertEqual(metrics["GmailInboundPollHealth"]["Value"], 1)
        self.assertEqual(metrics["GmailInboundPollEnabled"]["Value"], 1)
        self.assertEqual(metrics["GmailInboundMessagesReceived"]["Value"], 3)
        self.assertEqual(metrics["GmailInboundMessagesDeduplicated"]["Value"], 2)
        self.assertEqual(metrics["GmailInboundLabelFailures"]["Value"], 1)
        self.assertEqual(metrics["GmailInboundPollDuration"]["Value"], 1.25)
        self.assertEqual(
            metrics["GmailInboundPollHealth"]["Dimensions"],
            [{"Name": "Environment", "Value": "production"}],
        )
        self.assertNotIn("publicrecords", repr(payload).lower())

    @patch("apps.correspondence.gmail_observability.boto3.client")
    def test_provider_failure_uses_bounded_category_and_unhealthy_value(self, factory):
        failure = gmail_client.GmailUnavailable(
            "requester@example.org secret response",
            category="provider_unavailable",
            status_code=503,
        )

        self.publish(failure=failure, duration_seconds=2.0)

        payload = factory.return_value.put_metric_data.call_args.kwargs
        metrics = payload["MetricData"]
        health = next(
            row for row in metrics if row["MetricName"] == "GmailInboundPollHealth"
        )
        failed = next(
            row for row in metrics if row["MetricName"] == "GmailInboundPollFailure"
        )
        self.assertEqual(health["Value"], 0)
        self.assertEqual(
            failed["Dimensions"],
            [
                {"Name": "Environment", "Value": "production"},
                {"Name": "FailureCategory", "Value": "provider_unavailable"},
            ],
        )
        self.assertNotIn("requester@example.org", repr(payload))
        self.assertNotIn("secret response", repr(payload))

    @override_settings(PRR_GMAIL_INBOUND_POLL_ENABLED=False)
    @patch("apps.correspondence.gmail_observability.boto3.client")
    def test_disabled_poller_reports_unhealthy_and_disabled_state(self, factory):
        self.publish(result={"status": "disabled"}, duration_seconds=0.01)

        metrics = {
            row["MetricName"]: row
            for row in factory.return_value.put_metric_data.call_args.kwargs[
                "MetricData"
            ]
        }
        self.assertEqual(metrics["GmailInboundPollHealth"]["Value"], 0)
        self.assertEqual(metrics["GmailInboundPollEnabled"]["Value"], 0)

    @patch("apps.correspondence.gmail_observability.boto3.client")
    def test_metric_failure_is_logged_without_masking_poll_result(self, factory):
        factory.return_value.put_metric_data.side_effect = RuntimeError(
            "AWS unavailable"
        )

        with self.assertLogs(
            "apps.correspondence.gmail_observability",
            level="ERROR",
        ) as logs:
            result = self.publish(
                result={"status": "ok", "received": 0},
                duration_seconds=0.5,
            )

        self.assertFalse(result)
        self.assertIn("CloudWatch", " ".join(logs.output))

    @override_settings(PRR_CLOUDWATCH_METRICS_ENABLED=False)
    @patch("apps.correspondence.gmail_observability.boto3.client")
    def test_disabled_telemetry_makes_no_aws_call(self, factory):
        self.assertFalse(
            self.publish(
                result={"status": "ok", "received": 0},
                duration_seconds=0.5,
            )
        )

        factory.assert_not_called()


class GmailInboundTaskTelemetryTests(SimpleTestCase):
    @patch("apps.correspondence.tasks.gmail_sync.poll_inbound_mailbox")
    @patch("apps.correspondence.tasks.publish_inbound_poll_metrics")
    def test_task_reports_success_result(self, publish, poll):
        poll.return_value = {"status": "ok", "received": 0}

        result = __import__(
            "apps.correspondence.tasks",
            fromlist=["poll_inbound_gmail"],
        ).poll_inbound_gmail.run()

        self.assertEqual(result, poll.return_value)
        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["result"], poll.return_value)
        self.assertNotIn("failure", publish.call_args.kwargs)

    @patch("apps.correspondence.tasks.gmail_sync.poll_inbound_mailbox")
    @patch("apps.correspondence.tasks.publish_inbound_poll_metrics")
    def test_task_reports_failure_then_reraises_original_error(self, publish, poll):
        failure = gmail_client.GmailUnavailable(
            "Provider unavailable.",
            category="provider_unavailable",
            status_code=503,
        )
        poll.side_effect = failure

        with self.assertRaises(gmail_client.GmailUnavailable):
            __import__(
                "apps.correspondence.tasks",
                fromlist=["poll_inbound_gmail"],
            ).poll_inbound_gmail.run()

        publish.assert_called_once()
        self.assertIs(publish.call_args.kwargs["failure"], failure)
        self.assertNotIn("result", publish.call_args.kwargs)
