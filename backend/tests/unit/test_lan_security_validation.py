from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def load_validation_module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "validate-phase1.5-lan-security.py"
    )
    spec = importlib.util.spec_from_file_location(
        "validate_phase15_lan_security",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validation_module = load_validation_module()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://192.168.1.50:8080", "http://192.168.1.50:8080"),
        ("https://machine.example.ts.net/", "https://machine.example.ts.net"),
        ("http://[fd00::50]:8080", "http://[fd00::50]:8080"),
    ],
)
def test_parse_origin_accepts_exact_http_origins(value: str, expected: str) -> None:
    assert validation_module.parse_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "ftp://192.168.1.50:8080",
        "http://user@192.168.1.50:8080",
        "http://192.168.1.50:8080/dashboard",
        "http://192.168.1.50:8080?next=dashboard",
        "http://192.168.1.50:invalid",
    ],
)
def test_parse_origin_rejects_non_origin_urls(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        validation_module.parse_origin(value)
