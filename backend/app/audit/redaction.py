from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_DETAILS_BYTES = 16 * 1024
MAX_STRING_LENGTH = 4096
MAX_COLLECTION_ITEMS = 100
MAX_DEPTH = 8

SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|credential|csrf|cookie|session|token|api[_-]?key|secret|authorization)",
    re.I,
)
SENSITIVE_CONTAINER = re.compile(
    r"^(?:env|environment|headers?|request_body|login_request|credentials)$", re.I
)
SENSITIVE_PATH = re.compile(
    r"(?:^|_)(?:path|working_dir|compose_file|env_file)$", re.I
)
ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|csrf|cookie|session|token|api[_-]?key|secret|authorization)\b"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/-]+=*")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
URL = re.compile(r"https?://[^\s<>\"]+")


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
        query = [
            (name, "[REDACTED]" if SENSITIVE_KEY.search(name) else value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment))
    except ValueError:
        return "[REDACTED_URL]"


def redact_text(value: str) -> str:
    bounded = value[:MAX_STRING_LENGTH]
    redacted = BEARER.sub(r"\1[REDACTED]", bounded)
    redacted = ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted
    )
    redacted = JWT.sub("[REDACTED_JWT]", redacted)
    return URL.sub(_redact_url, redacted)


def _sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if SENSITIVE_CONTAINER.fullmatch(key):
        return "[REDACTED_CONTAINER]"
    if SENSITIVE_PATH.search(key):
        return "[REDACTED_PATH]"
    if depth >= MAX_DEPTH:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, dict):
        output = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                output["_truncated_items"] = len(value) - MAX_COLLECTION_ITEMS
                break
            name = str(child_key)[:200]
            output[name] = _sanitize(child_value, key=name, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        output = [
            _sanitize(item, depth=depth + 1)
            for item in value[:MAX_COLLECTION_ITEMS]
        ]
        if len(value) > MAX_COLLECTION_ITEMS:
            output.append(f"[TRUNCATED_{len(value) - MAX_COLLECTION_ITEMS}_ITEMS]")
        return output
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def sanitize_audit_details(details: dict[str, Any] | None) -> dict[str, Any]:
    sanitized = _sanitize(details or {})
    encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_DETAILS_BYTES:
        return sanitized
    preview = encoded[:4096].decode("utf-8", errors="ignore")
    return {
        "_truncated": True,
        "original_sanitized_size_bytes": len(encoded),
        "preview": redact_text(preview),
    }
