from __future__ import annotations

import json

from app.audit.redaction import MAX_DETAILS_BYTES, sanitize_audit_details


def test_recursive_audit_redaction_blocks_credentials_containers_paths_and_tokens() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.signature"
    details = sanitize_audit_details(
        {
            "password": "plain-password",
            "csrf_token": "csrf-value",
            "headers": {"Cookie": "session=secret"},
            "environment": {"DATABASE_URL": "postgres://admin:secret@db/app"},
            "working_dir": "/home/private/application",
            "message": (
                f"authorization: Bearer bearer-value token=abc {jwt} "
                "https://example.test/path?api_key=url-secret&mode=safe"
            ),
            "nested": {"safe": "visible"},
        }
    )
    encoded = json.dumps(details)
    for secret in (
        "plain-password",
        "csrf-value",
        "bearer-value",
        "url-secret",
        "signature",
        "/home/private/application",
        "postgres://",
    ):
        assert secret not in encoded
    assert details["nested"]["safe"] == "visible"
    assert details["headers"] == "[REDACTED_CONTAINER]"
    assert details["working_dir"] == "[REDACTED_PATH]"


def test_large_audit_details_are_bounded_after_redaction() -> None:
    details = sanitize_audit_details(
        {
            **{f"message_{index}": "safe-data-" * 1000 for index in range(20)},
            "password": "must-never-appear",
        }
    )
    encoded = json.dumps(details).encode()
    assert len(encoded) < MAX_DETAILS_BYTES
    assert details["_truncated"] is True
    assert "must-never-appear" not in encoded.decode()
