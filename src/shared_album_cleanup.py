"""Cleanup helpers for shared-albums shadow-library mirrored assets."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

import asyncpg

from src.config import SharedAlbumsShadowLibraryConfig
from src.file_ops import remove_hardlinks
from src.shared_album_shadow import (
    remove_shadow_path,
    shadow_import_path_relative,
    shadow_library_import_path,
)


logger = logging.getLogger(__name__)


async def cleanup_orphaned_shared_album_assets(
    conn: asyncpg.Connection,
    shadow_config: SharedAlbumsShadowLibraryConfig,
) -> int:
    """Delete sidecar-owned shared-album target assets with no justification.

    Cleanup is intentionally scoped to assets whose original path maps from the
    configured Immich import prefix into the configured shadow filesystem root.
    Any suspicious path or non-symlink original is left untouched so the DB rows
    remain available for retry or manual inspection.
    """
    candidates = await conn.fetch(
        """
        SELECT
            m.source_asset_id,
            m.target_asset_id,
            m.target_user_id,
            a."ownerId",
            a."libraryId",
            a."originalPath",
            l."ownerId" AS library_owner_id,
            COUNT(am.target_asset_id) AS remaining_justifications
        FROM _face_sync_asset_map m
        JOIN asset a ON a.id = m.target_asset_id
        JOIN library l ON l.id = a."libraryId"
        LEFT JOIN _face_sync_album_map am ON am.target_asset_id = m.target_asset_id
        GROUP BY
            m.source_asset_id,
            m.target_asset_id,
            m.target_user_id,
            a."ownerId",
            a."libraryId",
            a."originalPath",
            l."ownerId"
        HAVING COUNT(am.target_asset_id) = 0
        """,
    )

    cleaned = 0
    for row in candidates:
        target_asset_id = row["target_asset_id"]
        try:
            if not _is_verified_shadow_asset_row(row):
                logger.warning("Skipping shared-album cleanup for unverified target asset %s", target_asset_id)
                continue

            target_user_id = UUID(str(row["target_user_id"]))
            target_import_path = shadow_library_import_path(
                shadow_config.import_path_prefix,
                target_user_id,
            )
            relative_path = _relative_path_from_target_user_root(
                target_user_id,
                shadow_import_path_relative(target_import_path, str(row["originalPath"])),
            )

            # Remove the sidecar-owned original symlink first. A non-symlink or
            # escaping path raises and leaves DB rows intact for inspection.
            remove_shadow_path(shadow_config.filesystem_root, relative_path)

            files = await conn.fetch(
                'SELECT path FROM asset_file WHERE "assetId" = $1',
                target_asset_id,
            )
            remove_hardlinks([str(file_row["path"]) for file_row in files])

            await conn.execute(
                'DELETE FROM album_asset WHERE "assetId" = $1',
                target_asset_id,
            )
            await conn.execute("DELETE FROM asset WHERE id = $1", target_asset_id)
            await conn.execute(
                "DELETE FROM _face_sync_asset_map WHERE target_asset_id = $1",
                target_asset_id,
            )

            logger.info(
                "Cleaned up orphaned shared-album target asset: source=%s target=%s",
                row["source_asset_id"],
                target_asset_id,
            )
            cleaned += 1
        except ValueError as exc:
            logger.warning(
                "Skipping shared-album cleanup for target asset %s: %s",
                target_asset_id,
                exc,
            )
        except Exception:
            logger.exception("Failed to clean up shared-album target asset %s", target_asset_id)

    return cleaned


def _is_verified_shadow_asset_row(row) -> bool:
    if int(row["remaining_justifications"] or 0) != 0:
        return False
    target_user_id = UUID(str(row["target_user_id"]))
    return (
        row["libraryId"] is not None
        and UUID(str(row["ownerId"])) == target_user_id
        and UUID(str(row["library_owner_id"])) == target_user_id
    )


def _relative_path_from_target_user_root(
    target_user_id: UUID,
    relative_below_user_root: Path,
) -> Path:
    """Reattach target user root after validating against its import path."""
    return Path(str(target_user_id)) / relative_below_user_root
