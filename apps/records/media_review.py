# ABOUTME: Probing, redacting, and releasing time-based records, which cannot
# ABOUTME: be represented as pages and so need their own review path.
"""Review and redaction for audio and video records.

Everything else in this application reduces a record to pages. Time-based
media cannot be reduced that way: the exempt material is a face that appears
for eleven seconds, or a name spoken once in a forty-minute recording. Review
needs a player and a timeline, and redaction is applied by re-encoding rather
than by drawing a rectangle.

The legal shape is identical to paper even though the mechanics are not. Every
redaction cites an exemption, the citation is not optional, and the released
copy is a new derived object - the original is never modified, so the district
can always show what it started from.

Face detection is deliberately absent. An automated blur that misses one frame
discloses a student, and there is no way for a reviewer to verify a negative
across thousands of frames. A human marks the intervals; the machine applies
exactly what was marked.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import audit
from apps.configuration import runtime

from .handling import MEDIA, handling_for_item
from .models import MediaRedaction, RecordItem
from .storage import (
    generated_record_storage_key,
    load_verified_object,
    store_immutable_object,
)


class MediaToolUnavailable(RuntimeError):
    """The media toolchain is absent, so nothing can be claimed about a file."""


def _binary(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise MediaToolUnavailable(
            f"{name} is not installed, so audio and video records cannot be "
            "inspected or redacted in this environment."
        )
    return resolved


def probe_media(payload: bytes) -> dict:
    """Read duration and stream shape without decoding the whole file."""
    with tempfile.TemporaryDirectory() as workspace:
        source = Path(workspace) / "input"
        source.write_bytes(payload)
        completed = subprocess.run(
            [
                _binary("ffprobe"), "-v", "error",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(source),
            ],
            capture_output=True,
            timeout=runtime.get("PRR_MEDIA_TOOL_TIMEOUT_SECONDS"),
            check=False,
        )
    if completed.returncode != 0:
        raise ValidationError("This media record could not be read.")
    probed = json.loads(completed.stdout or b"{}")
    streams = probed.get("streams", [])
    duration = float(probed.get("format", {}).get("duration") or 0.0)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    return {
        "duration_seconds": duration,
        "has_video": video is not None,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "width": int(video.get("width") or 0) if video else 0,
        "height": int(video.get("height") or 0) if video else 0,
    }


def _media_redaction_manifest(item, redactions) -> str:
    """Digest of exactly what was applied, mirroring the paper manifest.

    Lets the district show later that the released copy corresponds to the
    marked redactions and no others.
    """
    payload = {
        "source_sha256": item.sha256,
        "redactions": [
            {
                "id": str(entry.pk),
                "starts_at": round(entry.starts_at, 3),
                "ends_at": round(entry.ends_at, 3),
                "treatment": entry.treatment,
                "coordinates": [entry.x0, entry.y0, entry.x1, entry.y1],
                "exemption_id": entry.exemption_id,
            }
            for entry in redactions
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _filters_for(redactions, probe) -> tuple[str, str]:
    """Build the ffmpeg filter graphs that apply exactly what was marked."""
    video_steps: list[str] = []
    audio_steps: list[str] = []
    for entry in redactions:
        window = f"between(t,{entry.starts_at:.3f},{entry.ends_at:.3f})"
        if entry.treatment == MediaRedaction.Treatment.MUTE_AUDIO:
            audio_steps.append(f"volume=enable='{window}':volume=0")
        elif entry.treatment == MediaRedaction.Treatment.OBSCURE_FRAME:
            video_steps.append(f"drawbox=x=0:y=0:w=iw:h=ih:color=black@1:t=fill:enable='{window}'")
        elif entry.treatment == MediaRedaction.Treatment.OBSCURE_REGION:
            video_steps.append(
                f"drawbox=x=iw*{entry.x0:.4f}:y=ih*{entry.y0:.4f}"
                f":w=iw*{(entry.x1 - entry.x0):.4f}:h=ih*{(entry.y1 - entry.y0):.4f}"
                f":color=black@1:t=fill:enable='{window}'"
            )
    return ",".join(video_steps), ",".join(audio_steps)


def _cut_segments(redactions, duration: float) -> list[tuple[float, float]]:
    """Intervals that survive after removing every CUT redaction."""
    cuts = sorted(
        (r.starts_at, r.ends_at)
        for r in redactions
        if r.treatment == MediaRedaction.Treatment.CUT
    )
    if not cuts:
        return [(0.0, duration)]
    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in cuts:
        if start > cursor:
            kept.append((cursor, min(start, duration)))
        cursor = max(cursor, end)
    if cursor < duration:
        kept.append((cursor, duration))
    return [(a, b) for a, b in kept if b - a > 0.001]


def create_media_redaction(
    record_item_id,
    *,
    actor,
    starts_at,
    ends_at,
    treatment,
    exemption_id,
    explanation="",
    x0=None, y0=None, x1=None, y1=None,
):
    """Mark one interval for withholding, with its citation.

    Validated here rather than only at the database, so a reviewer gets a
    sentence they can act on instead of a constraint name.
    """
    item = RecordItem.objects.select_related("request").get(pk=record_item_id)
    if handling_for_item(item) != MEDIA:
        raise ValidationError("Only audio or video records take timed redactions.")
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise ValidationError("A named reviewer is required.")
    try:
        starts_at = float(starts_at)
        ends_at = float(ends_at)
    except (TypeError, ValueError) as exc:
        raise ValidationError("An interval needs numeric start and end times.") from exc
    if starts_at < 0:
        raise ValidationError("An interval cannot start before the recording does.")
    if ends_at <= starts_at:
        raise ValidationError("An interval must end after it starts.")
    if treatment not in MediaRedaction.Treatment.values:
        raise ValidationError("An explicit treatment is required.")
    if treatment == MediaRedaction.Treatment.OBSCURE_REGION:
        box = (x0, y0, x1, y1)
        if any(value is None for value in box):
            raise ValidationError(
                "Obscuring a region needs the region. Without it the redaction "
                "would cover nothing while still reading as applied."
            )
        x0, y0, x1, y1 = (float(value) for value in box)
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ValidationError("The region must be normalized and ordered.")
    else:
        x0 = y0 = x1 = y1 = None

    entry = MediaRedaction.objects.create(
        record_item=item,
        starts_at=starts_at,
        ends_at=ends_at,
        treatment=treatment,
        x0=x0, y0=y0, x1=x1, y1=y1,
        exemption_id=exemption_id,
        explanation=(explanation or "")[:500],
        created_by=actor,
    )
    audit(
        "record.media_redaction_marked",
        target=str(item.pk),
        actor=actor,
        request_number=item.request.number,
        starts_at=starts_at,
        ends_at=ends_at,
        treatment=treatment,
        exemption_id=exemption_id,
    )
    return entry


def void_media_redaction(redaction_id, *, actor, reason):
    """Withdraw a marked interval, keeping the row and the reason."""
    entry = MediaRedaction.objects.select_related(
        "record_item__request"
    ).get(pk=redaction_id)
    if entry.voided_at is not None:
        raise ValidationError("This interval is already withdrawn.")
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValidationError("Record why the interval is being withdrawn.")
    entry.voided_at = timezone.now()
    entry.voided_by = actor
    entry.void_reason = reason[:500]
    entry.save(update_fields=["voided_at", "voided_by", "void_reason"])
    audit(
        "record.media_redaction_voided",
        target=str(entry.record_item_id),
        actor=actor,
        request_number=entry.record_item.request.number,
        reason=entry.void_reason,
    )
    return entry


def render_media_redactions(record_item_id, *, actor=None, _client=None):
    """Burn every active redaction into a new released copy.

    The original object is untouched. What the requester receives is this
    derived file, which is stored immutably with its own digest so the
    district can prove which copy was released.
    """
    item = RecordItem.objects.select_related("request").get(pk=record_item_id)
    if handling_for_item(item) != MEDIA:
        raise ValidationError("This record is not audio or video.")
    if item.review_state != RecordItem.ReviewState.RESPONSIVE:
        raise ValidationError(
            "Only a record marked responsive can have redactions applied."
        )
    redactions = list(
        item.media_redactions.filter(voided_at__isnull=True).order_by("starts_at")
    )
    if not redactions:
        raise ValidationError(
            "This record has no active redactions to apply."
        )

    source = load_verified_object(
        item.storage_key,
        expected_size=item.content_length,
        expected_sha256=item.sha256,
        _client=_client,
    )
    try:
        payload = source.read()
    finally:
        source.close()

    probe = probe_media(payload)
    duration = probe["duration_seconds"]
    if duration <= 0:
        raise ValidationError("This media record reports no duration.")
    for entry in redactions:
        if entry.ends_at > duration + 0.5:
            raise ValidationError(
                "A redaction extends past the end of the recording."
            )

    suffix = Path(item.original_filename or "media.mp4").suffix.lower() or ".mp4"
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        source_path = root / f"input{suffix}"
        source_path.write_bytes(payload)
        output_path = root / f"output{suffix}"
        video_filter, audio_filter = _filters_for(redactions, probe)
        segments = _cut_segments(redactions, duration)

        select = ""
        if len(segments) != 1 or segments[0] != (0.0, duration):
            spans = "+".join(
                f"between(t,{start:.3f},{end:.3f})" for start, end in segments
            )
            select = f"select='{spans}',setpts=N/FRAME_RATE/TB"
            audio_select = f"aselect='{spans}',asetpts=N/SR/TB"
            audio_filter = f"{audio_filter},{audio_select}" if audio_filter else audio_select
            video_filter = f"{video_filter},{select}" if video_filter else select

        command = [_binary("ffmpeg"), "-y", "-i", str(source_path)]
        if video_filter and probe["has_video"]:
            command += ["-vf", video_filter]
        if audio_filter and probe["has_audio"]:
            command += ["-af", audio_filter]
        # Re-encode rather than copy: a stream copy would leave the original
        # frames intact and only change the container, releasing exactly the
        # material the redaction was meant to remove.
        command += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
        if not probe["has_video"]:
            command = [c for c in command if c not in {"-c:v", "libx264", "-preset", "veryfast"}]
        command.append(str(output_path))

        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=runtime.get("PRR_MEDIA_RENDER_TIMEOUT_SECONDS"),
            check=False,
        )
        if completed.returncode != 0 or not output_path.is_file():
            detail = (completed.stderr or b"").decode("utf-8", "replace")[-400:]
            raise ValidationError(f"Redacted media could not be produced. {detail}")
        rendered = output_path.read_bytes()

    if not rendered:
        raise ValidationError("The redacted media render produced no output.")

    digest = hashlib.sha256(rendered).hexdigest()
    storage_key = generated_record_storage_key(
        item.request_id, original_filename=f"redacted{suffix}"
    )
    import io

    store_immutable_object(
        storage_key,
        io.BytesIO(rendered),
        content_length=len(rendered),
        sha256=digest,
        content_type=item.content_type or "application/octet-stream",
        _client=_client,
    )
    manifest_sha256 = _media_redaction_manifest(item, redactions)
    with transaction.atomic():
        item.redacted_storage_key = storage_key
        item.redacted_sha256 = digest
        item.redacted_content_length = len(rendered)
        item.redacted_content_type = item.content_type or "application/octet-stream"
        item.redacted_manifest_sha256 = manifest_sha256
        item.save(update_fields=[
            "redacted_storage_key", "redacted_sha256",
            "redacted_content_length", "redacted_content_type",
            "redacted_manifest_sha256",
        ])
        audit(
            "record.media_redactions_rendered",
            target=str(item.pk),
            actor=actor,
            request_number=item.request.number,
            redaction_count=len(redactions),
            citations=sorted({r.exemption.citation for r in redactions}),
            redacted_sha256=digest,
            manifest_sha256=manifest_sha256,
        )
    return item
