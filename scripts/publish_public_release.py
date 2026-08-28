from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile


PUBLIC_ROOTS = {
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "UI_GUIDE.md",
    "STEAM_SETUP.md",
    "release_public.ps1",
    "alembic",
    "alembic.ini",
    "config",
    "db",
    "pyproject.toml",
    "scripts",
    "src",
    "tests",
    "uv.lock",
}
PUBLIC_DOCUMENTS = (
    "README.md",
    "UI_GUIDE.md",
    "STEAM_SETUP.md",
    "CHANGELOG.md",
    "LICENSE",
)
FORBIDDEN_PREFIXES = (
    "docs/",
    "prompts/",
    "data/",
    "backups/",
    "plans/",
    "exports/",
    "covers/",
    "media-cache/",
    "cache/",
    "logs/",
    "data-manifests/",
)
FORBIDDEN_ROOT_FILES = {"AGENTS.md", "MANIFEST.md", ".env"}
FORBIDDEN_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".bundle",
    ".pyc",
)
FORBIDDEN_PATH_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "media-cache",
    "media_cache",
}
SECRET_PATTERNS = (
    re.compile(rb"BANGUMI_ACCESS_TOKEN[ \t]*=[ \t]*(?!replace-)[A-Za-z0-9_-]{16,}", re.MULTILINE),
    re.compile(rb"STEAM_WEB_API_KEY[ \t]*=[ \t]*(?!replace-)[A-Za-z0-9_-]{16,}", re.MULTILINE),
    re.compile(rb"Bearer\s+[A-Za-z0-9._-]{20,}"),
    re.compile(rb"ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"[A-Za-z]:\\\\Users\\\\[^\\\r\n]+", re.IGNORECASE),
    re.compile(rb"[A-Za-z]:\\\\MyDocuments\\\\", re.IGNORECASE),
    re.compile(rb"\b(?!76561197960265728\b)7656119[0-9]{10}\b"),
)
EMAIL_PATTERN = re.compile(rb"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
SEMVER_PATTERN = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
PUBLIC_AUTHOR_NAME = "visIon147"
PUBLIC_AUTHOR_EMAIL = "visIon147@users.noreply.github.com"


class ReleaseError(RuntimeError):
    pass


def run(
    *args: str,
    input_bytes: bytes | None = None,
    capture: bool = True,
    environment: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> bytes:
    result = subprocess.run(
        args,
        input=input_bytes,
        check=True,
        stdout=subprocess.PIPE if capture else None,
        env=environment,
        cwd=cwd,
    )
    return result.stdout if result.stdout is not None else b""


def text_run(
    *args: str,
    environment: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> str:
    return run(*args, environment=environment, cwd=cwd).decode("utf-8").strip()


def git(
    *args: str,
    input_bytes: bytes | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    return run(
        "git", *args, input_bytes=input_bytes, environment=environment
    ).decode("utf-8").strip()


def _validate_version(version: str) -> str:
    if SEMVER_PATTERN.fullmatch(version) is None:
        raise ReleaseError("Version must use A.B.C without a leading v.")
    return version


def _project_versions(root: Path) -> tuple[str, str]:
    with (root / "pyproject.toml").open("rb") as stream:
        package_version = str(tomllib.load(stream)["project"]["version"])
    init_text = (root / "src/bangumi_local/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    if match is None:
        raise ReleaseError("Runtime __version__ is missing.")
    return package_version, match.group(1)


def _public_root_tree(head: str) -> str:
    entries: list[str] = []
    for line in git("ls-tree", head).splitlines():
        _, name = line.split("\t", 1)
        if name in PUBLIC_ROOTS:
            entries.append(line)
    public_names = {line.split("\t", 1)[1] for line in entries}
    for required in PUBLIC_DOCUMENTS:
        if required not in public_names:
            raise ReleaseError(f"{required} is missing from the public tree.")
    return git("mktree", input_bytes="\n".join(entries).encode("utf-8") + b"\n")


def _tree_files(tree: str) -> tuple[str, ...]:
    return tuple(git("ls-tree", "-r", "--name-only", tree).splitlines())


def _verify_paths(files: tuple[str, ...]) -> None:
    for name in files:
        normalized = name.replace("\\", "/")
        root = normalized.split("/", 1)[0]
        lowered = normalized.casefold()
        parts = {part.casefold() for part in normalized.split("/")}
        if root in FORBIDDEN_ROOT_FILES or normalized.startswith(FORBIDDEN_PREFIXES):
            raise ReleaseError(f"Forbidden path in public tree: {normalized}")
        if parts & FORBIDDEN_PATH_PARTS:
            raise ReleaseError(f"Forbidden cache path in public tree: {normalized}")
        if lowered.endswith(FORBIDDEN_SUFFIXES):
            raise ReleaseError(f"Forbidden generated/private file: {normalized}")
        if root not in PUBLIC_ROOTS:
            raise ReleaseError(f"Path is not in the public allowlist: {normalized}")


def _verify_content(name: str, content: bytes) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(content):
            raise ReleaseError(f"Sensitive content pattern found in: {name}")
    for match in EMAIL_PATTERN.finditer(content):
        address = match.group(0).decode("ascii", errors="ignore").casefold()
        if not address.endswith(
            ("@users.noreply.github.com", "@example.com", "@example.org", "@example.net")
        ):
            raise ReleaseError(f"Non-noreply email address found in: {name}")


def _verify_blob_contents(tree: str, files: tuple[str, ...]) -> None:
    for name in files:
        _verify_content(name, run("git", "show", f"{tree}:{name}"))


def _materialize_tree(tree: str, destination: Path) -> None:
    archive_bytes = run("git", "archive", "--format=tar", tree)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        root = destination.resolve()
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise ReleaseError(f"Unsafe archive member: {member.name}")
        archive.extractall(destination, filter="data")


def _archive_names(path: Path) -> tuple[str, ...]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return tuple(archive.namelist())
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return tuple(member.name for member in archive.getmembers())
    return ()


def _verify_archive_blob_contents(path: Path) -> None:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    _verify_content(f"{path.name}:{info.filename}", archive.read(info))
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if stream is not None:
                    _verify_content(f"{path.name}:{member.name}", stream.read())


def _verify_artifact_paths(path: Path) -> None:
    relative_names: list[str] = []
    for raw_name in _archive_names(path):
        name = raw_name.replace("\\", "/")
        parts = name.split("/")
        relative = "/".join(parts[1:]) if path.name.endswith(".tar.gz") else name
        relative_names.append(relative)
        root = relative.split("/", 1)[0]
        parts_set = {part.casefold() for part in relative.split("/")}
        if root in FORBIDDEN_ROOT_FILES or relative.startswith(FORBIDDEN_PREFIXES):
            raise ReleaseError(f"Forbidden development/private path in {path.name}: {relative}")
        if parts_set & FORBIDDEN_PATH_PARTS or relative.casefold().endswith(FORBIDDEN_SUFFIXES):
            raise ReleaseError(f"Forbidden generated/private path in {path.name}: {relative}")

    if path.suffix == ".whl":
        required_prefixes = (
            "bangumi_local/web/templates/",
            "bangumi_local/web/static/",
            "bangumi_local/migrations/versions/",
            "bangumi_local/resources/",
        )
        required_files = tuple(
            f"bangumi_local/web/content/{name}" for name in PUBLIC_DOCUMENTS
        )
        required_files += ("bangumi_local/resources/env.example", "bangumi_local/resources/steam.example.toml")
    elif path.name.endswith(".tar.gz"):
        required_prefixes = (
            "src/bangumi_local/web/templates/",
            "src/bangumi_local/web/static/",
            "alembic/versions/",
            "src/bangumi_local/resources/",
        )
        required_files = PUBLIC_DOCUMENTS
    else:
        return
    for prefix in required_prefixes:
        if not any(name.startswith(prefix) for name in relative_names):
            raise ReleaseError(f"Required runtime content missing from {path.name}: {prefix}")
    for required in required_files:
        if required not in relative_names:
            raise ReleaseError(f"Required public content missing from {path.name}: {required}")


def _build_and_verify(tree: str, version: str, output_directory: Path) -> tuple[Path, ...]:
    with tempfile.TemporaryDirectory(prefix="bld-public-tree-") as directory:
        root = Path(directory)
        _materialize_tree(tree, root)
        package_version, runtime_version = _project_versions(root)
        if package_version != version or runtime_version != version:
            raise ReleaseError(
                f"Version mismatch: requested={version}, package={package_version}, runtime={runtime_version}"
            )
        run("uv", "run", "pytest", "-q", capture=False, cwd=root)
        with tempfile.TemporaryDirectory(prefix="bld-public-schema-") as schema_directory:
            environment = os.environ.copy()
            database = (Path(schema_directory) / "release-check.sqlite3").as_posix()
            environment["BLD_DATABASE_URL"] = f"sqlite:///{database}"
            run("uv", "run", "alembic", "upgrade", "head", capture=False, environment=environment, cwd=root)
            run("uv", "run", "alembic", "check", capture=False, environment=environment, cwd=root)
        with tempfile.TemporaryDirectory(prefix="bld-public-build-") as build_directory:
            build_root = Path(build_directory)
            run("uv", "build", "--out-dir", str(build_root), capture=False, cwd=root)
            generated = tuple(sorted(build_root.iterdir()))
            unexpected = tuple(
                item
                for item in generated
                if item.name != ".gitignore"
                and item.suffix != ".whl"
                and not item.name.endswith(".tar.gz")
            )
            if unexpected:
                raise ReleaseError(
                    f"Unexpected build output: {[item.name for item in unexpected]}"
                )
            artifacts = tuple(
                item
                for item in generated
                if item.suffix == ".whl" or item.name.endswith(".tar.gz")
            )
            expected_names = {
                f"bangumi_local_database-{version}-py3-none-any.whl",
                f"bangumi_local_database-{version}.tar.gz",
            }
            if {item.name for item in artifacts} != expected_names:
                raise ReleaseError(f"Unexpected build artifacts: {[item.name for item in artifacts]}")
            output_directory.mkdir(parents=True, exist_ok=True)
            copied: list[Path] = []
            for artifact in artifacts:
                _verify_archive_blob_contents(artifact)
                _verify_artifact_paths(artifact)
                destination = output_directory / artifact.name
                shutil.copy2(artifact, destination)
                copied.append(destination)

    checksum_path = output_directory / "SHA256SUMS.txt"
    checksum_lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in copied
    ]
    checksum_path.write_text("\n".join(checksum_lines) + "\n", encoding="ascii", newline="\n")
    return (*copied, checksum_path)


def _optional_ref(name: str) -> str | None:
    result = subprocess.run(
        ("git", "rev-parse", "--verify", "--quiet", name),
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _remote_ref(name: str) -> str | None:
    output = git("ls-remote", "origin", name)
    if not output:
        return None
    return output.split("\t", 1)[0]


def _public_commit(tree: str, version: str, *, fresh_root: bool) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": PUBLIC_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": PUBLIC_AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": PUBLIC_AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": PUBLIC_AUTHOR_EMAIL,
        }
    )
    arguments = ["commit-tree", tree, "-m", f"release: v{version}"]
    parent = None if fresh_root else _optional_ref("refs/heads/public-release")
    if parent is not None:
        arguments.extend(("-p", parent))
    commit = git(*arguments, environment=environment)
    metadata = git("show", "-s", "--format=%an%n%ae%n%cn%n%ce", commit).splitlines()
    if metadata != [PUBLIC_AUTHOR_NAME, PUBLIC_AUTHOR_EMAIL, PUBLIC_AUTHOR_NAME, PUBLIC_AUTHOR_EMAIL]:
        raise ReleaseError("Public commit metadata was not sanitized.")
    if fresh_root and git("rev-list", "--count", commit) != "1":
        raise ReleaseError("Fresh-root release unexpectedly has parent history.")
    return commit


def _create_bundle(version: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = Path("backups/releases") / f"pre-v{version}-{stamp}.bundle"
    destination.parent.mkdir(parents=True, exist_ok=True)
    run("git", "bundle", "create", str(destination), "--all", capture=False)
    run("git", "bundle", "verify", str(destination), capture=False)
    return destination


def publish(version: str, *, fresh_root: bool, push: bool) -> tuple[str, tuple[Path, ...], Path]:
    version = _validate_version(version)
    if git("branch", "--show-current") != "development-private":
        raise ReleaseError("Run releases only from local development-private.")
    if git("status", "--porcelain"):
        raise ReleaseError("The complete worktree must be clean before release.")

    package_version, runtime_version = _project_versions(Path.cwd())
    if package_version != version or runtime_version != version:
        raise ReleaseError(
            f"Version mismatch: requested={version}, package={package_version}, runtime={runtime_version}"
        )
    tag_ref = f"refs/tags/v{version}"
    if _optional_ref(tag_ref) is not None or _remote_ref(tag_ref) is not None:
        raise ReleaseError(f"Tag v{version} already exists.")

    bundle = _create_bundle(version)
    head = git("rev-parse", "HEAD")
    tree = _public_root_tree(head)
    files = _tree_files(tree)
    _verify_paths(files)
    _verify_blob_contents(tree, files)
    output_directory = Path("dist") / f"release-v{version}"
    if output_directory.exists():
        expected_parent = (Path.cwd() / "dist").resolve()
        resolved_output = output_directory.resolve()
        if resolved_output.parent != expected_parent or not resolved_output.name.startswith("release-v"):
            raise ReleaseError(f"Refusing to replace unsafe output directory: {resolved_output}")
        shutil.rmtree(resolved_output)
    artifacts = _build_and_verify(tree, version, output_directory)
    commit = _public_commit(tree, version, fresh_root=fresh_root)

    if push:
        remote_main = _remote_ref("refs/heads/main")
        if remote_main is None:
            raise ReleaseError("Remote main is missing; exact force-with-lease cannot be established.")
        push_arguments = [
            "push",
            "--atomic",
            f"--force-with-lease=refs/heads/main:{remote_main}",
            "origin",
            f"{commit}:refs/heads/main",
            f"{commit}:{tag_ref}",
        ]
        run("git", *push_arguments, capture=False)
        previous_public = _optional_ref("refs/heads/public-release")
        if previous_public is None:
            git("update-ref", "refs/heads/public-release", commit)
        else:
            git("update-ref", "refs/heads/public-release", commit, previous_public)
        git("update-ref", tag_ref, commit)
        git("config", "remote.origin.push", "refs/heads/public-release:refs/heads/main")

    return commit, artifacts, bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and publish a sanitized public release.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--fresh-root", action="store_true")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    commit, artifacts, bundle = publish(
        args.version,
        fresh_root=args.fresh_root,
        push=args.push,
    )
    print(f"public release commit: {commit}")
    print(f"private bundle: {bundle}")
    for artifact in artifacts:
        print(f"release asset: {artifact}")


if __name__ == "__main__":
    main()
