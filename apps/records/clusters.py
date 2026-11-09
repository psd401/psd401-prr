# ABOUTME: Group a case's records into clusters a reviewer can judge together:
# ABOUTME: one email thread, one sender or list, one tag, or one AI label band.
"""Record clusters for review (issue #38).

Every grouping reads stored facts only -- message headers, the deterministic
tags, and the latest suggestion the review list already shows. Clustering
decides nothing: a cluster is a way to look at records, and any decision on
them is still a human review.

* ``thread``: messages linked by ``In-Reply-To`` -> ``Message-ID`` within the
  set. Records with neither header share one "Not email" cluster.
* ``sender``: the mailing list when the message has a ``List-Id``, otherwise
  the sending domain.
* ``tag``: one cluster per tag; a record with two tags is in both.
* ``ai_label``: suggested state plus confidence band.
"""

from collections import Counter
from typing import Any

from django.core.exceptions import ValidationError

from .agreement import annotate_latest_suggestion, confidence_band
from .models import RecordItem
from .record_tags import TAG_LABELS, annotate_tags, counsel_domains, tags_for_item

GROUPINGS = ("thread", "sender", "tag", "ai_label")
SAMPLE_SIZE = 3


def _thread_keys(items) -> dict[Any, list[str]]:
    by_message_id = {item.email_message_id: item for item in items if item.email_message_id}
    parent = {item.pk: item.pk for item in items}

    def root(pk):
        while parent[pk] != pk:
            parent[pk] = parent[parent[pk]]
            pk = parent[pk]
        return pk

    for item in items:
        replied_to = by_message_id.get(item.email_in_reply_to)
        if replied_to is not None:
            parent[root(item.pk)] = root(replied_to.pk)
    return {
        item.pk: [
            str(root(item.pk)) if item.email_message_id or item.email_in_reply_to else ""
        ]
        for item in items
    }


def _sender_key(item) -> str:
    if item.email_list_id:
        return f"list:{item.email_list_id.lower()}"
    if item.email_sender_domain:
        return f"domain:{item.email_sender_domain.lower()}"
    return ""


def _ai_key(item) -> str:
    label = getattr(item, "advisory_suggested_state", None)
    if not label:
        return ""
    return f"{label}:{confidence_band(getattr(item, 'advisory_confidence', None))}"


def _label(grouping, key, members) -> str:
    if grouping == "thread":
        return members[0].original_filename if key else "Not email"
    if grouping == "sender":
        return key.split(":", 1)[1] if key else "No sender"
    if grouping == "tag":
        return str(TAG_LABELS.get(key) or key) if key else "No tag"
    if not key:
        return "No suggestion"
    label, band = key.split(":", 1)
    return f"{label.replace('_', '-')} ({band})"


def _sample(item) -> dict[str, Any]:
    return {
        "id": str(item.pk),
        "original_filename": item.original_filename,
        "review_state": item.review_state,
        "advisory_suggested_state": getattr(item, "advisory_suggested_state", None) or None,
        "advisory_confidence": getattr(item, "advisory_confidence", None),
    }


def cluster_records(records, grouping: str) -> list[dict[str, Any]]:
    """Clusters over ``records`` (a queryset or list), largest first."""
    if grouping not in GROUPINGS:
        raise ValidationError(f"Group by one of: {', '.join(GROUPINGS)}.")
    pks = [item.pk for item in records] if isinstance(records, list) else records.values("pk")
    counsel = counsel_domains()
    items = list(
        annotate_latest_suggestion(
            annotate_tags(RecordItem.objects.filter(pk__in=pks), counsel=counsel),
        ).order_by("email_sent_at", "created_at", "pk")
    )
    if grouping == "thread":
        keys = _thread_keys(items)
    elif grouping == "sender":
        keys = {item.pk: [_sender_key(item)] for item in items}
    elif grouping == "tag":
        keys = {item.pk: tags_for_item(item, counsel=counsel) or [""] for item in items}
    else:
        keys = {item.pk: [_ai_key(item)] for item in items}

    groups: dict[str, list[RecordItem]] = {}
    for item in items:
        for key in keys[item.pk]:
            groups.setdefault(key, []).append(item)

    clusters = []
    for key, members in groups.items():
        suggested = Counter(
            label
            for item in members
            if (label := getattr(item, "advisory_suggested_state", None))
        )
        majority = suggested.most_common(1)
        clusters.append({
            "key": key,
            "label": _label(grouping, key, members),
            "size": len(members),
            "ai_majority": {"label": majority[0][0], "count": majority[0][1]} if majority else None,
            "samples": [_sample(item) for item in members[:SAMPLE_SIZE]],
            "record_item_ids": [str(item.pk) for item in members],
        })
    # Largest first; the catch-all cluster (empty key) last among equals.
    clusters.sort(key=lambda cluster: (-cluster["size"], cluster["key"] == "", cluster["key"]))
    return clusters
