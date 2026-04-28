import os
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from src.shared_album_shadow import (
    assert_path_within_root,
    create_shadow_symlink,
    remove_shadow_path,
    shadow_asset_relative_path,
    shadow_library_import_path,
    shadow_library_name,
)


SOURCE_USER = UUID("11111111-1111-1111-1111-111111111111")
TARGET_USER = UUID("22222222-2222-2222-2222-222222222222")
SOURCE_ASSET = UUID("33333333-3333-3333-3333-333333333333")


class SharedAlbumShadowTest(unittest.TestCase):
    def test_shadow_library_name_and_import_path_are_deterministic_per_target(self) -> None:
        self.assertEqual(
            shadow_library_name("Immich Shared Library Mirrors", TARGET_USER),
            f"Immich Shared Library Mirrors - {TARGET_USER}",
        )
        self.assertEqual(
            shadow_library_import_path("/external_library/mirrors", TARGET_USER),
            f"/external_library/mirrors/{TARGET_USER}",
        )

    def test_shadow_asset_relative_path_uses_safe_stable_layout(self) -> None:
        self.assertEqual(
            shadow_asset_relative_path(
                target_user_id=TARGET_USER,
                source_user_id=SOURCE_USER,
                source_asset_id=SOURCE_ASSET,
                original_filename="IMG_0001.JPG",
            ),
            Path(str(TARGET_USER)) / str(SOURCE_USER) / str(SOURCE_ASSET) / "IMG_0001.JPG",
        )

    def test_shadow_asset_relative_path_rejects_path_separator_in_filename(self) -> None:
        with self.assertRaisesRegex(ValueError, "original_filename must be a plain file name"):
            shadow_asset_relative_path(
                target_user_id=TARGET_USER,
                source_user_id=SOURCE_USER,
                source_asset_id=SOURCE_ASSET,
                original_filename="../evil.jpg",
            )

    def test_assert_path_within_root_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            root.mkdir()

            with self.assertRaisesRegex(ValueError, "escapes shadow root"):
                assert_path_within_root(root, root / ".." / "evil.jpg")

    def test_create_and_remove_shadow_symlink_stays_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            root = temp_path / "shadow-root"
            source = temp_path / "source.jpg"
            source.write_text("original", encoding="utf-8")
            relative_path = shadow_asset_relative_path(
                target_user_id=TARGET_USER,
                source_user_id=SOURCE_USER,
                source_asset_id=SOURCE_ASSET,
                original_filename="source.jpg",
            )

            link_path = create_shadow_symlink(root, relative_path, source)

            self.assertTrue(link_path.is_symlink())
            self.assertEqual(os.readlink(link_path), str(source))

            self.assertTrue(remove_shadow_path(root, relative_path))
            self.assertFalse(link_path.exists())
            self.assertFalse(remove_shadow_path(root, relative_path))

    def test_create_shadow_symlink_rejects_intermediate_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            root = temp_path / "shadow-root"
            outside = temp_path / "outside"
            root.mkdir()
            outside.mkdir()
            (root / str(TARGET_USER)).symlink_to(outside, target_is_directory=True)
            source = temp_path / "source.jpg"
            source.write_text("original", encoding="utf-8")
            relative_path = Path(str(TARGET_USER)) / str(SOURCE_USER) / str(SOURCE_ASSET) / "source.jpg"

            with self.assertRaisesRegex(ValueError, "escapes shadow root"):
                create_shadow_symlink(root, relative_path, source)
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
