import unittest

SCHEMA_IMPORT_ERROR = None

try:
    from src.config import SYNC_MODE_PATH_PREFIX, SYNC_MODE_SHARED_ALBUMS
    from src.schema import required_schema_for_sync_mode
except ModuleNotFoundError as exc:
    if exc.name in {"asyncpg", "pydantic", "pydantic_settings"}:
        SCHEMA_IMPORT_ERROR = exc
    else:
        raise


@unittest.skipIf(
    SCHEMA_IMPORT_ERROR is not None,
    f"missing dependency: {SCHEMA_IMPORT_ERROR.name if SCHEMA_IMPORT_ERROR else ''}",
)
class SchemaModeRequirementsTest(unittest.TestCase):
    def test_path_prefix_mode_does_not_require_shared_album_tables(self) -> None:
        required_schema = required_schema_for_sync_mode(SYNC_MODE_PATH_PREFIX)

        self.assertNotIn("album_user", required_schema)
        self.assertNotIn("shared_link", required_schema)
        self.assertNotIn("shared_link_asset", required_schema)

    def test_shared_albums_mode_requires_authenticated_and_public_link_tables(self) -> None:
        required_schema = required_schema_for_sync_mode(SYNC_MODE_SHARED_ALBUMS)

        self.assertEqual(required_schema["album_user"], {"albumId", "userId", "role"})
        self.assertEqual(required_schema["shared_link"], {"albumId", "type"})
        self.assertEqual(required_schema["shared_link_asset"], {"sharedLinkId", "assetId"})


if __name__ == "__main__":
    unittest.main()
