#!/usr/bin/env python3
"""Safely reconcile the public cloud mirror from an explicit allowlist.

The canonical private repository is the only editable source. This tool copies
only the files listed in ``deploy/cloud_manifest.txt`` to a separately checked
out target repository. It never follows target-directory symlinks outside that
repository and it only deletes paths recorded in its generated state file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
from typing import Iterable, Sequence


SCHEMA_VERSION = 1
DEFAULT_MANIFEST = Path("deploy/cloud_manifest.txt")
STATE_RELATIVE_PATH = PurePosixPath("deploy/.cloud-mirror-state.json")
MODES = ("dry-run", "check", "apply")

_DENIED_PARTS = {
    ".aws",
    ".codex",
    ".git",
    ".ssh",
    "secrets",
    "secrets.toml",
}
_DENIED_SUFFIXES = (".key", ".p12", ".pem", ".pfx")


class SyncError(RuntimeError):
    """Raised when a reconciliation would violate a safety invariant."""


@dataclass(frozen=True)
class SyncPlan:
    """A deterministic plan for one reconciliation run."""

    copy_paths: tuple[str, ...]
    delete_paths: tuple[str, ...]
    unchanged_paths: tuple[str, ...]
    state_needs_update: bool

    @property
    def has_drift(self) -> bool:
        return bool(self.copy_paths or self.delete_paths or self.state_needs_update)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_relative_path(raw_path: str) -> PurePosixPath:
    if not isinstance(raw_path, str):
        raise SyncError("Manifest and state paths must be strings.")
    value = raw_path.strip()
    if not value or "\\" in value:
        raise SyncError(f"Unsafe managed path: {raw_path!r}")

    relative = PurePosixPath(value)
    if relative.is_absolute() or value != relative.as_posix():
        raise SyncError(f"Managed path must be normalized and relative: {value!r}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise SyncError(f"Managed path cannot contain dot traversal: {value!r}")

    lower_parts = tuple(part.lower() for part in relative.parts)
    filename = lower_parts[-1]
    if any(part in _DENIED_PARTS for part in lower_parts):
        raise SyncError(f"Sensitive path is forbidden: {value!r}")
    if filename == ".env" or filename.startswith(".env."):
        raise SyncError(f"Environment files are forbidden: {value!r}")
    if filename.endswith(_DENIED_SUFFIXES):
        raise SyncError(f"Credential-like files are forbidden: {value!r}")
    if lower_parts[:2] == (".streamlit", "secrets.toml"):
        raise SyncError("Streamlit secrets must never be mirrored.")
    return relative


def load_manifest(source_root: Path, manifest_path: Path) -> tuple[str, ...]:
    """Load and validate the exact file allowlist."""

    source_root = source_root.resolve()
    manifest_path = manifest_path.resolve()
    if not _is_relative_to(manifest_path, source_root):
        raise SyncError("Manifest must live inside the canonical repository.")
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise SyncError(f"Manifest is missing or unsafe: {manifest_path}")

    managed: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        manifest_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            relative = _validate_relative_path(stripped)
        except SyncError as exc:
            raise SyncError(f"Manifest line {line_number}: {exc}") from exc
        normalized = relative.as_posix()
        if normalized in seen:
            raise SyncError(f"Manifest line {line_number}: duplicate path {normalized!r}")

        source_file = source_root.joinpath(*relative.parts)
        source_resolved = source_file.resolve(strict=False)
        if not _is_relative_to(source_resolved, source_root):
            raise SyncError(f"Source path escapes the canonical repository: {normalized!r}")
        if not source_file.is_file() or source_file.is_symlink():
            raise SyncError(f"Manifest path is missing or not a regular file: {normalized!r}")
        seen.add(normalized)
        managed.append(normalized)

    if not managed:
        raise SyncError("The cloud manifest is empty; refusing to reconcile.")
    return tuple(sorted(managed))


def _validate_roots(source_root: Path, target_root: Path) -> tuple[Path, Path]:
    source_root = source_root.resolve()
    target_root = target_root.resolve()
    if not source_root.is_dir():
        raise SyncError(f"Canonical repository does not exist: {source_root}")
    if not target_root.is_dir():
        raise SyncError(f"Target repository does not exist: {target_root}")
    if not (target_root / ".git").exists():
        raise SyncError("Target must be an explicitly checked out Git repository.")
    if (
        source_root == target_root
        or _is_relative_to(target_root, source_root)
        or _is_relative_to(source_root, target_root)
    ):
        raise SyncError("Canonical and target repositories must be separate trees.")
    return source_root, target_root


def _safe_target_path(target_root: Path, relative_path: str | PurePosixPath) -> Path:
    relative = (
        relative_path
        if isinstance(relative_path, PurePosixPath)
        else _validate_relative_path(relative_path)
    )
    candidate = target_root.joinpath(*relative.parts)

    # Resolve the parent to catch any directory symlink that would redirect a
    # write or deletion outside the explicitly supplied target repository.
    parent_resolved = candidate.parent.resolve(strict=False)
    if not _is_relative_to(parent_resolved, target_root):
        raise SyncError(f"Target path escapes through a symlink: {relative.as_posix()!r}")
    if candidate.exists() and not candidate.is_symlink():
        resolved = candidate.resolve()
        if not _is_relative_to(resolved, target_root):
            raise SyncError(f"Target path escapes the target repository: {relative.as_posix()!r}")
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_path(target_root: Path) -> Path:
    return _safe_target_path(target_root, STATE_RELATIVE_PATH)


def _load_previous_state(target_root: Path) -> dict[str, object] | None:
    path = _state_path(target_root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SyncError(f"Mirror state is not a safe regular file: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"Mirror state is unreadable; refusing deletions: {path}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise SyncError("Mirror state has an unsupported schema; refusing deletions.")
    expected_keys = {"schema_version", "managed_paths", "manifest_sha256", "files"}
    if set(state) != expected_keys:
        raise SyncError("Mirror state has an invalid shape; refusing deletions.")
    managed_paths = state.get("managed_paths")
    if not isinstance(managed_paths, list):
        raise SyncError("Mirror state managed_paths must be a list.")

    normalized: list[str] = []
    for raw_path in managed_paths:
        normalized.append(_validate_relative_path(raw_path).as_posix())
    if len(normalized) != len(set(normalized)):
        raise SyncError("Mirror state contains duplicate managed paths.")

    normalized = sorted(normalized)
    files = state.get("files")
    if not isinstance(files, dict) or set(files) != set(normalized):
        raise SyncError("Mirror state file hashes do not match managed paths.")
    if any(
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for digest in files.values()
    ):
        raise SyncError("Mirror state contains an invalid file hash.")
    expected_manifest_hash = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
    if state.get("manifest_sha256") != expected_manifest_hash:
        raise SyncError("Mirror state manifest hash is invalid; refusing deletions.")
    return {
        "schema_version": SCHEMA_VERSION,
        "managed_paths": normalized,
        "manifest_sha256": expected_manifest_hash,
        "files": {relative: files[relative] for relative in normalized},
    }


def _desired_state(source_root: Path, managed_paths: Sequence[str]) -> dict[str, object]:
    files = {
        relative: _sha256(source_root.joinpath(*PurePosixPath(relative).parts))
        for relative in managed_paths
    }
    manifest_payload = "\n".join(managed_paths).encode("utf-8")
    return {
        "schema_version": SCHEMA_VERSION,
        "managed_paths": list(managed_paths),
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "files": files,
    }


def build_plan(
    source_root: Path,
    target_root: Path,
    managed_paths: Sequence[str],
    previous_state: dict[str, object] | None,
) -> tuple[SyncPlan, dict[str, object]]:
    """Compare source, target, and prior ownership state without mutating files."""

    desired_state = _desired_state(source_root, managed_paths)
    copy_paths: list[str] = []
    unchanged_paths: list[str] = []
    for relative in managed_paths:
        source = source_root.joinpath(*PurePosixPath(relative).parts)
        target = _safe_target_path(target_root, relative)
        if target.is_symlink() or not target.is_file():
            copy_paths.append(relative)
        elif _sha256(source) != _sha256(target):
            copy_paths.append(relative)
        else:
            unchanged_paths.append(relative)

    previous_managed = set(previous_state.get("managed_paths", [])) if previous_state else set()
    previous_hashes = previous_state.get("files", {}) if previous_state else {}
    stale_managed = previous_managed.difference(managed_paths)
    delete_paths: list[str] = []
    for relative in sorted(stale_managed):
        target = _safe_target_path(target_root, relative)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                raise SyncError(f"Managed file path became a directory: {relative!r}")
            if target.is_symlink() or not target.is_file():
                raise SyncError(
                    f"Stale managed target changed type; refusing deletion: {relative!r}"
                )
            if _sha256(target) != previous_hashes[relative]:
                raise SyncError(
                    f"Stale managed target changed since the last sync; "
                    f"refusing deletion: {relative!r}"
                )
            delete_paths.append(relative)

    state_needs_update = previous_state != desired_state
    return (
        SyncPlan(
            copy_paths=tuple(sorted(copy_paths)),
            delete_paths=tuple(delete_paths),
            unchanged_paths=tuple(sorted(unchanged_paths)),
            state_needs_update=state_needs_update,
        ),
        desired_state,
    )


def _atomic_copy(source: Path, destination: Path, target_root: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise SyncError(f"Source changed after planning; refusing copy: {source.name!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not _is_relative_to(destination.parent.resolve(), target_root):
        raise SyncError(f"Destination parent escaped target repository: {destination}")
    if destination.exists() and destination.is_dir() and not destination.is_symlink():
        raise SyncError(f"Cannot replace directory with managed file: {destination}")

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_handle:
            temp_path = Path(temp_handle.name)
        shutil.copy2(source, temp_path, follow_symlinks=False)
        os.replace(temp_path, destination)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, object], target_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _is_relative_to(path.parent.resolve(), target_root):
        raise SyncError(f"State parent escaped target repository: {path.parent}")
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_handle:
            temp_handle.write(encoded)
            temp_path = Path(temp_handle.name)
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def apply_plan(
    source_root: Path,
    target_root: Path,
    plan: SyncPlan,
    desired_state: dict[str, object],
) -> None:
    """Apply an already validated plan inside the explicit target only."""

    for relative in plan.copy_paths:
        source = source_root.joinpath(*PurePosixPath(relative).parts)
        destination = _safe_target_path(target_root, relative)
        _atomic_copy(source, destination, target_root)

    for relative in plan.delete_paths:
        destination = _safe_target_path(target_root, relative)
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                raise SyncError(f"Refusing to delete directory for managed file: {relative!r}")
            destination.unlink()

    _atomic_write_json(_state_path(target_root), desired_state, target_root)


def run_sync(
    *,
    source_root: Path,
    target_root: Path,
    manifest_path: Path,
    mode: str,
) -> SyncPlan:
    """Plan, check, or apply one canonical-to-cloud reconciliation."""

    if mode not in MODES:
        raise SyncError(f"Unknown mode {mode!r}; choose one of {', '.join(MODES)}.")
    source_root, target_root = _validate_roots(source_root, target_root)
    managed_paths = load_manifest(source_root, manifest_path)
    previous_state = _load_previous_state(target_root)
    plan, desired_state = build_plan(
        source_root, target_root, managed_paths, previous_state
    )
    if mode == "apply":
        apply_plan(source_root, target_root, plan, desired_state)
    return plan


def _format_plan(plan: SyncPlan, mode: str) -> Iterable[str]:
    yield (
        f"mode={mode} copy={len(plan.copy_paths)} "
        f"delete={len(plan.delete_paths)} unchanged={len(plan.unchanged_paths)} "
        f"state_update={'yes' if plan.state_needs_update else 'no'}"
    )
    for relative in plan.copy_paths:
        yield f"COPY {relative}"
    for relative in plan.delete_paths:
        yield f"DELETE_MANAGED {relative}"
    if plan.state_needs_update:
        yield f"STATE {STATE_RELATIVE_PATH.as_posix()}"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        required=True,
        help="Absolute path to the separately checked out public cloud repository.",
    )
    parser.add_argument("--mode", choices=MODES, default="dry-run")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    target_argument = Path(args.target).expanduser()
    if not target_argument.is_absolute():
        print("ERROR: --target must be an absolute path.", file=sys.stderr)
        return 2

    source_root = Path(__file__).resolve().parents[1]
    manifest_path = source_root / DEFAULT_MANIFEST
    try:
        plan = run_sync(
            source_root=source_root,
            target_root=target_argument,
            manifest_path=manifest_path,
            mode=args.mode,
        )
    except SyncError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for line in _format_plan(plan, args.mode):
        print(line)
    if args.mode == "check" and plan.has_drift:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
