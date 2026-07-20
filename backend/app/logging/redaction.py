from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.schemas.applications import ApplicationManifest


SENSITIVE_NAME = re.compile(r"(?:password|passwd|token|api[_-]?key|secret|authorization)", re.I)
ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|api[_-]?key|secret|authorization)\b"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/-]+=*")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
URL = re.compile(r"https?://[^\s<>\"]+")


class LogRedactionService:
    def __init__(self, manifest: ApplicationManifest) -> None:
        self.secret_values = sorted(
            {
                value
                for name, value in manifest.environment.items()
                if value and len(value) >= 4 and SENSITIVE_NAME.search(name)
            },
            key=len,
            reverse=True,
        )

    @staticmethod
    def _redact_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            query = [
                (name, "[REDACTED]" if SENSITIVE_NAME.search(name) else value)
                for name, value in parse_qsl(parts.query, keep_blank_values=True)
            ]
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
        except ValueError:
            return raw

    def redact(self, message: str) -> str:
        redacted = message
        for value in self.secret_values:
            redacted = redacted.replace(value, "[REDACTED]")
        redacted = BEARER.sub(r"\1[REDACTED]", redacted)
        redacted = ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
        redacted = JWT.sub("[REDACTED_JWT]", redacted)
        return URL.sub(self._redact_url, redacted)
