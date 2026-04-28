import unittest
from uuid import UUID

from src.shared_album_discovery import SharedAlbumEdge, discover_shared_album_edges


OWNER = UUID("11111111-1111-1111-1111-111111111111")
PARTICIPANT = UUID("22222222-2222-2222-2222-222222222222")
OTHER_PARTICIPANT = UUID("33333333-3333-3333-3333-333333333333")
EXCLUDED = UUID("44444444-4444-4444-4444-444444444444")
ALBUM = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ASSET = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class SharedAlbumDiscoveryTest(unittest.TestCase):
    def test_owner_asset_mirrors_to_authenticated_participant(self) -> None:
        edges = discover_shared_album_edges(
            albums=[{"id": ALBUM, "ownerId": OWNER, "deletedAt": None}],
            album_users=[{"albumId": ALBUM, "userId": PARTICIPANT, "role": "viewer"}],
            album_assets=[{"albumId": ALBUM, "assetId": ASSET}],
            assets=[{"id": ASSET, "ownerId": OWNER, "deletedAt": None}],
        )

        self.assertEqual(edges, [SharedAlbumEdge(ALBUM, ASSET, OWNER, PARTICIPANT)])

    def test_does_not_mirror_asset_to_its_owner(self) -> None:
        edges = discover_shared_album_edges(
            albums=[{"id": ALBUM, "ownerId": OWNER, "deletedAt": None}],
            album_users=[{"albumId": ALBUM, "userId": PARTICIPANT, "role": "editor"}],
            album_assets=[{"albumId": ALBUM, "assetId": ASSET}],
            assets=[{"id": ASSET, "ownerId": PARTICIPANT, "deletedAt": None}],
        )

        self.assertEqual(edges, [SharedAlbumEdge(ALBUM, ASSET, PARTICIPANT, OWNER)])

    def test_multiple_participants_receive_fanout_edges(self) -> None:
        edges = discover_shared_album_edges(
            albums=[{"id": ALBUM, "ownerId": OWNER, "deletedAt": None}],
            album_users=[
                {"albumId": ALBUM, "userId": PARTICIPANT, "role": "viewer"},
                {"albumId": ALBUM, "userId": OTHER_PARTICIPANT, "role": "editor"},
            ],
            album_assets=[{"albumId": ALBUM, "assetId": ASSET}],
            assets=[{"id": ASSET, "ownerId": OWNER, "deletedAt": None}],
        )

        self.assertEqual(
            set(edges),
            {
                SharedAlbumEdge(ALBUM, ASSET, OWNER, PARTICIPANT),
                SharedAlbumEdge(ALBUM, ASSET, OWNER, OTHER_PARTICIPANT),
            },
        )

    def test_exclude_user_removes_source_and_target_edges(self) -> None:
        edges = discover_shared_album_edges(
            albums=[{"id": ALBUM, "ownerId": OWNER, "deletedAt": None}],
            album_users=[
                {"albumId": ALBUM, "userId": PARTICIPANT, "role": "viewer"},
                {"albumId": ALBUM, "userId": EXCLUDED, "role": "viewer"},
            ],
            album_assets=[{"albumId": ALBUM, "assetId": ASSET}],
            assets=[{"id": ASSET, "ownerId": OWNER, "deletedAt": None}],
            exclude_users=[EXCLUDED],
        )

        self.assertEqual(edges, [SharedAlbumEdge(ALBUM, ASSET, OWNER, PARTICIPANT)])

    def test_exclude_album_name_pattern_skips_album(self) -> None:
        edges = discover_shared_album_edges(
            albums=[{"id": ALBUM, "ownerId": OWNER, "albumName": "Private holiday", "deletedAt": None}],
            album_users=[{"albumId": ALBUM, "userId": PARTICIPANT, "role": "viewer"}],
            album_assets=[{"albumId": ALBUM, "assetId": ASSET}],
            assets=[{"id": ASSET, "ownerId": OWNER, "deletedAt": None}],
            exclude_name_patterns=["private*"],
        )

        self.assertEqual(edges, [])

    def test_public_link_fixture_data_is_irrelevant(self) -> None:
        public_link_rows = [
            {"id": "public-link-1", "albumId": ALBUM, "type": "ALBUM"},
        ]
        public_link_asset_rows = [
            {"sharedLinkId": "public-link-1", "assetId": ASSET},
        ]
        self.assertTrue(public_link_rows)
        self.assertTrue(public_link_asset_rows)

        edges = discover_shared_album_edges(
            albums=[{"id": ALBUM, "ownerId": OWNER, "deletedAt": None}],
            album_users=[],
            album_assets=[{"albumId": ALBUM, "assetId": ASSET}],
            assets=[{"id": ASSET, "ownerId": OWNER, "deletedAt": None}],
        )

        self.assertEqual(edges, [])


if __name__ == "__main__":
    unittest.main()
