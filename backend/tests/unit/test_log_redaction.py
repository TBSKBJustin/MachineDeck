from pathlib import Path

from app.logging.redaction import LogRedactionService
from app.schemas.applications import ApplicationManifest


def redactor() -> LogRedactionService:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "compose"
    manifest = ApplicationManifest.model_validate(
        {
            "id": "redaction-app",
            "name": "Redaction",
            "runtime": {"type": "compose", "working_dir": str(fixture)},
            "environment": {
                "API_TOKEN": "exact-secret-value",
                "NORMAL_VALUE": "not-secret",
            },
        }
    )
    return LogRedactionService(manifest)


def test_secret_values_assignments_headers_jwt_and_urls_are_redacted() -> None:
    message = (
        "token=abc123 password: hunter2 Authorization: Bearer bearer-token "
        "exact-secret-value "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature "
        "https://example.test/path?api_key=url-secret&normal=visible"
    )
    output = redactor().redact(message)
    for secret in ("abc123", "hunter2", "bearer-token", "exact-secret-value", "url-secret"):
        assert secret not in output
    assert "normal=visible" in output
    assert "not-secret" not in output  # It was not present and is not spuriously inserted.


def test_normal_log_content_is_preserved() -> None:
    message = "Server started on http://127.0.0.1:8080/items?limit=20"
    assert redactor().redact(message) == message
