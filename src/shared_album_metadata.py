"""Conflict-aware shared metadata reconciliation for shared-albums mode."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import logging
from typing import Any, Mapping
from uuid import UUID

import asyncpg
import httpx

from src.immich_api import ImmichAPI

logger = logging.getLogger(__name__)

FIELD_GROUPS = ("taken_at", "location", "description")
_UNINITIALIZED = object()


async def reconcile_shared_album_metadata(
    conn: asyncpg.Connection,
    api: ImmichAPI,
    *,
    source_asset_ids: set[UUID] | None = None,
) -> dict[str, int]:
    """Reconcile first-slice shared metadata for shared-album logical photos."""
    stats = {
        "metadata_fields_initialized": 0,
        "metadata_fields_propagated": 0,
        "metadata_conflicts": 0,
        "metadata_assets_updated": 0,
    }
    rows = await fetch_shared_album_metadata_rows(conn, source_asset_ids=source_asset_ids)
    if not rows:
        return stats

    states = await fetch_metadata_states(conn)
    updated_asset_ids: set[UUID] = set()
    rows_by_source: dict[UUID, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_source[UUID(str(row["source_asset_id"]))].append(row)

    for source_asset_id, asset_rows in rows_by_source.items():
        for field_group in FIELD_GROUPS:
            current_values = [(row, _field_value(row, field_group)) for row in asset_rows]
            last_value = states.get((source_asset_id, field_group), _UNINITIALIZED)
            decision = _choose_reconciled_value(last_value, [value for _, value in current_values])

            if decision[0] == "noop":
                continue
            if decision[0] == "conflict":
                await record_metadata_state(
                    conn,
                    source_asset_id,
                    field_group,
                    None if last_value is _UNINITIALIZED else last_value,
                    conflict_values=decision[1],
                )
                stats["metadata_conflicts"] += 1
                logger.warning(
                    "Shared metadata conflict for source_asset_id=%s field_group=%s values=%s",
                    source_asset_id,
                    field_group,
                    decision[1],
                )
                continue

            desired_value = decision[1]
            updates = 0
            blocked = False
            for row, current_value in current_values:
                if _value_key(current_value) == _value_key(desired_value):
                    continue
                asset_id = UUID(str(row["asset_id"]))
                applied = await _apply_api_update(conn, api, asset_id, field_group, desired_value)
                if not applied:
                    blocked = True
                    await record_metadata_state(
                        conn,
                        source_asset_id,
                        field_group,
                        None if last_value is _UNINITIALIZED else last_value,
                        conflict_values=[desired_value],
                    )
                    stats["metadata_conflicts"] += 1
                    logger.warning(
                        "Shared metadata value cannot be represented through the Immich API; "
                        "source_asset_id=%s asset_id=%s field_group=%s value=%s",
                        source_asset_id,
                        asset_id,
                        field_group,
                        desired_value,
                    )
                    continue
                updated_asset_ids.add(asset_id)
                updates += 1

            if blocked:
                continue

            await record_metadata_state(conn, source_asset_id, field_group, desired_value)
            if last_value is _UNINITIALIZED:
                stats["metadata_fields_initialized"] += 1
            if updates:
                stats["metadata_fields_propagated"] += 1

    stats["metadata_assets_updated"] = len(updated_asset_ids)

    return stats


async def fetch_shared_album_metadata_rows(
    conn: asyncpg.Connection,
    *,
    source_asset_ids: set[UUID] | None = None,
) -> list[Mapping[str, Any]]:
    """Fetch only shared-album-justified logical photos and their mirrors."""
    return await conn.fetch(
        """
        WITH logical_assets AS (
            SELECT DISTINCT source_asset_id, source_asset_id AS asset_id
            FROM _face_sync_album_map
            UNION
            SELECT DISTINCT source_asset_id, target_asset_id AS asset_id
            FROM _face_sync_album_map
        )
        SELECT
            la.source_asset_id,
            la.asset_id,
            ae."dateTimeOriginal",
            ae."timeZone",
            ae.latitude,
            ae.longitude,
            ae.description
        FROM logical_assets la
        JOIN asset a ON a.id = la.asset_id
        LEFT JOIN asset_exif ae ON ae."assetId" = la.asset_id
        WHERE a."deletedAt" IS NULL
          AND COALESCE(a."isOffline", FALSE) = FALSE
          AND (a.status IS NULL OR a.status = 'active')
          AND ($1::uuid[] IS NULL OR la.source_asset_id = ANY($1::uuid[]))
        ORDER BY la.source_asset_id, la.asset_id
        """,
        list(source_asset_ids) if source_asset_ids is not None else None,
    )


async def fetch_metadata_states(conn: asyncpg.Connection) -> dict[tuple[UUID, str], Any]:
    rows = await conn.fetch(
        """
        SELECT source_asset_id, field_group, synced_value
        FROM _face_sync_metadata_state
        """
    )
    return {
        (UUID(str(row["source_asset_id"])), str(row["field_group"])): _decode_jsonb(row["synced_value"])
        for row in rows
    }


async def record_metadata_state(
    conn: asyncpg.Connection,
    source_asset_id: UUID,
    field_group: str,
    synced_value: Any,
    *,
    conflict_values: list[Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO _face_sync_metadata_state (
            source_asset_id,
            field_group,
            synced_value,
            conflict_values,
            updated_at
        ) VALUES ($1, $2, $3::jsonb, $4::jsonb, NOW())
        ON CONFLICT (source_asset_id, field_group)
        DO UPDATE SET
            synced_value = EXCLUDED.synced_value,
            conflict_values = EXCLUDED.conflict_values,
            updated_at = EXCLUDED.updated_at
        """,
        source_asset_id,
        field_group,
        json.dumps(synced_value, sort_keys=True),
        json.dumps(conflict_values, sort_keys=True) if conflict_values is not None else None,
    )


def _choose_reconciled_value(last_value: Any, values: list[Any]) -> tuple[str, Any | None]:
    distinct = {_value_key(value): value for value in values}
    if last_value is _UNINITIALIZED:
        non_empty = {_value_key(value): value for value in values if not _is_empty_value(value)}
        if len(non_empty) > 1:
            return "conflict", list(non_empty.values())
        if len(non_empty) == 1:
            return "sync", next(iter(non_empty.values()))
        return "sync", next(iter(distinct.values()))

    changed = {
        _value_key(value): value
        for value in values
        if _value_key(value) != _value_key(last_value)
    }
    if not changed:
        return "noop", None
    if len(changed) == 1:
        return "sync", next(iter(changed.values()))
    return "conflict", list(changed.values())


def _field_value(row: Mapping[str, Any], field_group: str) -> Any:
    if field_group == "taken_at":
        return {
            "dateTimeOriginal": _datetime_value(row.get("dateTimeOriginal")),
            "timeZone": _blank_to_none(row.get("timeZone")),
        }
    if field_group == "location":
        return {
            "latitude": _number_or_none(row.get("latitude")),
            "longitude": _number_or_none(row.get("longitude")),
        }
    if field_group == "description":
        return _blank_to_none(row.get("description"))
    raise ValueError(f"unknown metadata field group: {field_group}")


async def _apply_api_update(
    conn: asyncpg.Connection,
    api: ImmichAPI,
    asset_id: UUID,
    field_group: str,
    value: Any,
) -> bool:
    """Apply a reconciled value using only Immich API shapes accepted by v2.7.5."""
    try:
        if field_group == "taken_at":
            if value.get("dateTimeOriginal") is None:
                return False
            # Immich v2.7.5's bulk endpoint persisted metadata reliably in live
            # testing. timeZone cannot be sent alongside dateTimeOriginal.
            await api.update_assets_metadata([asset_id], dateTimeOriginal=value.get("dateTimeOriginal"))
            if value.get("timeZone") is not None:
                await api.update_assets_metadata([asset_id], timeZone=value.get("timeZone"))
            return True
        if field_group == "location":
            # The public DTO validates latitude/longitude as non-empty when either
            # GPS field is present, so location clears are surfaced as conflicts.
            if value.get("latitude") is None or value.get("longitude") is None:
                return False
            await api.update_assets_metadata([asset_id], latitude=value.get("latitude"), longitude=value.get("longitude"))
            return True
        if field_group == "description":
            await api.update_assets_metadata([asset_id], description="" if value is None else value)
            return True
    except httpx.HTTPStatusError as exc:
        if not _is_asset_update_access_denied(exc):
            raise
        await _apply_db_metadata_update(conn, asset_id, field_group, value)
        return True
    raise ValueError(f"unknown metadata field group: {field_group}")


async def _apply_db_metadata_update(
    conn: asyncpg.Connection,
    asset_id: UUID,
    field_group: str,
    value: Any,
) -> None:
    """Fallback for mirror rows the Immich API key cannot update."""
    await conn.execute("SET LOCAL immich_shared_sidecar.suppress_events = 'on'")
    if field_group == "taken_at":
        taken_at = _datetime_db_value(value.get("dateTimeOriginal"))
        await conn.execute(
            """
            UPDATE asset_exif
            SET "dateTimeOriginal" = $2,
                "timeZone" = $3,
                "updatedAt" = NOW()
            WHERE "assetId" = $1
            """,
            asset_id,
            taken_at,
            value.get("timeZone"),
        )
        await conn.execute(
            """
            UPDATE asset
            SET "fileCreatedAt" = $2,
                "localDateTime" = $2
            WHERE id = $1
            """,
            asset_id,
            taken_at,
        )
        return
    if field_group == "location":
        await conn.execute(
            """
            UPDATE asset_exif
            SET latitude = $2,
                longitude = $3,
                "updatedAt" = NOW()
            WHERE "assetId" = $1
            """,
            asset_id,
            value.get("latitude"),
            value.get("longitude"),
        )
        return
    if field_group == "description":
        await conn.execute(
            """
            UPDATE asset_exif
            SET description = $2,
                "updatedAt" = NOW()
            WHERE "assetId" = $1
            """,
            asset_id,
            "" if value is None else value,
        )
        return
    raise ValueError(f"unknown metadata field group: {field_group}")


def _is_asset_update_access_denied(exc: httpx.HTTPStatusError) -> bool:
    response = exc.response
    if response.status_code != 400:
        return False
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, Mapping):
        message = body.get("message")
        if isinstance(message, list):
            return any("Not found or no asset.update access" in str(item) for item in message)
        return "Not found or no asset.update access" in str(message)
    return "Not found or no asset.update access" in str(body)


def _is_empty_value(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_blank_to_none(item) is None for item in value.values())
    return _blank_to_none(value) is None


def _value_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode_jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _datetime_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _datetime_db_value(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _blank_to_none(value: Any) -> Any | None:
    if value == "":
        return None
    return value
