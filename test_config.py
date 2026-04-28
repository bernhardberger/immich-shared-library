import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

CONFIG_IMPORT_ERROR = None

try:
    from src.config import (
        SYNC_MODE_PATH_PREFIX,
        SYNC_MODE_SHARED_ALBUMS,
        Settings,
        load_config,
        load_sync_jobs,
    )
except ModuleNotFoundError as exc:
    if exc.name in {"pydantic", "pydantic_settings"}:
        CONFIG_IMPORT_ERROR = exc
    else:
        raise


SOURCE_USER_ID = "11111111-1111-1111-1111-111111111111"
TARGET_USER_ID = "22222222-2222-2222-2222-222222222222"
TARGET_LIBRARY_ID = "33333333-3333-3333-3333-333333333333"
TARGET_ALBUM_ID = "44444444-4444-4444-4444-444444444444"
EXCLUDED_USER_ID = "55555555-5555-5555-5555-555555555555"


@unittest.skipIf(
    CONFIG_IMPORT_ERROR is not None,
    f"missing dependency: {CONFIG_IMPORT_ERROR.name if CONFIG_IMPORT_ERROR else ''}",
)
class ConfigParsingTest(unittest.TestCase):
    def write_config(self, temp_dir: str, content: str) -> Path:
        path = Path(temp_dir) / "config.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_sync_jobs_yaml_defaults_to_path_prefix_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_config(
                temp_dir,
                f"""
sync_jobs:
  - name: external-library
    source_user_id: "{SOURCE_USER_ID}"
    target_user_id: "{TARGET_USER_ID}"
    target_library_id: "{TARGET_LIBRARY_ID}"
    source_path_prefix: "/external/source/"
    target_path_prefix: "/external/target/"
    album_id: "{TARGET_ALBUM_ID}"
""",
            )

            config = load_config(str(path))

            self.assertEqual(config.sync_mode, SYNC_MODE_PATH_PREFIX)
            self.assertIsNone(config.shared_albums)
            self.assertEqual(len(config.sync_jobs), 1)
            self.assertEqual(load_sync_jobs(str(path)), config.sync_jobs)

            job = config.sync_jobs[0]
            self.assertEqual(job.name, "external-library")
            self.assertEqual(job.source_user_id, UUID(SOURCE_USER_ID))
            self.assertEqual(job.target_user_id, UUID(TARGET_USER_ID))
            self.assertEqual(job.target_library_id, UUID(TARGET_LIBRARY_ID))
            self.assertEqual(job.source_path_prefix, "/external/source/")
            self.assertEqual(job.target_path_prefix, "/external/target/")
            self.assertEqual(job.album_id, UUID(TARGET_ALBUM_ID))

    def test_env_fallback_still_builds_path_prefix_jobs(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(
                config_file="/tmp/immich-shared-library-missing-config.yaml",
                source_user_id=SOURCE_USER_ID,
                target_user_id=TARGET_USER_ID,
                target_library_id=TARGET_LIBRARY_ID,
                shared_path_prefix="/external/source/",
                target_path_prefix="/external/target/",
                target_album_id=TARGET_ALBUM_ID,
            )

            config = settings.sync_config

        self.assertEqual(config.sync_mode, SYNC_MODE_PATH_PREFIX)
        self.assertEqual(len(config.sync_jobs), 1)
        self.assertEqual(config.sync_jobs[0].album_id, UUID(TARGET_ALBUM_ID))

    def test_shared_albums_yaml_parses_mode_scope_and_albums(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_config(
                temp_dir,
                f"""
sync_mode: shared_albums
scope:
  mode: all_local_users
  exclude_users:
    - "{EXCLUDED_USER_ID}"
albums:
  include: all_shared_albums
  exclude_name_patterns:
    - "Private*"
""",
            )

            config = load_config(str(path))

            self.assertEqual(config.sync_mode, SYNC_MODE_SHARED_ALBUMS)
            self.assertEqual(config.sync_jobs, [])
            self.assertIsNotNone(config.shared_albums)
            shared_albums = config.shared_albums
            assert shared_albums is not None
            self.assertEqual(shared_albums.scope.mode, "all_local_users")
            self.assertEqual(shared_albums.scope.exclude_users, (UUID(EXCLUDED_USER_ID),))
            self.assertEqual(shared_albums.albums.include, "all_shared_albums")
            self.assertEqual(shared_albums.albums.exclude_name_patterns, ("Private*",))

            with self.assertRaisesRegex(ValueError, "does not define sync_jobs"):
                load_sync_jobs(str(path))

    def test_shared_albums_and_sync_jobs_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_config(
                temp_dir,
                f"""
sync_mode: shared_albums
sync_jobs:
  - name: external-library
    source_user_id: "{SOURCE_USER_ID}"
    target_user_id: "{TARGET_USER_ID}"
    target_library_id: "{TARGET_LIBRARY_ID}"
    source_path_prefix: "/external/source/"
    target_path_prefix: "/external/target/"
""",
            )

            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                load_config(str(path))


if __name__ == "__main__":
    unittest.main()
