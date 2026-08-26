"""Exception types raised by the release verification tooling."""

from __future__ import annotations


class BoomiError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(BoomiError):
    """Invalid or missing configuration supplied by the caller."""


class BoomiHTTPError(BoomiError):
    """A Boomi Platform API call returned an unsuccessful HTTP status."""

    def __init__(self, status: int, method: str, url: str, body: str = ""):
        self.status = status
        self.method = method
        self.url = url
        self.body = body
        snippet = body.strip()
        if len(snippet) > 500:
            snippet = snippet[:500] + "..."
        message = f"{method} {url} failed with HTTP {status}"
        if snippet:
            message = f"{message}: {snippet}"
        super().__init__(message)


class ReleaseNotReady(BoomiError):
    """ReleaseIntegrationPackStatus still returns 202 (IN_PROGRESS or SCHEDULED)."""

    def __init__(self, request_id: str, release_status: str = "IN_PROGRESS", progress: str = ""):
        self.request_id = request_id
        self.release_status = release_status
        self.progress = progress
        message = f"Release {request_id} is not finished yet (releaseStatus={release_status}"
        if progress:
            message += f", releaseProgress={progress}"
        super().__init__(message + ")")
