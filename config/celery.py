import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
app = Celery("psd401_prr")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "deadline-watchdog-hourly": {
        "task": "apps.records.tasks.deadline_watchdog",
        "schedule": crontab(minute=0),  # hourly; cheap and the stakes are high
    },
    "poll-vault-exports": {
        "task": "apps.records.tasks.poll_vault_exports",
        "schedule": crontab(minute="*/10"),
    },
    "prepare-record-review-advisories": {
        "task": "apps.records.tasks.prepare_record_review_advisories",
        "schedule": crontab(minute="*/5"),
        "kwargs": {"limit": 25},
    },
    "build-pending-record-derivatives": {
        "task": "apps.records.tasks.build_pending_record_derivatives",
        "schedule": crontab(minute="*/10"),
        "kwargs": {"limit": 25},
    },
    "auto-acknowledge-safety-net": {
        "task": "apps.records.tasks.auto_acknowledge_safety_net",
        "schedule": crontab(minute=30),  # hourly at :30, offset from watchdog
    },
    "recover-stuck-correspondence-deliveries": {
        "task": (
            "apps.correspondence.tasks."
            "watch_stuck_correspondence_deliveries"
        ),
        "schedule": crontab(minute="*"),
        "kwargs": {"limit": 100},
    },
    "recover-stale-correspondence-attachment-scans": {
        "task": (
            "apps.correspondence.tasks."
            "requeue_stale_correspondence_attachment_scans"
        ),
        "schedule": crontab(minute="*"),
    },
    "recover-pending-correspondence-attachment-cleanups": {
        "task": (
            "apps.correspondence.tasks."
            "requeue_pending_correspondence_attachment_cleanups"
        ),
        "schedule": crontab(minute="*"),
    },
    "recover-stale-inbound-quarantine-writes": {
        "task": (
            "apps.correspondence.tasks."
            "requeue_stale_inbound_quarantine_writes"
        ),
        "schedule": crontab(minute="*"),
    },
    "poll-inbound-gmail": {
        "task": "apps.correspondence.tasks.poll_inbound_gmail",
        "schedule": crontab(minute="*"),
    },
    "reconcile-inbound-gmail": {
        "task": "apps.correspondence.tasks.reconcile_inbound_gmail",
        "schedule": crontab(minute=45),  # hourly, offset from the :00/:30 jobs
    },
    "alert-waiting-inbound-triage": {
        "task": "apps.work_queue.tasks.alert_waiting_inbound_triage_daily",
        "schedule": crontab(day_of_week="mon-fri", hour=7, minute=45),
    },
    "reconcile-inbound-work-items": {
        "task": "apps.work_queue.tasks.reconcile_inbound_work_item_backfill",
        "schedule": crontab(minute="*/5"),
        "kwargs": {"limit": 250},
    },
    "reconcile-case-work-items": {
        "task": "apps.work_queue.tasks.reconcile_case_work_item_backfill",
        "schedule": crontab(minute="*/5"),
        "kwargs": {"source": "requests", "limit": 250},
    },
    "estimate-watchdog-daily": {
        "task": "apps.records.tasks.estimate_watchdog",
        "schedule": crontab(hour=7, minute=0),
    },
    "unpaid-fee-watchdog-daily": {
        "task": "apps.records.tasks.unpaid_fee_watchdog",
        "schedule": crontab(hour=7, minute=15),
    },
    "stall-detector-weekly": {
        "task": "apps.records.tasks.stall_detector",
        "schedule": crontab(day_of_week="mon", hour=7, minute=30),
    },
}
