"""Event-driven shared-album metadata sync helpers.

This module is intentionally dormant by default. Callers must explicitly create
the queue/trigger schema and invoke the batch processor; the normal one-shot
sidecar path does not install triggers or start a daemon.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

EVENT_CHANNEL = "face_sync_events"
METADATA_EVENT_TYPE = "asset_exif_changed"
EVENT_DAEMON_ADVISORY_LOCK = 0xFACC0065

EVENT_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS _face_sync_event_queue (
    id bigserial PRIMARY KEY,
    event_type text NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid NOT NULL,
    source_asset_id uuid NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    processed_at timestamptz NULL,
    error text NULL,
    CONSTRAINT _face_sync_event_queue_status_check
        CHECK (status IN ('pending', 'processing', 'done', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_face_sync_event_queue_pending
    ON _face_sync_event_queue (status, created_at, id)
    WHERE status = 'pending';

CREATE UNIQUE INDEX IF NOT EXISTS idx_face_sync_event_queue_coalesce
    ON _face_sync_event_queue (
        event_type,
        entity_type,
        entity_id,
        (COALESCE(source_asset_id, '00000000-0000-0000-0000-000000000000'::uuid))
    )
    WHERE status = 'pending';
"""

METADATA_TRIGGER_DDL = """
CREATE OR REPLACE FUNCTION immich_shared_sidecar_enqueue_asset_exif_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    changed_columns jsonb := '{}'::jsonb;
    queue_id bigint;
BEGIN
    IF current_setting('immich_shared_sidecar.suppress_events', true) = 'on' THEN
        RETURN NEW;
    END IF;

    IF OLD.description IS NOT DISTINCT FROM NEW.description
       AND OLD."dateTimeOriginal" IS NOT DISTINCT FROM NEW."dateTimeOriginal"
       AND OLD."timeZone" IS NOT DISTINCT FROM NEW."timeZone"
       AND OLD.latitude IS NOT DISTINCT FROM NEW.latitude
       AND OLD.longitude IS NOT DISTINCT FROM NEW.longitude THEN
        RETURN NEW;
    END IF;

    IF OLD.description IS DISTINCT FROM NEW.description THEN
        changed_columns := changed_columns || jsonb_build_object('description', true);
    END IF;
    IF OLD."dateTimeOriginal" IS DISTINCT FROM NEW."dateTimeOriginal" THEN
        changed_columns := changed_columns || jsonb_build_object('dateTimeOriginal', true);
    END IF;
    IF OLD."timeZone" IS DISTINCT FROM NEW."timeZone" THEN
        changed_columns := changed_columns || jsonb_build_object('timeZone', true);
    END IF;
    IF OLD.latitude IS DISTINCT FROM NEW.latitude THEN
        changed_columns := changed_columns || jsonb_build_object('latitude', true);
    END IF;
    IF OLD.longitude IS DISTINCT FROM NEW.longitude THEN
        changed_columns := changed_columns || jsonb_build_object('longitude', true);
    END IF;

    INSERT INTO _face_sync_event_queue (
        event_type,
        entity_type,
        entity_id,
        payload,
        updated_at
    )
    VALUES (
        'asset_exif_changed',
        'asset_exif',
        NEW."assetId",
        jsonb_build_object('changed_columns', changed_columns),
        NOW()
    )
    ON CONFLICT (
        event_type,
        entity_type,
        entity_id,
        (COALESCE(source_asset_id, '00000000-0000-0000-0000-000000000000'::uuid))
    )
        WHERE status = 'pending'
    DO UPDATE SET
        payload = _face_sync_event_queue.payload || EXCLUDED.payload,
        updated_at = EXCLUDED.updated_at,
        error = NULL
    RETURNING id INTO queue_id;

    PERFORM pg_notify('face_sync_events', jsonb_build_object(
        'event_queue_id', queue_id,
        'event_type', 'asset_exif_changed',
        'entity_type', 'asset_exif',
        'entity_id', NEW."assetId"
    )::text);

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS immich_shared_sidecar_asset_exif_metadata ON asset_exif;
CREATE TRIGGER immich_shared_sidecar_asset_exif_metadata
AFTER UPDATE OF description, "dateTimeOriginal", "timeZone", latitude, longitude ON asset_exif
FOR EACH ROW
EXECUTE FUNCTION immich_shared_sidecar_enqueue_asset_exif_event();
"""

METADATA_TRIGGER_REMOVE_DDL = """
DROP TRIGGER IF EXISTS immich_shared_sidecar_asset_exif_metadata ON asset_exif;
DROP FUNCTION IF EXISTS immich_shared_sidecar_enqueue_asset_exif_event();
"""


async def install_event_queue(conn: Any) -> None:
    """Create the sidecar event queue. Does not install live triggers."""
    await conn.execute(EVENT_QUEUE_DDL)


async def install_metadata_trigger(conn: Any) -> None:
    """Install the first-slice metadata trigger after an explicit rollout gate."""
    await install_event_queue(conn)
    await conn.execute(METADATA_TRIGGER_DDL)


async def remove_metadata_trigger(conn: Any) -> None:
    """Remove the first-slice metadata trigger/function rollback hook."""
    await conn.execute(METADATA_TRIGGER_REMOVE_DDL)


async def reconcile_shared_album_metadata(
    conn: Any,
    api: Any,
    *,
    source_asset_ids: set[UUID] | None = None,
) -> dict[str, int]:
    """Lazy wrapper so SQL helpers remain importable without runtime deps installed."""
    from src.shared_album_metadata import reconcile_shared_album_metadata as reconcile

    return await reconcile(conn, api, source_asset_ids=source_asset_ids)


async def run_event_sync_once(conn: Any, api: Any, *, batch_size: int = 100) -> dict[str, int]:
    """Process one bounded batch if this process can take the advisory lock."""
    locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", EVENT_DAEMON_ADVISORY_LOCK)
    if not locked:
        return {
            "events_claimed": 0,
            "events_done": 0,
            "events_error": 0,
            "logical_sources_reconciled": 0,
        }
    try:
        return await process_pending_metadata_events(conn, api, batch_size=batch_size)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", EVENT_DAEMON_ADVISORY_LOCK)


async def process_pending_metadata_events(conn: Any, api: Any, *, batch_size: int = 100) -> dict[str, int]:
    """Claim and process one bounded batch of pending metadata events.

    Event payloads are wake-up hints only. The reconciler rereads current DB state
    for the affected logical source assets.
    """
    claimed = await conn.fetch(
        """
        UPDATE _face_sync_event_queue
        SET status = 'processing',
            attempts = attempts + 1,
            updated_at = NOW(),
            error = NULL
        WHERE id IN (
            SELECT id
            FROM _face_sync_event_queue
            WHERE status = 'pending'
              AND event_type = 'asset_exif_changed'
              AND entity_type = 'asset_exif'
            ORDER BY created_at, id
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, entity_id, source_asset_id
        """,
        batch_size,
    )
    event_ids = [row["id"] for row in claimed]
    stats = {
        "events_claimed": len(event_ids),
        "events_done": 0,
        "events_error": 0,
        "logical_sources_reconciled": 0,
    }
    if not event_ids:
        return stats

    source_asset_ids = await _resolve_logical_source_asset_ids(conn, claimed)
    try:
        if source_asset_ids:
            await reconcile_shared_album_metadata(conn, api, source_asset_ids=source_asset_ids)
            stats["logical_sources_reconciled"] = len(source_asset_ids)
        await _mark_events_done(conn, event_ids)
        stats["events_done"] = len(event_ids)
    except Exception as exc:  # noqa: BLE001 - queue rows must record failures
        logger.exception("Failed to process shared metadata events: event_ids=%s", event_ids)
        await _mark_events_error(conn, event_ids, exc)
        stats["events_error"] = len(event_ids)
    return stats


async def _resolve_logical_source_asset_ids(conn: Any, claimed_rows: list[Any]) -> set[UUID]:
    explicit_sources = {
        UUID(str(row["source_asset_id"]))
        for row in claimed_rows
        if row["source_asset_id"] is not None
    }
    changed_asset_ids = [row["entity_id"] for row in claimed_rows]
    mapped_rows = await conn.fetch(
        """
        SELECT DISTINCT source_asset_id
        FROM _face_sync_album_map
        WHERE source_asset_id = ANY($1::uuid[])
           OR target_asset_id = ANY($1::uuid[])
        """,
        changed_asset_ids,
    )
    return explicit_sources | {UUID(str(row["source_asset_id"])) for row in mapped_rows}


async def _mark_events_done(conn: Any, event_ids: list[int]) -> None:
    await conn.execute(
        """
        UPDATE _face_sync_event_queue
        SET status = 'done',
            updated_at = NOW(),
            processed_at = NOW(),
            error = NULL
        WHERE id = ANY($1::bigint[])
        """,
        event_ids,
    )


async def _mark_events_error(conn: Any, event_ids: list[int], exc: Exception) -> None:
    await conn.execute(
        """
        UPDATE _face_sync_event_queue
        SET status = 'error',
            updated_at = NOW(),
            processed_at = NOW(),
            error = $2
        WHERE id = ANY($1::bigint[])
        """,
        event_ids,
        str(exc)[:1000],
    )
