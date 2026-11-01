"""Streaming multipart limits for the boundaries that accept staff or public files."""

from django.core.files.uploadhandler import FileUploadHandler, StopUpload

from apps.configuration import runtime


class _MultipartLimitUploadHandler(FileUploadHandler):
    """Reject oversized multipart files before storage handlers see bytes.

    Subclasses name the three registered limits they enforce and the request
    attribute the view reads the refusal from. The limits are read once per
    request, on the first file, so every chunk of one upload is judged
    against the same values and no chunk costs a settings-store round trip.
    """

    chunk_size = 64 * 1024
    count_key = ""
    file_bytes_key = ""
    total_bytes_key = ""
    error_attribute = ""

    def __init__(self, request=None):
        super().__init__(request)
        self.file_count = 0
        self.current_file_bytes = 0
        self.total_file_bytes = 0
        self._limits = None

    def _limit(self, which):
        if self._limits is None:
            self._limits = {
                "count": runtime.get(self.count_key),
                "file": runtime.get(self.file_bytes_key),
                "total": runtime.get(self.total_bytes_key),
            }
        return self._limits[which]

    def _reject(self, detail):
        setattr(self.request, self.error_attribute, detail)
        raise StopUpload(connection_reset=True)

    def new_file(
        self,
        field_name,
        file_name,
        content_type,
        content_length,
        charset=None,
        content_type_extra=None,
    ):
        super().new_file(
            field_name,
            file_name,
            content_type,
            content_length,
            charset,
            content_type_extra,
        )
        self.file_count += 1
        self.current_file_bytes = 0
        if self.file_count > self._limit("count"):
            self._reject(
                "The multipart upload contains too many files."
            )
        if content_length is not None and content_length > self._limit("file"):
            self._reject(
                "A multipart file exceeds the per-file size limit."
            )
        if (
            content_length is not None
            and self.total_file_bytes + content_length > self._limit("total")
        ):
            self._reject(
                "The multipart files exceed the combined size limit."
            )

    def receive_data_chunk(self, raw_data, start):
        chunk_bytes = len(raw_data)
        next_file_bytes = self.current_file_bytes + chunk_bytes
        next_total_bytes = self.total_file_bytes + chunk_bytes
        if next_file_bytes > self._limit("file"):
            self._reject(
                "A multipart file exceeds the per-file size limit."
            )
        if next_total_bytes > self._limit("total"):
            self._reject(
                "The multipart files exceed the combined size limit."
            )
        self.current_file_bytes = next_file_bytes
        self.total_file_bytes = next_total_bytes
        return raw_data

    def file_complete(self, file_size):
        return None


class IntakeAttachmentLimitUploadHandler(_MultipartLimitUploadHandler):
    """Limits for files attached to a new request at intake."""

    count_key = "PRR_INTAKE_ATTACHMENT_MAX_COUNT"
    file_bytes_key = "PRR_INTAKE_ATTACHMENT_MAX_BYTES"
    total_bytes_key = "PRR_INTAKE_ATTACHMENT_TOTAL_MAX_BYTES"
    error_attribute = "_prr_intake_upload_limit_error"


class RecordUploadLimitUploadHandler(_MultipartLimitUploadHandler):
    """Limits for candidate records a Records Officer uploads onto a case."""

    count_key = "PRR_RECORD_UPLOAD_MAX_COUNT"
    file_bytes_key = "PRR_RECORD_UPLOAD_MAX_BYTES"
    total_bytes_key = "PRR_RECORD_UPLOAD_TOTAL_MAX_BYTES"
    error_attribute = "_prr_record_upload_limit_error"
