"""API errors rendered as ``{"error": code, "detail": ...}``. Detail never carries secrets."""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


def not_found(what: str = "not found") -> ApiError:
    return ApiError(404, "not_found", what)


def forbidden(detail: str = "") -> ApiError:
    return ApiError(403, "forbidden", detail)


def conflict(detail: str) -> ApiError:
    return ApiError(409, "conflict", detail)


def unprocessable(detail: str) -> ApiError:
    return ApiError(422, "unprocessable", detail)
