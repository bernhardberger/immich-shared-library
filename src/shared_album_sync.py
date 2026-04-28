"""Runtime integration for shared-albums sync mode."""

from __future__ import annotations

import logging
import posixpath
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID

import asyncpg

from src.asset_sync import find_duplicate_filenames, record_skipped_duplicates, sync_asset
from src.config import SharedAlbumsShadowLibraryConfig, SyncJob, settings
from src.db import transaction
from src.immich_api import ImmichAPI
from src.ml_sync import sync_faces_for_asset, sync_faces_incremental
from src.person_sync import sync_person_names, sync_person_thumbnails, sync_person_visibility
from src.schema import validate_schema
from src.shared_album_cleanup import cleanup_orphaned_shared_album_assets
from src.shared_album_discovery import SharedAlbumEdge, discover_shared_album_edges
from src.shared_album_shadow import (
    create_shadow_symlink,
    shadow_asset_relative_path,
    shadow_library_import_path,
    shadow_library_name,
)
from src.shared_album_tracking import (
    find_mirrored_target_asset_id,
    mark_album_justification,
    remove_stale_album_justifications,
)

logger = logging.getLogger(__name__)

SyncAssetFn = Callable[[asyncpg.Connection, Mapping[str, Any], SyncJob], Awaitable[UUID | None]]
SyncFacesFn = Callable[[asyncpg.Connection, UUID, UUID, UUID, UUID], Awaitable[int]]
FindDuplicateFilenamesFn = Callable[[asyncpg.Connection, list[Mapping[str, Any]], SyncJob], Awaitable[set[UUID]]]
RecordSkippedDuplicatesFn = Callable[[asyncpg.Connection, set[UUID], UUID], Awaitable[None]]


def _empty_shared_album_stats() -> dict[str, int]:
    return {
        "assets_synced": 0,
        "assets_skipped_duplicate": 0,
        "faces_synced": 0,
        "persons_updated": 0,
        "assets_cleaned": 0,
        "faces_reassigned": 0,
        "persons_cleaned": 0,
        "album_assets_added": 0,
        "shared_album_edges": 0,
        "album_justifications_marked": 0,
        "album_justifications_removed": 0,
        "shared_album_assets_cleaned": 0,
    }


async def run_shared_albums_sync(api: ImmichAPI | None = None) -> dict[str, int]:
    """Run one shared-albums discovery and mirror cycle."""
    config = settings.sync_config.shared_albums
    if config is None:
        raise RuntimeError("sync_mode=shared_albums requires shared_albums config")

    owns_api = api is None
    api = api or ImmichAPI()
    cycle_started_at = datetime.now(timezone.utc)
    stats = _empty_shared_album_stats()

    try:
        async with transaction() as conn:
            await validate_schema(conn)
            edges = await discover_shared_album_edges_from_db(conn)
            source_assets = await fetch_eligible_shared_album_source_assets(
                conn,
                [edge.asset_id for edge in edges],
            )
            stats["shared_album_edges"] = len(edges)
            edge_stats = await process_shared_album_edges(
                conn,
                api,
                config.shadow_library,
                edges,
                source_assets,
                cycle_started_at=cycle_started_at,
            )
            for key, value in edge_stats.items():
                stats[key] += value
            stats["album_justifications_removed"] = await remove_stale_album_justifications(
                conn,
                cycle_started_at,
            )
            stats["shared_album_assets_cleaned"] = await cleanup_orphaned_shared_album_assets(
                conn,
                config.shadow_library,
            )

        async with transaction() as conn:
            stats["faces_synced"] += await sync_faces_incremental(conn)

        async with transaction() as conn:
            stats["persons_updated"] += await sync_person_names(conn)
            stats["persons_updated"] += await sync_person_visibility(conn)
            stats["persons_updated"] += await sync_person_thumbnails(conn)

        if any(v > 0 for v in stats.values()):
            logger.info("Shared-albums sync complete: %s", stats)
        else:
            logger.debug("Shared-albums sync complete: nothing to do")
        return stats
    finally:
        if owns_api:
            await api.close()


async def discover_shared_album_edges_from_db(conn: asyncpg.Connection) -> list[SharedAlbumEdge]:
    """Fetch Immich shared-album rows and return candidate mirror edges."""
    config = settings.sync_config.shared_albums
    if config is None:
        raise RuntimeError("shared_albums config is required")

    album_name_column = await _optional_column_name(conn, "album", ["albumName", "name"])
    album_name_select = f', "{album_name_column}"' if album_name_column else ""
    albums = await conn.fetch(
        f"""
        SELECT id, "ownerId", "deletedAt"{album_name_select}
        FROM album
        WHERE "deletedAt" IS NULL
        """
    )
    album_users = await conn.fetch(
        """
        SELECT "albumId", "userId", role
        FROM album_user
        """
    )
    album_assets = await conn.fetch(
        """
        SELECT "albumId", "assetId"
        FROM album_asset
        """
    )
    assets = await conn.fetch(
        """
        SELECT DISTINCT a.id, a."ownerId", a."deletedAt"
        FROM asset a
        JOIN album_asset aa ON aa."assetId" = a.id
        WHERE a."deletedAt" IS NULL
          AND COALESCE(a."isOffline", FALSE) = FALSE
          AND (a.status IS NULL OR a.status = 'active')
        """
    )
    user_has_status = await _optional_column_name(conn, "user", ["status"])
    users = await conn.fetch(
        """
        SELECT id, "deletedAt", status
        FROM "user"
        WHERE "deletedAt" IS NULL
          AND (status IS NULL OR status = 'active')
        """
        if user_has_status
        else
        """
        SELECT id, "deletedAt"
        FROM "user"
        WHERE "deletedAt" IS NULL
        """
    )
    return discover_shared_album_edges(
        albums=_records_as_dicts(albums),
        album_users=_records_as_dicts(album_users),
        album_assets=_records_as_dicts(album_assets),
        assets=_records_as_dicts(assets),
        users=_records_as_dicts(users),
        exclude_users=config.scope.exclude_users,
        exclude_name_patterns=config.albums.exclude_name_patterns,
    )


def _records_as_dicts(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


async def _optional_column_name(
    conn: asyncpg.Connection,
    table_name: str,
    candidates: list[str],
) -> str | None:
    rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = $1
          AND column_name = ANY($2::text[])
        """,
        table_name,
        candidates,
    )
    available = {row["column_name"] for row in rows}
    for candidate in candidates:
        if candidate in available:
            return candidate
    return None


async def fetch_eligible_shared_album_source_assets(
    conn: asyncpg.Connection,
    asset_ids: list[UUID],
) -> dict[UUID, Mapping[str, Any]]:
    """Fetch fully processed source assets for shared-albums candidate edges."""
    if not asset_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT a.*
        FROM asset a
        JOIN asset_job_status ajs ON ajs."assetId" = a.id
        JOIN smart_search ss ON ss."assetId" = a.id
        WHERE a.id = ANY($1::uuid[])
          AND a."deletedAt" IS NULL
          AND COALESCE(a."isOffline", FALSE) = FALSE
          AND (a.status IS NULL OR a.status = 'active')
          AND ajs."metadataExtractedAt" IS NOT NULL
          AND ajs."facesRecognizedAt" IS NOT NULL
        """,
        list(set(asset_ids)),
    )
    return {row["id"]: row for row in rows}


async def process_shared_album_edges(
    conn: asyncpg.Connection,
    api: ImmichAPI,
    shadow_config: SharedAlbumsShadowLibraryConfig,
    edges: list[SharedAlbumEdge],
    source_assets: Mapping[UUID, Mapping[str, Any]],
    *,
    cycle_started_at: datetime,
    sync_asset_fn: SyncAssetFn = sync_asset,
    sync_faces_for_asset_fn: SyncFacesFn = sync_faces_for_asset,
    find_duplicate_filenames_fn: FindDuplicateFilenamesFn = find_duplicate_filenames,
    record_skipped_duplicates_fn: RecordSkippedDuplicatesFn = record_skipped_duplicates,
) -> dict[str, int]:
    """Mirror current shared-album edges and mark justification rows."""
    stats = {
        "assets_synced": 0,
        "assets_skipped_duplicate": 0,
        "faces_synced": 0,
        "album_justifications_marked": 0,
    }
    library_cache: dict[UUID, Mapping[str, Any]] = {}
    pending_edges_by_target: dict[UUID, list[SharedAlbumEdge]] = {}

    for edge in edges:
        source = source_assets.get(edge.asset_id)
        if source is None:
            continue

        target_asset_id = await find_mirrored_target_asset_id(
            conn,
            edge.asset_id,
            edge.target_user_id,
        )
        if target_asset_id is not None:
            await mark_album_justification(
                conn,
                edge,
                target_asset_id,
                seen_at=cycle_started_at,
            )
            stats["album_justifications_marked"] += 1
            continue

        pending_edges_by_target.setdefault(edge.target_user_id, []).append(edge)

    for target_user_id, pending_edges in pending_edges_by_target.items():
        library = library_cache.get(target_user_id)
        if library is None:
            library = await ensure_shadow_library(api, target_user_id, shadow_config)
            library_cache[target_user_id] = library

        library_id = UUID(str(library["id"]))
        duplicate_check_job = SyncJob(
            name=f"shared-albums:{target_user_id}:duplicate-check",
            source_user_id=pending_edges[0].source_user_id,
            target_user_id=target_user_id,
            target_library_id=library_id,
            source_path_prefix="",
            target_path_prefix="",
            album_id=None,
        )
        candidate_sources = [source_assets[edge.asset_id] for edge in pending_edges]
        duplicate_source_ids = await find_duplicate_filenames_fn(
            conn,
            candidate_sources,
            duplicate_check_job,
        )
        if duplicate_source_ids:
            await record_skipped_duplicates_fn(conn, duplicate_source_ids, target_user_id)

        for edge in pending_edges:
            if edge.asset_id in duplicate_source_ids:
                stats["assets_skipped_duplicate"] += 1
                continue

            source = source_assets[edge.asset_id]

            relative_path = shadow_asset_relative_path(
                target_user_id=edge.target_user_id,
                source_user_id=edge.source_user_id,
                source_asset_id=edge.asset_id,
                original_filename=str(source["originalFileName"]),
            )
            create_shadow_symlink(
                shadow_config.filesystem_root,
                relative_path,
                str(source["originalPath"]),
            )
            target_path = posixpath.join(
                shadow_library_import_path(shadow_config.import_path_prefix, edge.target_user_id),
                *relative_path.parts[1:],
            )
            job = SyncJob(
                name=f"shared-albums:{edge.target_user_id}",
                source_user_id=edge.source_user_id,
                target_user_id=edge.target_user_id,
                target_library_id=library_id,
                source_path_prefix=str(source["originalPath"]),
                target_path_prefix=target_path,
                album_id=None,
            )
            target_asset_id = await sync_asset_fn(conn, source, job)
            if target_asset_id is None:
                continue

            stats["assets_synced"] += 1
            stats["faces_synced"] += await sync_faces_for_asset_fn(
                conn,
                edge.asset_id,
                target_asset_id,
                edge.source_user_id,
                edge.target_user_id,
            )
            await mark_album_justification(
                conn,
                edge,
                target_asset_id,
                seen_at=cycle_started_at,
            )
            stats["album_justifications_marked"] += 1

    return stats


async def ensure_shadow_library(
    api: ImmichAPI,
    target_user_id: UUID,
    config: SharedAlbumsShadowLibraryConfig,
) -> Mapping[str, Any]:
    """Find/create/update the deterministic shadow library for one target user."""
    expected_name = shadow_library_name(config.name_prefix, target_user_id)
    expected_import_path = shadow_library_import_path(config.import_path_prefix, target_user_id)

    libraries = await api.list_libraries()
    matches = [library for library in libraries if library.get("name") == expected_name]
    if not matches:
        if not config.auto_create:
            raise RuntimeError(f"shadow library {expected_name!r} not found for target user {target_user_id}")
        library = await api.create_library(
            owner_id=target_user_id,
            name=expected_name,
            import_paths=[expected_import_path],
            exclusion_patterns=[],
        )
        await _scan_shadow_library_if_enabled(api, library, config)
        return library

    library = matches[0]
    owner_id = UUID(str(library.get("ownerId") or library.get("owner", {}).get("id")))
    if owner_id != target_user_id:
        raise RuntimeError(
            f"shadow library {expected_name!r} belongs to user {owner_id}, not {target_user_id}"
        )

    import_paths = list(library.get("importPaths") or [])
    exclusion_patterns = list(library.get("exclusionPatterns") or [])
    if import_paths != [expected_import_path] or exclusion_patterns:
        if not config.auto_create:
            raise RuntimeError(
                f"shadow library {expected_name!r} has unexpected import paths or exclusions"
            )
        library = await api.update_library(
            library_id=UUID(str(library["id"])),
            name=expected_name,
            import_paths=[expected_import_path],
            exclusion_patterns=[],
        )
        await _scan_shadow_library_if_enabled(api, library, config)
        return library

    return library


async def _scan_shadow_library_if_enabled(
    api: ImmichAPI,
    library: Mapping[str, Any],
    config: SharedAlbumsShadowLibraryConfig,
) -> None:
    if not config.auto_scan:
        return
    library_id = UUID(str(library["id"]))
    await api.validate_library(library_id)
    await api.scan_library(library_id)
