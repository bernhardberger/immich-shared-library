import logging
import os
import posixpath
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

SYNC_MODE_PATH_PREFIX = "path_prefix"
SYNC_MODE_SHARED_ALBUMS = "shared_albums"


@dataclass(frozen=True)
class SyncJob:
    name: str
    source_user_id: UUID
    target_user_id: UUID
    target_library_id: UUID
    source_path_prefix: str
    target_path_prefix: str
    album_id: UUID | None = field(default=None)


@dataclass(frozen=True)
class SharedAlbumsScopeConfig:
    mode: str = "all_local_users"
    exclude_users: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class SharedAlbumsAlbumConfig:
    include: str = "all_shared_albums"
    exclude_name_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class SharedAlbumsShadowLibraryConfig:
    name_prefix: str = "Immich Shared Library Mirrors"
    filesystem_root: str = "/external_library/.immich-shared-library/shared-albums"
    import_path_prefix: str = "/external_library/.immich-shared-library/shared-albums"
    auto_create: bool = True
    auto_scan: bool = False


@dataclass(frozen=True)
class SharedAlbumsConfig:
    scope: SharedAlbumsScopeConfig = field(default_factory=SharedAlbumsScopeConfig)
    albums: SharedAlbumsAlbumConfig = field(default_factory=SharedAlbumsAlbumConfig)
    shadow_library: SharedAlbumsShadowLibraryConfig = field(
        default_factory=SharedAlbumsShadowLibraryConfig
    )


@dataclass(frozen=True)
class AppConfig:
    sync_mode: str
    sync_jobs: list[SyncJob] = field(default_factory=list)
    shared_albums: SharedAlbumsConfig | None = None


def load_config(config_path: str) -> AppConfig:
    """Load the sidecar config file.

    Existing configs with only sync_jobs are treated as path-prefix mode.
    shared_albums mode is parsed here but its runtime discovery is added later.
    """
    import yaml
    path = Path(config_path)
    data = yaml.safe_load(path.read_text())

    if not isinstance(data, dict):
        raise ValueError(f"{config_path}: must contain a mapping")

    sync_mode = str(data.get("sync_mode") or SYNC_MODE_PATH_PREFIX)
    if sync_mode not in {SYNC_MODE_PATH_PREFIX, SYNC_MODE_SHARED_ALBUMS}:
        raise ValueError(
            f"{config_path}: sync_mode must be one of: "
            f"{SYNC_MODE_PATH_PREFIX}, {SYNC_MODE_SHARED_ALBUMS}"
        )

    has_sync_jobs = "sync_jobs" in data
    if sync_mode == SYNC_MODE_SHARED_ALBUMS:
        if has_sync_jobs:
            raise ValueError(
                f"{config_path}: sync_mode: {SYNC_MODE_SHARED_ALBUMS} and "
                "sync_jobs are mutually exclusive"
            )
        return AppConfig(
            sync_mode=SYNC_MODE_SHARED_ALBUMS,
            shared_albums=_parse_shared_albums_config(config_path, data),
        )

    return AppConfig(
        sync_mode=SYNC_MODE_PATH_PREFIX,
        sync_jobs=_parse_sync_jobs(config_path, data),
    )

def load_sync_jobs(config_path: str) -> list[SyncJob]:
    """Load sync jobs from a YAML config file.
    Validates required fields, UUID format, and unique job names.
    Raises ValueError on invalid config.
    """
    config = load_config(config_path)
    if config.sync_mode != SYNC_MODE_PATH_PREFIX:
        raise ValueError(f"{config_path}: sync_mode '{config.sync_mode}' does not define sync_jobs")
    return config.sync_jobs


def _parse_sync_jobs(config_path: str, data: dict) -> list[SyncJob]:
    """Parse path-prefix sync jobs from a loaded YAML mapping."""

    if not isinstance(data, dict) or "sync_jobs" not in data:
        raise ValueError(f"{config_path}: must contain a 'sync_jobs' key")

    raw_jobs = data["sync_jobs"]
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ValueError(f"{config_path}: 'sync_jobs' must be a non-empty list")

    required_fields = [
        "name", "source_user_id", "target_user_id",
        "target_library_id", "source_path_prefix", "target_path_prefix",
    ]

    jobs: list[SyncJob] = []
    names: set[str] = set()

    for i, raw in enumerate(raw_jobs):
        if not isinstance(raw, dict):
            raise ValueError(f"{config_path}: sync_jobs[{i}] must be a mapping")

        missing = [f for f in required_fields if not raw.get(f)]
        if missing:
            raise ValueError(
                f"{config_path}: sync_jobs[{i}] missing required fields: {', '.join(missing)}"
            )

        name = str(raw["name"])
        if name in names:
            raise ValueError(f"{config_path}: duplicate job name '{name}'")
        names.add(name)

        try:
            album_id = UUID(raw["album_id"]) if raw.get("album_id") else None
            jobs.append(SyncJob(
                name=name,
                source_user_id=UUID(raw["source_user_id"]),
                target_user_id=UUID(raw["target_user_id"]),
                target_library_id=UUID(raw["target_library_id"]),
                source_path_prefix=raw["source_path_prefix"],
                target_path_prefix=raw["target_path_prefix"],
                album_id=album_id,
            ))
        except ValueError as e:
            raise ValueError(f"{config_path}: sync_jobs[{i}] ({name}): {e}") from e

    return jobs


def _parse_shared_albums_config(config_path: str, data: dict) -> SharedAlbumsConfig:
    """Parse shared-albums mode options from a loaded YAML mapping."""
    scope_data = data.get("scope", {})
    if scope_data is None:
        scope_data = {}
    if not isinstance(scope_data, dict):
        raise ValueError(f"{config_path}: 'scope' must be a mapping")

    scope_mode = str(scope_data.get("mode") or "all_local_users")
    if scope_mode != "all_local_users":
        raise ValueError(f"{config_path}: scope.mode must be 'all_local_users'")

    exclude_users_raw = scope_data.get("exclude_users", [])
    if exclude_users_raw is None:
        exclude_users_raw = []
    if not isinstance(exclude_users_raw, list):
        raise ValueError(f"{config_path}: scope.exclude_users must be a list")
    try:
        exclude_users = tuple(UUID(str(user_id)) for user_id in exclude_users_raw)
    except ValueError as e:
        raise ValueError(f"{config_path}: scope.exclude_users contains invalid UUID: {e}") from e

    albums_data = data.get("albums", {})
    if albums_data is None:
        albums_data = {}
    if not isinstance(albums_data, dict):
        raise ValueError(f"{config_path}: 'albums' must be a mapping")

    albums_include = str(albums_data.get("include") or "all_shared_albums")
    if albums_include != "all_shared_albums":
        raise ValueError(f"{config_path}: albums.include must be 'all_shared_albums'")

    patterns_raw = albums_data.get("exclude_name_patterns", [])
    if patterns_raw is None:
        patterns_raw = []
    if not isinstance(patterns_raw, list) or not all(
        isinstance(p, str) for p in patterns_raw
    ):
        raise ValueError(f"{config_path}: albums.exclude_name_patterns must be a list of strings")

    return SharedAlbumsConfig(
        scope=SharedAlbumsScopeConfig(
            mode=scope_mode,
            exclude_users=exclude_users,
        ),
        albums=SharedAlbumsAlbumConfig(
            include=albums_include,
            exclude_name_patterns=tuple(patterns_raw),
        ),
        shadow_library=_parse_shadow_library_config(config_path, data.get("shadow_library", {})),
    )


def _parse_shadow_library_config(
    config_path: str,
    shadow_data: object,
) -> SharedAlbumsShadowLibraryConfig:
    if shadow_data is None:
        shadow_data = {}
    if not isinstance(shadow_data, dict):
        raise ValueError(f"{config_path}: 'shadow_library' must be a mapping")

    defaults = SharedAlbumsShadowLibraryConfig()
    name_prefix = str(shadow_data.get("name_prefix") or defaults.name_prefix).strip()
    if not name_prefix:
        raise ValueError(f"{config_path}: shadow_library.name_prefix must not be empty")

    filesystem_root = _validate_absolute_config_path(
        config_path,
        "shadow_library.filesystem_root",
        shadow_data.get("filesystem_root") or defaults.filesystem_root,
        allow_root=False,
    )
    import_path_prefix = _validate_absolute_config_path(
        config_path,
        "shadow_library.import_path_prefix",
        shadow_data.get("import_path_prefix") or defaults.import_path_prefix,
        allow_root=False,
    )

    return SharedAlbumsShadowLibraryConfig(
        name_prefix=name_prefix,
        filesystem_root=filesystem_root,
        import_path_prefix=import_path_prefix,
        auto_create=_get_bool_config_value(
            config_path,
            shadow_data,
            "shadow_library.auto_create",
            "auto_create",
            defaults.auto_create,
        ),
        auto_scan=_get_bool_config_value(
            config_path,
            shadow_data,
            "shadow_library.auto_scan",
            "auto_scan",
            defaults.auto_scan,
        ),
    )


def _validate_absolute_config_path(
    config_path: str,
    field_name: str,
    value: object,
    *,
    allow_root: bool,
) -> str:
    path = str(value or "").strip()
    if not path.startswith("/"):
        raise ValueError(f"{config_path}: {field_name} must be an absolute path")
    normalized = posixpath.normpath(path)
    if normalized == "/" and not allow_root:
        raise ValueError(f"{config_path}: {field_name} must not be /")
    return normalized


def _get_bool_config_value(
    config_path: str,
    data: dict,
    field_name: str,
    key: str,
    default: bool,
) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{config_path}: {field_name} must be a boolean")
    return value


class Settings(BaseSettings):
    db_hostname: str = "localhost"
    db_port: int = 5432
    db_username: str = "postgres"
    db_password: SecretStr = SecretStr("postgres")
    db_database_name: str = "immich"

    immich_api_url: str = "http://immich_server:2283"
    immich_api_key: SecretStr = SecretStr("")

    sync_interval_seconds: int = Field(default=60, ge=5)

    source_user_id: str = ""
    target_user_id: str = ""
    target_library_id: str = ""
    shared_path_prefix: str = ""
    target_path_prefix: str = ""

    upload_location_mount: str = "/usr/src/app/upload"

    # Internal library sync (optional, disabled when upload_source_user_id is empty)
    upload_source_user_id: str = ""
    upload_target_user_id: str = ""
    upload_target_library_id: str = ""
    target_upload_path_prefix: str = ""

    # Album (optional, legacy — use per-job album_id in config.yaml)
    target_album_id: str = ""

    # Path to YAML config file (overrides per-job env vars when file exists)
    config_file: str = "/app/config.yaml"

    log_level: str = "INFO"

    @cached_property
    def source_uid(self) -> UUID:
        return UUID(self.source_user_id)

    @cached_property
    def target_uid(self) -> UUID:
        return UUID(self.target_user_id)

    @cached_property
    def target_lid(self) -> UUID:
        return UUID(self.target_library_id)

    @cached_property
    def upload_source_uid(self) -> UUID:
        return UUID(self.upload_source_user_id)

    @cached_property
    def upload_target_uid(self) -> UUID:
        if self.upload_target_user_id:
            return UUID(self.upload_target_user_id)
        return self.target_uid

    @cached_property
    def upload_target_lid(self) -> UUID:
        return UUID(self.upload_target_library_id)

    @cached_property
    def upload_path_prefix(self) -> str:
        """Source path prefix for internal library assets: {upload_location_mount}/library/{upload_source_user_id}/"""
        return f"{self.upload_location_mount}/library/{self.upload_source_user_id}/"

    @cached_property
    def target_album_uid(self) -> UUID | None:
        return UUID(self.target_album_id) if self.target_album_id else None

    @cached_property
    def sync_jobs(self) -> list[SyncJob]:
        return self.sync_config.sync_jobs

    @cached_property
    def sync_config(self) -> AppConfig:
        # Check for YAML config file (env var override or default path)
        config_path = os.environ.get("CONFIG_FILE", self.config_file)
        if Path(config_path).is_file():
            logger.info("Loading config from %s", config_path)
            return load_config(config_path)

        # Fallback: build jobs from env vars (backward compat)
        logger.info("No config.yaml found, using environment variables")
        return AppConfig(sync_mode=SYNC_MODE_PATH_PREFIX, sync_jobs=self._sync_jobs_from_env())

    def _sync_jobs_from_env(self) -> list[SyncJob]:
        album_id = self.target_album_uid
        jobs = []
        if self.shared_path_prefix:
            jobs.append(SyncJob(
                name="external-library",
                source_user_id=self.source_uid,
                target_user_id=self.target_uid,
                target_library_id=self.target_lid,
                source_path_prefix=self.shared_path_prefix,
                target_path_prefix=self.target_path_prefix,
                album_id=album_id,
            ))
        if self.upload_source_user_id:
            jobs.append(SyncJob(
                name="internal-library",
                source_user_id=self.upload_source_uid,
                target_user_id=self.upload_target_uid,
                target_library_id=self.upload_target_lid,
                source_path_prefix=self.upload_path_prefix,
                target_path_prefix=self.target_upload_path_prefix,
                album_id=album_id,
            ))
        return jobs


settings = Settings()
