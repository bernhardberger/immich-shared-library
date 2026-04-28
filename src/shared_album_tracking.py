"""Shared-album tracking helpers.

These helpers only maintain sidecar justification rows. They do not create,
delete, or mutate Immich assets.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

from src.shared_album_discovery import SharedAlbumEdge


async def find_mirrored_target_asset_id(
    conn: asyncpg.Connection,
    source_asset_id: UUID,
    target_user_id: UUID,
) -> UUID | None:
    """Return an existing lower-level target asset mapping, if present."""
    return await conn.fetchval(
        """
        SELECT target_asset_id
        FROM _face_sync_asset_map
        WHERE source_asset_id = $1
          AND target_user_id = $2
        """,
        source_asset_id,
        target_user_id,
    )


async def mark_album_justification(
    conn: asyncpg.Connection,
    edge: SharedAlbumEdge,
    target_asset_id: UUID,
    *,
    target_album_id: UUID | None = None,
    seen_at: datetime | None = None,
) -> None:
    """Upsert one shared-album justification for a mirrored target asset."""
    await conn.execute(
        """
        INSERT INTO _face_sync_album_map (
            source_album_id,
            source_asset_id,
            source_user_id,
            target_user_id,
            target_asset_id,
            target_album_id,
            last_seen_at
        ) VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7::timestamptz, NOW()))
        ON CONFLICT (source_album_id, source_asset_id, target_user_id)
        DO UPDATE SET
            source_user_id = EXCLUDED.source_user_id,
            target_asset_id = EXCLUDED.target_asset_id,
            target_album_id = EXCLUDED.target_album_id,
            last_seen_at = EXCLUDED.last_seen_at
        """,
        edge.album_id,
        edge.asset_id,
        edge.source_user_id,
        edge.target_user_id,
        target_asset_id,
        target_album_id,
        seen_at,
    )


async def has_shared_album_justification(
    conn: asyncpg.Connection,
    target_asset_id: UUID,
) -> bool:
    """Return whether any shared album still justifies a target asset."""
    return bool(await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM _face_sync_album_map
            WHERE target_asset_id = $1
        )
        """,
        target_asset_id,
    ))


async def remove_stale_album_justifications(
    conn: asyncpg.Connection,
    seen_before: datetime,
    *,
    target_user_id: UUID | None = None,
) -> int:
    """Delete album-map rows not refreshed during the current discovery cycle.

    ``seen_before`` is normally the sync cycle's start timestamp. Rows marked
    at or after that timestamp are current; older rows represent removed album
    assets, removed participants, deleted albums, or newly excluded users.
    """
    if target_user_id is None:
        result = await conn.execute(
            """
            DELETE FROM _face_sync_album_map
            WHERE last_seen_at < $1
            """,
            seen_before,
        )
    else:
        result = await conn.execute(
            """
            DELETE FROM _face_sync_album_map
            WHERE last_seen_at < $1
              AND target_user_id = $2
            """,
            seen_before,
            target_user_id,
        )
    return _rows_affected(result)


def _rows_affected(command_status: str) -> int:
    """Parse asyncpg command status strings such as ``DELETE 3``."""
    parts = command_status.split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0
