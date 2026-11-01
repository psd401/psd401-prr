# ABOUTME: Classifies a record's media type into the review path it needs,
# ABOUTME: so the UI can state what is possible instead of failing on a click.
"""What can actually be done with a record, by media type.

A public records collection is not a pile of PDFs. A single district request
can pull in Office documents, email, phone photos, spreadsheets, bus and
building camera footage, call recordings, and the occasional dead format like
Flash. Those do not differ by degree - they need structurally different review
surfaces, and only one of them exists today.

This module is the single place that says which path a record is on. It
deliberately reports what is *possible*, never what is convenient:

* ``PREVIEW`` - renders and redacts today (PDF).
* ``CONVERT`` - a faithful paginated rendition is achievable, so the existing
  review and burn-in pipeline works once a derivative exists (Office, images,
  text, email).
* ``MEDIA`` - time-based. A page raster is meaningless; review needs a player
  and a timeline, and redaction means blurring a face across frames or muting
  a passage, not drawing a box on page 3. Camera footage is not an edge case
  in this system - the whole footage triage exists to preserve it.
* ``OPAQUE`` - nothing available can render it. The lawful moves are release
  in full, withhold in full, or describe it; pretending otherwise invites a
  reviewer to certify a file they never saw.

Being explicit about ``OPAQUE`` matters as much as the rest. A reviewer who
clicks a Flash bundle and gets a generic error learns nothing; a reviewer told
"no renderer exists for this format" knows the decision is theirs to make on
other evidence.
"""
from __future__ import annotations

from pathlib import PurePosixPath

#: Review paths, ordered from most to least capable.
PREVIEW = "preview"
CONVERT = "convert"
MEDIA = "media"
OPAQUE = "opaque"

HANDLING_LABELS = {
    PREVIEW: "Ready to review",
    CONVERT: "Needs conversion",
    MEDIA: "Audio or video",
    OPAQUE: "No renderer available",
}

#: Why a record is not reviewable yet, in words a records officer can act on.
HANDLING_DETAIL = {
    PREVIEW: "Opens in the reviewer and supports redaction.",
    CONVERT: (
        "A paginated rendition is needed before this can be read or redacted "
        "in the reviewer."
    ),
    MEDIA: (
        "Time-based media. Reviewing and redacting it needs a player and "
        "timeline rather than page rasters."
    ),
    OPAQUE: (
        "No available renderer can open this format. Decide from other "
        "evidence and record the reasoning."
    ),
}

_PREVIEW_TYPES = {"application/pdf"}

_CONVERT_TYPES = {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/rtf",
    "text/plain",
    "text/csv",
    "text/html",
    "text/xml",
    "application/xml",
    "application/json",
    "message/rfc822",
    "application/vnd.ms-outlook",
}

_CONVERT_PREFIXES = ("image/",)
_MEDIA_PREFIXES = ("video/", "audio/")

#: Extensions carry the answer when the stored media type is generic, which is
#: routine for files that arrived from another system rather than a browser.
_EXTENSION_HANDLING = {
    ".pdf": PREVIEW,
    ".doc": CONVERT, ".docx": CONVERT, ".rtf": CONVERT, ".odt": CONVERT,
    ".xls": CONVERT, ".xlsx": CONVERT, ".ods": CONVERT, ".csv": CONVERT,
    ".ppt": CONVERT, ".pptx": CONVERT, ".odp": CONVERT,
    ".txt": CONVERT, ".html": CONVERT, ".htm": CONVERT,
    ".xml": CONVERT, ".json": CONVERT, ".log": CONVERT, ".md": CONVERT,
    ".msg": CONVERT, ".eml": CONVERT,
    ".png": CONVERT, ".jpg": CONVERT, ".jpeg": CONVERT, ".gif": CONVERT,
    ".bmp": CONVERT, ".tif": CONVERT, ".tiff": CONVERT, ".webp": CONVERT,
    ".heic": CONVERT, ".heif": CONVERT,
    ".mp4": MEDIA, ".mov": MEDIA, ".avi": MEDIA, ".mkv": MEDIA, ".wmv": MEDIA,
    ".m4v": MEDIA, ".mpg": MEDIA, ".mpeg": MEDIA, ".webm": MEDIA,
    ".mp3": MEDIA, ".wav": MEDIA, ".m4a": MEDIA, ".aac": MEDIA,
    ".flac": MEDIA, ".ogg": MEDIA, ".wma": MEDIA,
    ".swf": OPAQUE, ".keg": OPAQUE, ".exe": OPAQUE, ".dll": OPAQUE,
    ".nib": OPAQUE, ".thmx": OPAQUE,
}


def normalized_content_type(content_type: str) -> str:
    return (content_type or "").lower().split(";", 1)[0].strip()


def handling_for(content_type: str = "", filename: str = "") -> str:
    """Classify one record by its media type, falling back to its extension.

    The media type wins when it says something specific. A generic
    ``application/octet-stream`` says nothing, so the extension decides - and
    when neither is informative the answer is ``OPAQUE``, because claiming a
    file is reviewable when nothing can open it is the costlier mistake.
    """
    media_type = normalized_content_type(content_type)
    if media_type in _PREVIEW_TYPES:
        return PREVIEW
    if media_type in _CONVERT_TYPES:
        return CONVERT
    if media_type.startswith(_MEDIA_PREFIXES):
        return MEDIA
    if media_type.startswith(_CONVERT_PREFIXES):
        return CONVERT
    suffix = PurePosixPath(filename or "").suffix.lower()
    return _EXTENSION_HANDLING.get(suffix, OPAQUE)


def handling_for_item(item) -> str:
    return handling_for(
        content_type=item.content_type,
        filename=item.original_filename,
    )
