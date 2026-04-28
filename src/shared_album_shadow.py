"""Safe path and symlink helpers for shared-album shadow libraries.

The helpers in this module only manage sidecar-owned paths below the configured
shadow root. They never modify source originals, camera uploads, or Immich DB
rows.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path, PurePosixPath
from uuid import UUID


def shadow_library_name(name_prefix: str, target_user_id: UUID | str) -> str:
    return f"{name_prefix} - {target_user_id}"


def shadow_library_import_path(import_path_prefix: str, target_user_id: UUID | str) -> str:
    prefix = posixpath.normpath(import_path_prefix)
    return posixpath.join(prefix, str(target_user_id))


def shadow_asset_relative_path(
    *,
    target_user_id: UUID | str,
    source_user_id: UUID | str,
    source_asset_id: UUID | str,
    original_filename: str,
) -> Path:
    filename = _safe_plain_filename(original_filename)
    return Path(str(target_user_id)) / str(source_user_id) / str(source_asset_id) / filename


def assert_path_within_root(root: Path | str, candidate: Path | str) -> Path:
    root_path = Path(root).resolve(strict=False)
    candidate_path = Path(candidate).resolve(strict=False)
    if candidate_path != root_path and root_path not in candidate_path.parents:
        raise ValueError(f"path escapes shadow root: {candidate}")
    return candidate_path


def create_shadow_symlink(
    root: Path | str,
    relative_path: Path | str,
    source_path: Path | str,
) -> Path:
    root_path = Path(root)
    if root_path.is_symlink():
        raise ValueError(f"shadow root must not be a symlink: {root_path}")
    safe_relative_path = _safe_relative_path(relative_path)
    link_path = root_path / safe_relative_path
    root_path.mkdir(parents=True, exist_ok=True)
    assert_path_within_root(root_path, link_path)
    _assert_no_existing_symlink_ancestor(root_path, safe_relative_path.parent)

    link_path.parent.mkdir(parents=True, exist_ok=True)
    assert_path_within_root(root_path, link_path.parent)

    source = Path(source_path)
    if link_path.exists() or link_path.is_symlink():
        if not link_path.is_symlink() or Path(os.readlink(link_path)) != source:
            raise FileExistsError(f"shadow path already exists: {link_path}")
        return link_path

    link_path.symlink_to(source)
    return link_path


def remove_shadow_path(root: Path | str, relative_path: Path | str) -> bool:
    root_path = Path(root)
    safe_relative_path = _safe_relative_path(relative_path)
    link_path = root_path / safe_relative_path
    assert_path_within_root(root_path, link_path.parent)
    if not link_path.is_symlink():
        if link_path.exists():
            raise ValueError(f"shadow path is not a symlink: {link_path}")
        return False

    link_path.unlink()
    _remove_empty_parents(link_path.parent, root_path)
    return True


def _safe_plain_filename(original_filename: str) -> str:
    filename = str(original_filename)
    path = PurePosixPath(filename)
    if not filename or filename in {".", ".."} or path.name != filename:
        raise ValueError("original_filename must be a plain file name")
    return filename


def _safe_relative_path(relative_path: Path | str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"shadow path must be relative and stay within the shadow root: {relative_path}")
    return path


def _remove_empty_parents(start: Path, root: Path) -> None:
    root_resolved = root.resolve(strict=False)
    current = start
    while current.resolve(strict=False) != root_resolved:
        assert_path_within_root(root, current)
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _assert_no_existing_symlink_ancestor(root: Path, relative_parent: Path) -> None:
    current = root
    for part in relative_parent.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            return
        if current.is_symlink():
            raise ValueError(f"path escapes shadow root: {current}")
        assert_path_within_root(root, current)
