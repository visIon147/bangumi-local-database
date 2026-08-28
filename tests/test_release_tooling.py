from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "publish_public_release", Path("scripts/publish_public_release.py")
)
assert _SPEC is not None and _SPEC.loader is not None
_RELEASE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RELEASE)

PUBLIC_AUTHOR_EMAIL = _RELEASE.PUBLIC_AUTHOR_EMAIL
ReleaseError = _RELEASE.ReleaseError
_project_versions = _RELEASE._project_versions
_validate_version = _RELEASE._validate_version
_verify_content = _RELEASE._verify_content


def test_release_version_and_project_versions_are_consistent() -> None:
    assert _validate_version("1.0.0") == "1.0.0"
    assert _project_versions(Path.cwd()) == ("1.0.0", "1.0.0")
    with pytest.raises(ReleaseError):
        _validate_version("v1.0.0")


def test_release_content_scan_allows_examples_and_rejects_private_identity() -> None:
    safe = f"author={PUBLIC_AUTHOR_EMAIL}\ncontact=dev@example.com".encode()
    _verify_content("safe", safe)
    private_email = b"person" + b"@" + b"private.invalid"
    with pytest.raises(ReleaseError):
        _verify_content("unsafe", private_email)
