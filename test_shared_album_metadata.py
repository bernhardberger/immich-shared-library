from __future__ import annotations

from datetime import datetime, timezone
import unittest
from uuid import UUID

METADATA_IMPORT_ERROR = None

try:
    from src.shared_album_metadata import reconcile_shared_album_metadata
except ModuleNotFoundError as exc:
    if exc.name == "asyncpg":
        METADATA_IMPORT_ERROR = exc
    else:
        raise


SOURCE_ASSET = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TARGET_ASSET_1 = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TARGET_ASSET_2 = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
PATH_PREFIX_ONLY_SOURCE = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def row(
    asset_id: UUID,
    *,
    source_asset_id: UUID = SOURCE_ASSET,
    taken_at=None,
    time_zone=None,
    latitude=None,
    longitude=None,
    description=None,
):
    return {
        "source_asset_id": source_asset_id,
        "asset_id": asset_id,
        "dateTimeOriginal": taken_at,
        "timeZone": time_zone,
        "latitude": latitude,
        "longitude": longitude,
        "description": description,
    }


class FakeConnection:
    def __init__(self, rows, state=None):
        self.rows = rows
        self.state = state or {}
        self.fetch_calls = []
        self.execute_calls = []

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        if "_face_sync_metadata_state" in sql:
            return [
                {
                    "source_asset_id": source_asset_id,
                    "field_group": field_group,
                    "synced_value": value,
                }
                for (source_asset_id, field_group), value in self.state.items()
            ]
        return self.rows

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


@unittest.skipIf(
    METADATA_IMPORT_ERROR is not None,
    f"missing dependency: {METADATA_IMPORT_ERROR.name if METADATA_IMPORT_ERROR else ''}",
)
class SharedAlbumMetadataSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_all_empty_records_state_without_api_writes(self) -> None:
        conn = FakeConnection([
            row(SOURCE_ASSET),
            row(TARGET_ASSET_1),
        ])

        stats = await reconcile_shared_album_metadata(conn)

        self.assertEqual(stats["metadata_fields_initialized"], 3)
        self.assertEqual(stats["metadata_assets_updated"], 0)
        recorded_groups = {call[1][1] for call in conn.execute_calls}
        self.assertEqual(recorded_groups, {"taken_at", "location", "description"})

    async def test_bootstrap_one_non_empty_value_propagates_to_other_copies(self) -> None:
        conn = FakeConnection([
            row(SOURCE_ASSET),
            row(TARGET_ASSET_1, description="Fasching"),
            row(TARGET_ASSET_2),
        ])

        stats = await reconcile_shared_album_metadata(conn)

        fallback_calls = [call for call in conn.execute_calls if 'UPDATE asset_exif' in call[0]]
        self.assertEqual(
            [call[1] for call in fallback_calls],
            [
                (SOURCE_ASSET, "Fasching"),
                (TARGET_ASSET_2, "Fasching"),
            ],
        )
        self.assertEqual(stats["metadata_fields_propagated"], 1)
        self.assertEqual(stats["metadata_assets_updated"], 2)

    async def test_existing_state_one_mirror_change_propagates_to_original_and_other_mirror(self) -> None:
        state = {(SOURCE_ASSET, "description"): "Leobersdorf"}
        conn = FakeConnection([
            row(SOURCE_ASSET, description="Leobersdorf"),
            row(TARGET_ASSET_1, description="Kottingbrunn"),
            row(TARGET_ASSET_2, description="Leobersdorf"),
        ], state=state)

        stats = await reconcile_shared_album_metadata(conn)

        fallback_calls = [call for call in conn.execute_calls if 'UPDATE asset_exif' in call[0]]
        self.assertEqual(
            [call[1] for call in fallback_calls],
            [
                (SOURCE_ASSET, "Kottingbrunn"),
                (TARGET_ASSET_2, "Kottingbrunn"),
            ],
        )
        self.assertEqual(stats["metadata_fields_propagated"], 1)

    async def test_existing_state_date_change_does_not_overwrite_unchanged_location(self) -> None:
        old_date = datetime(2025, 4, 20, 10, 0, tzinfo=timezone.utc)
        new_date = datetime(2025, 4, 21, 10, 0, tzinfo=timezone.utc)
        state = {
            (SOURCE_ASSET, "taken_at"): {"dateTimeOriginal": old_date.isoformat(), "timeZone": "Europe/Vienna"},
            (SOURCE_ASSET, "location"): {"latitude": 47.927, "longitude": 16.216},
        }
        conn = FakeConnection([
            row(SOURCE_ASSET, taken_at=new_date, time_zone="Europe/Vienna", latitude=47.927, longitude=16.216),
            row(TARGET_ASSET_1, taken_at=old_date, time_zone="Europe/Vienna", latitude=47.927, longitude=16.216),
        ], state=state)

        stats = await reconcile_shared_album_metadata(conn)

        fallback_calls = [call for call in conn.execute_calls if 'UPDATE asset_exif' in call[0]]
        self.assertEqual(len(fallback_calls), 1)
        self.assertEqual(fallback_calls[0][1], (TARGET_ASSET_1, new_date, "Europe/Vienna"))
        self.assertEqual(stats["metadata_fields_propagated"], 1)

    async def test_multiple_different_changed_values_record_conflict_without_api_writes(self) -> None:
        state = {(SOURCE_ASSET, "description"): "Leobersdorf"}
        conn = FakeConnection([
            row(SOURCE_ASSET, description="Kottingbrunn"),
            row(TARGET_ASSET_1, description="Baden"),
        ], state=state)

        stats = await reconcile_shared_album_metadata(conn)

        self.assertEqual(stats["metadata_conflicts"], 1)
        conflict_calls = [call for call in conn.execute_calls if call[1][3] is not None]
        self.assertEqual(len(conflict_calls), 1)

    async def test_one_copy_clears_value_and_no_other_change_propagates_clear(self) -> None:
        state = {(SOURCE_ASSET, "description"): "Ostern"}
        conn = FakeConnection([
            row(SOURCE_ASSET, description=None),
            row(TARGET_ASSET_1, description="Ostern"),
            row(TARGET_ASSET_2, description="Ostern"),
        ], state=state)

        stats = await reconcile_shared_album_metadata(conn)

        fallback_calls = [call for call in conn.execute_calls if 'UPDATE asset_exif' in call[0]]
        self.assertEqual(
            [call[1] for call in fallback_calls],
            [
                (TARGET_ASSET_1, ""),
                (TARGET_ASSET_2, ""),
            ],
        )
        self.assertEqual(stats["metadata_fields_propagated"], 1)

    async def test_one_copy_clears_location_records_conflict_because_db_sync_does_not_clear_gps(self) -> None:
        state = {(SOURCE_ASSET, "location"): {"latitude": 47.927, "longitude": 16.216}}
        conn = FakeConnection([
            row(SOURCE_ASSET, latitude=None, longitude=None),
            row(TARGET_ASSET_1, latitude=47.927, longitude=16.216),
        ], state=state)

        stats = await reconcile_shared_album_metadata(conn)

        self.assertEqual(stats["metadata_conflicts"], 1)
        self.assertEqual([call for call in conn.execute_calls if 'UPDATE asset_exif' in call[0]], [])

    async def test_description_update_writes_db_directly_and_records_state(self) -> None:
        conn = FakeConnection([
            row(SOURCE_ASSET, description="Fasching"),
            row(TARGET_ASSET_1),
        ])

        stats = await reconcile_shared_album_metadata(conn)

        self.assertEqual(stats["metadata_fields_propagated"], 1)
        self.assertEqual(stats["metadata_assets_updated"], 1)
        fallback_calls = [call for call in conn.execute_calls if 'UPDATE asset_exif' in call[0]]
        self.assertEqual(len(fallback_calls), 1)
        self.assertIn('description = $2', fallback_calls[0][0])
        self.assertEqual(fallback_calls[0][1], (TARGET_ASSET_1, "Fasching"))
        self.assertEqual([call for call in conn.execute_calls if 'UPDATE asset\n' in call[0]], [])
        suppress_calls = [call for call in conn.execute_calls if 'suppress_events' in call[0]]
        self.assertEqual(len(suppress_calls), 1)
        self.assertLess(conn.execute_calls.index(suppress_calls[0]), conn.execute_calls.index(fallback_calls[0]))
        state_calls = [call for call in conn.execute_calls if 'INSERT INTO _face_sync_metadata_state' in call[0]]
        self.assertEqual(state_calls[-1][1][1], "description")
        self.assertEqual(state_calls[-1][1][2], '"Fasching"')

    async def test_description_clear_db_update_writes_empty_string_not_null(self) -> None:
        state = {(SOURCE_ASSET, "description"): "Ostern"}
        conn = FakeConnection([
            row(SOURCE_ASSET, description=None),
            row(TARGET_ASSET_1, description="Ostern"),
        ], state=state)

        stats = await reconcile_shared_album_metadata(conn)

        self.assertEqual(stats["metadata_fields_propagated"], 1)
        fallback_calls = [call for call in conn.execute_calls if 'UPDATE asset_exif' in call[0]]
        self.assertEqual(len(fallback_calls), 1)
        self.assertIn('description = $2', fallback_calls[0][0])
        self.assertEqual(fallback_calls[0][1], (TARGET_ASSET_1, ""))

    async def test_db_metadata_update_updates_taken_at_and_location_asset_exif_columns(self) -> None:
        old_date = datetime(2025, 4, 20, 10, 0, tzinfo=timezone.utc)
        new_date = datetime(2025, 4, 21, 10, 0, tzinfo=timezone.utc)
        state = {
            (SOURCE_ASSET, "taken_at"): {"dateTimeOriginal": old_date.isoformat(), "timeZone": "Europe/Vienna"},
            (SOURCE_ASSET, "location"): {"latitude": 47.927, "longitude": 16.216},
        }
        conn = FakeConnection([
            row(SOURCE_ASSET, taken_at=new_date, time_zone="Europe/Vienna", latitude=48.0, longitude=16.5),
            row(TARGET_ASSET_1, taken_at=old_date, time_zone="Europe/Vienna", latitude=47.927, longitude=16.216),
        ], state=state)

        stats = await reconcile_shared_album_metadata(conn)

        self.assertEqual(stats["metadata_fields_propagated"], 2)
        fallback_calls = [call for call in conn.execute_calls if 'UPDATE asset_exif' in call[0]]
        self.assertEqual(len(fallback_calls), 2)
        self.assertIn('"dateTimeOriginal" = $2', fallback_calls[0][0])
        self.assertIn('"timeZone" = $3', fallback_calls[0][0])
        self.assertEqual(fallback_calls[0][1], (TARGET_ASSET_1, new_date, "Europe/Vienna"))
        asset_calls = [call for call in conn.execute_calls if 'UPDATE asset\n' in call[0]]
        self.assertEqual(len(asset_calls), 1)
        self.assertIn('"fileCreatedAt" = $2', asset_calls[0][0])
        self.assertIn('"localDateTime" = $2', asset_calls[0][0])
        self.assertEqual(asset_calls[0][1], (TARGET_ASSET_1, new_date))
        self.assertIn('latitude = $2', fallback_calls[1][0])
        self.assertIn('longitude = $3', fallback_calls[1][0])
        self.assertEqual(fallback_calls[1][1], (TARGET_ASSET_1, 48.0, 16.5))
        recorded_groups = [call[1][1] for call in conn.execute_calls if 'INSERT INTO _face_sync_metadata_state' in call[0]]
        self.assertIn("taken_at", recorded_groups)
        self.assertIn("location", recorded_groups)

    async def test_path_prefix_only_asset_map_rows_are_ignored_by_query_scope(self) -> None:
        conn = FakeConnection([row(SOURCE_ASSET), row(TARGET_ASSET_1)])

        await reconcile_shared_album_metadata(conn)

        metadata_sql = conn.fetch_calls[0][0]
        self.assertIn("_face_sync_album_map", metadata_sql)
        self.assertNotIn("FROM _face_sync_asset_map", metadata_sql)


if __name__ == "__main__":
    unittest.main()
