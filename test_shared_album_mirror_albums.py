from __future__ import annotations

import unittest
from uuid import UUID

SYNC_IMPORT_ERROR = None

try:
    from src.shared_album_sync import (
        reconcile_shared_album_mirror_albums,
        shared_album_mirror_description,
    )
except ModuleNotFoundError as exc:
    if exc.name in {"asyncpg", "pydantic", "pydantic_settings", "httpx"}:
        SYNC_IMPORT_ERROR = exc
    else:
        raise


SOURCE_ALBUM_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TARGET_USER_ID = UUID("22222222-2222-2222-2222-222222222222")
SOURCE_ASSET_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TARGET_ASSET_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
OWN_ASSET_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
STALE_ASSET_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
MIRROR_ALBUM_ID = UUID("99999999-9999-9999-9999-999999999999")
UNRELATED_ALBUM_ID = UUID("88888888-8888-8888-8888-888888888888")


class FakeMirrorAlbumConnection:
    def __init__(self) -> None:
        self.album_name = "Fasching 2025"
        self.map_rows = [
            {
                "source_album_id": SOURCE_ALBUM_ID,
                "target_user_id": TARGET_USER_ID,
                "source_asset_id": SOURCE_ASSET_ID,
                "target_asset_id": TARGET_ASSET_ID,
                "target_album_id": None,
            }
        ]
        self.albums: dict[UUID, dict] = {}
        self.album_assets: set[tuple[UUID, UUID]] = set()
        self.assets: dict[UUID, dict] = {
            TARGET_ASSET_ID: {"ownerId": TARGET_USER_ID, "deletedAt": None, "isOffline": False, "status": "active"},
            OWN_ASSET_ID: {"ownerId": TARGET_USER_ID, "deletedAt": None, "isOffline": False, "status": "active"},
            STALE_ASSET_ID: {"ownerId": TARGET_USER_ID, "deletedAt": None, "isOffline": False, "status": "active"},
        }
        self.created_albums: list[dict] = []

    async def fetch(self, sql, *args):
        if "information_schema.columns" in sql:
            return [{"column_name": "albumName"}]
        if "FROM _face_sync_album_map m" in sql:
            return [dict(row, source_album_name=self.album_name) for row in self.map_rows]
        if 'JOIN asset a ON a.id = aa."assetId"' in sql:
            source_album_id, target_user_id = args
            rows = []
            for album_id, asset_id in self.album_assets:
                asset = self.assets.get(asset_id)
                if album_id == source_album_id and asset and asset["ownerId"] == target_user_id:
                    rows.append({"assetId": asset_id})
            return rows
        if "INSERT INTO album_asset" in sql:
            album_id, desired_asset_ids = args
            rows = []
            for asset_id in desired_asset_ids:
                entry = (album_id, asset_id)
                if entry not in self.album_assets:
                    self.album_assets.add(entry)
                    rows.append({"assetId": asset_id})
            return rows
        if "DELETE FROM album_asset" in sql:
            album_id, desired_asset_ids = args
            desired = set(desired_asset_ids)
            removed = []
            for entry in list(self.album_assets):
                if entry[0] == album_id and entry[1] not in desired:
                    self.album_assets.remove(entry)
                    removed.append({"assetId": entry[1]})
            return removed
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchrow(self, sql, *args):
        if "WHERE id = ANY" in sql:
            candidate_ids, target_user_id = args
            for album_id in candidate_ids:
                album = self.albums.get(album_id)
                if album and album["ownerId"] == target_user_id and album["deletedAt"] is None:
                    return {"id": album_id}
            return None
        if "AND description = $2" in sql:
            target_user_id, marker = args
            for album_id, album in self.albums.items():
                if album["ownerId"] == target_user_id and album["deletedAt"] is None and album["description"] == marker:
                    return {"id": album_id}
            return None
        if "INSERT INTO album" in sql:
            target_user_id, album_name, description = args
            self.albums[MIRROR_ALBUM_ID] = {
                "ownerId": target_user_id,
                "albumName": album_name,
                "description": description,
                "deletedAt": None,
            }
            self.created_albums.append(self.albums[MIRROR_ALBUM_ID])
            return {"id": MIRROR_ALBUM_ID}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql, *args):
        if "UPDATE _face_sync_album_map" in sql:
            source_album_id, target_user_id, target_album_id = args
            count = 0
            for row in self.map_rows:
                if (
                    row["source_album_id"] == source_album_id
                    and row["target_user_id"] == target_user_id
                    and row["target_album_id"] != target_album_id
                ):
                    row["target_album_id"] = target_album_id
                    count += 1
            return f"UPDATE {count}"
        if 'UPDATE album SET "updatedAt"' in sql:
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")


@unittest.skipIf(
    SYNC_IMPORT_ERROR is not None,
    f"missing dependency: {SYNC_IMPORT_ERROR.name if SYNC_IMPORT_ERROR else ''}",
)
class SharedAlbumMirrorAlbumsTest(unittest.IsolatedAsyncioTestCase):
    async def test_creates_target_mirror_album_and_adds_mirrored_asset(self) -> None:
        conn = FakeMirrorAlbumConnection()

        stats = await reconcile_shared_album_mirror_albums(conn)

        self.assertEqual(stats["mirror_albums_created"], 1)
        self.assertEqual(conn.created_albums[0]["albumName"], "Fasching 2025 (Shared mirror)")
        self.assertEqual(
            conn.created_albums[0]["description"],
            shared_album_mirror_description(SOURCE_ALBUM_ID, TARGET_USER_ID),
        )
        self.assertEqual(conn.map_rows[0]["target_album_id"], MIRROR_ALBUM_ID)
        self.assertIn((MIRROR_ALBUM_ID, TARGET_ASSET_ID), conn.album_assets)

    async def test_reuses_existing_managed_album_by_description_marker(self) -> None:
        conn = FakeMirrorAlbumConnection()
        conn.albums[MIRROR_ALBUM_ID] = {
            "ownerId": TARGET_USER_ID,
            "albumName": "Fasching 2025 (Shared mirror)",
            "description": shared_album_mirror_description(SOURCE_ALBUM_ID, TARGET_USER_ID),
            "deletedAt": None,
        }

        stats = await reconcile_shared_album_mirror_albums(conn)

        self.assertEqual(stats["mirror_albums_created"], 0)
        self.assertEqual(stats["mirror_albums_reused"], 1)
        self.assertEqual(conn.created_albums, [])
        self.assertEqual(conn.map_rows[0]["target_album_id"], MIRROR_ALBUM_ID)

    async def test_reuses_existing_mapped_target_album_when_owned_by_target_user(self) -> None:
        conn = FakeMirrorAlbumConnection()
        conn.map_rows[0]["target_album_id"] = MIRROR_ALBUM_ID
        conn.albums[MIRROR_ALBUM_ID] = {
            "ownerId": TARGET_USER_ID,
            "albumName": "Existing mapped album",
            "description": "not sidecar marker",
            "deletedAt": None,
        }

        stats = await reconcile_shared_album_mirror_albums(conn)

        self.assertEqual(stats["mirror_albums_created"], 0)
        self.assertEqual(stats["mirror_albums_reused"], 1)
        self.assertEqual(conn.created_albums, [])
        self.assertEqual(conn.map_rows[0]["target_album_id"], MIRROR_ALBUM_ID)

    async def test_includes_target_users_own_original_contribution(self) -> None:
        conn = FakeMirrorAlbumConnection()
        conn.album_assets.add((SOURCE_ALBUM_ID, OWN_ASSET_ID))

        await reconcile_shared_album_mirror_albums(conn)

        self.assertIn((MIRROR_ALBUM_ID, TARGET_ASSET_ID), conn.album_assets)
        self.assertIn((MIRROR_ALBUM_ID, OWN_ASSET_ID), conn.album_assets)

    async def test_removes_stale_membership_only_from_managed_target_album(self) -> None:
        conn = FakeMirrorAlbumConnection()
        conn.albums[MIRROR_ALBUM_ID] = {
            "ownerId": TARGET_USER_ID,
            "albumName": "Fasching 2025 (Shared mirror)",
            "description": shared_album_mirror_description(SOURCE_ALBUM_ID, TARGET_USER_ID),
            "deletedAt": None,
        }
        conn.album_assets.add((MIRROR_ALBUM_ID, TARGET_ASSET_ID))
        conn.album_assets.add((MIRROR_ALBUM_ID, STALE_ASSET_ID))
        conn.album_assets.add((UNRELATED_ALBUM_ID, STALE_ASSET_ID))

        stats = await reconcile_shared_album_mirror_albums(conn)

        self.assertEqual(stats["mirror_album_assets_removed"], 1)
        self.assertNotIn((MIRROR_ALBUM_ID, STALE_ASSET_ID), conn.album_assets)
        self.assertIn((UNRELATED_ALBUM_ID, STALE_ASSET_ID), conn.album_assets)


if __name__ == "__main__":
    unittest.main()
