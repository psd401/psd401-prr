# ABOUTME: Stable, counted keyset pagination for append-only activity feeds.
# ABOUTME: Signed compound cursors prevent front-insert races and timestamp ties.
import hashlib
import json
from datetime import date, datetime

from django.core import signing
from django.db.models import Q
from rest_framework.exceptions import NotFound
from rest_framework.pagination import BasePagination, PageNumberPagination
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.utils.urls import replace_query_param


class CountedKeysetPagination(BasePagination):
    """Forward keyset pagination with the project's normal page envelope."""

    cursor_query_param = "cursor"
    cursor_salt = "apps.api.pagination.CountedKeysetPagination"
    invalid_cursor_message = "Invalid cursor"
    ordering = ("-created_at", "-id")
    page_size = api_settings.PAGE_SIZE

    def paginate_queryset(self, queryset, request, view=None):
        del view
        self.request = request
        self.base_url = request.build_absolute_uri()
        if "page" in request.query_params:
            raise NotFound(
                "This feed uses cursor pagination; follow the next link unchanged."
            )
        self.count = queryset.count()

        primary, tie_breaker = self._ordering_fields()
        position = self._decode_cursor(request)
        if position is not None:
            primary_value, tie_breaker_value = position
            queryset = queryset.filter(
                Q(**{f"{primary}__lt": primary_value})
                | Q(
                    **{
                        primary: primary_value,
                        f"{tie_breaker}__lt": tie_breaker_value,
                    }
                )
            )

        rows = list(queryset.order_by(*self.ordering)[: self.page_size + 1])
        self.page = rows[: self.page_size]
        self.has_next = len(rows) > self.page_size
        return self.page

    def _ordering_fields(self):
        if len(self.ordering) != 2 or any(not field.startswith("-") for field in self.ordering):
            raise AssertionError(
                "CountedKeysetPagination requires two descending ordering fields."
            )
        return tuple(field.removeprefix("-") for field in self.ordering)

    def _decode_cursor(self, request):
        encoded = request.query_params.get(self.cursor_query_param)
        if encoded is None:
            return None
        try:
            position = signing.loads(encoded, salt=self.cursor_salt)
        except signing.BadSignature as exc:
            raise NotFound(self.invalid_cursor_message) from exc
        if (
            not isinstance(position, list)
            or len(position) != 2
            or any(not isinstance(value, str) for value in position)
        ):
            raise NotFound(self.invalid_cursor_message)
        return position

    @staticmethod
    def _serialize_position(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)

    def get_next_link(self):
        if not self.has_next:
            return None
        primary, tie_breaker = self._ordering_fields()
        last = self.page[-1]
        position = [
            self._serialize_position(getattr(last, primary)),
            self._serialize_position(getattr(last, tie_breaker)),
        ]
        encoded = signing.dumps(position, salt=self.cursor_salt)
        return replace_query_param(self.base_url, self.cursor_query_param, encoded)

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.count,
                "next": self.get_next_link(),
                "previous": None,
                "results": data,
            }
        )

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "required": ["count", "results"],
            "properties": {
                "count": {"type": "integer", "example": 123},
                "next": {
                    "type": "string",
                    "nullable": True,
                    "format": "uri",
                },
                "previous": {
                    "type": "string",
                    "nullable": True,
                    "format": "uri",
                },
                "results": schema,
            },
        }

    def get_schema_operation_parameters(self, view):
        del view
        return [
            {
                "name": self.cursor_query_param,
                "required": False,
                "in": "query",
                "description": "Opaque position for the next page.",
                "schema": {"type": "string"},
            }
        ]


class ScopedKeysetPagination(BasePagination):
    """Signed keyset pagination bound to the complete filtered request scope."""

    cursor_query_param = "cursor"
    cursor_salt = "apps.api.pagination.ScopedKeysetPagination"
    invalid_cursor_message = "Invalid cursor"
    ordering = ("-created_at", "-id")
    page_size = min(api_settings.PAGE_SIZE, 50)

    def paginate_queryset(self, queryset, request, view=None):
        self.request = request
        self.view = view
        self.base_url = request.build_absolute_uri()
        if "page" in request.query_params:
            raise NotFound(
                "This feed uses cursor pagination; follow the next link unchanged."
            )
        if view is not None and hasattr(view, "get_pagination_ordering"):
            self.ordering = tuple(view.get_pagination_ordering())
        self._validate_ordering()
        self.scope = self._scope_digest(request, view)
        self.count = queryset.count()

        position = self._decode_cursor(request)
        if position is not None:
            queryset = queryset.filter(self._after_position(position))

        rows = list(queryset.order_by(*self.ordering)[: self.page_size + 1])
        self.page = rows[: self.page_size]
        self.has_next = len(rows) > self.page_size
        return self.page

    def _validate_ordering(self):
        if len(self.ordering) < 2:
            raise AssertionError(
                "ScopedKeysetPagination requires a deterministic tie-breaker."
            )
        names = [field.removeprefix("-") for field in self.ordering]
        if len(names) != len(set(names)) or any(not name for name in names):
            raise AssertionError("Pagination ordering fields must be unique.")

    def _scope_digest(self, request, view):
        if view is not None and hasattr(view, "get_pagination_scope"):
            query = view.get_pagination_scope()
        else:
            query = {
                key: sorted(values)
                for key, values in sorted(request.query_params.lists())
                if key != self.cursor_query_param
            }
        canonical = json.dumps(
            {"path": request.path, "query": query},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(canonical).hexdigest()

    def _decode_cursor(self, request):
        if self.view is not None and hasattr(
            self.view,
            "get_pagination_cursor",
        ):
            encoded = self.view.get_pagination_cursor()
        else:
            encoded = request.query_params.get(self.cursor_query_param)
        if encoded is None:
            return None
        try:
            payload = signing.loads(encoded, salt=self.cursor_salt)
        except signing.BadSignature as exc:
            raise NotFound(self.invalid_cursor_message) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or payload.get("scope") != self.scope
            or not isinstance(payload.get("position"), list)
            or len(payload["position"]) != len(self.ordering)
            or any(not isinstance(value, str) for value in payload["position"])
        ):
            raise NotFound(self.invalid_cursor_message)
        return payload["position"]

    def _after_position(self, position):
        boundary = Q(pk__in=[])
        prefix = {}
        for ordering, value in zip(self.ordering, position, strict=True):
            descending = ordering.startswith("-")
            field = ordering.removeprefix("-")
            comparison = "lt" if descending else "gt"
            boundary |= Q(**prefix, **{f"{field}__{comparison}": value})
            prefix[field] = value
        return boundary

    @staticmethod
    def _row_value(row, field):
        return row[field] if isinstance(row, dict) else getattr(row, field)

    @staticmethod
    def _serialize_position(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)

    def get_next_link(self):
        if not self.has_next or not self.page:
            return None
        last = self.page[-1]
        position = [
            self._serialize_position(
                self._row_value(last, ordering.removeprefix("-"))
            )
            for ordering in self.ordering
        ]
        encoded = signing.dumps(
            {"version": 1, "scope": self.scope, "position": position},
            salt=self.cursor_salt,
        )
        if self.view is not None and getattr(
            self.view,
            "pagination_cursor_in_body",
            False,
        ):
            return encoded
        return replace_query_param(self.base_url, self.cursor_query_param, encoded)

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.count,
                "next": self.get_next_link(),
                "previous": None,
                "results": data,
            }
        )

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "required": ["count", "results"],
            "properties": {
                "count": {"type": "integer", "example": 123},
                "next": {
                    "type": "string",
                    "nullable": True,
                    "format": "uri",
                },
                "previous": {
                    "type": "string",
                    "nullable": True,
                    "format": "uri",
                },
                "results": schema,
            },
        }

    def get_schema_operation_parameters(self, view):
        del view
        return [
            {
                "name": self.cursor_query_param,
                "required": False,
                "in": "query",
                "description": (
                    "Opaque signed cursor bound to this request and filter scope."
                ),
                "schema": {"type": "string"},
            }
        ]


class CaseListPagination(PageNumberPagination):
    """The case list's page: 50 by default, up to 200 when asked.

    Today shows every open case at once; one request of up to 200 is faster
    than walking 50-case pages one after another.
    """

    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 200
