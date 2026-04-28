"""Pure shared-album discovery helpers.

This module only computes candidate mirror edges. It does not create assets,
write tracking rows, or call Immich.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Iterable, Mapping
from uuid import UUID


@dataclass(frozen=True, order=True)
class SharedAlbumEdge:
    album_id: UUID
    asset_id: UUID
    source_user_id: UUID
    target_user_id: UUID


def discover_shared_album_edges(
    *,
    albums: Iterable[Mapping[str, Any]],
    album_users: Iterable[Mapping[str, Any]],
    album_assets: Iterable[Mapping[str, Any]],
    assets: Iterable[Mapping[str, Any]],
    users: Iterable[Mapping[str, Any]] = (),
    exclude_users: Iterable[UUID | str] = (),
    exclude_name_patterns: Iterable[str] = (),
) -> list[SharedAlbumEdge]:
    """Transform already-fetched Immich rows into candidate mirror edges.

    Participants are the album owner plus authenticated ``album_user`` rows.
    Public-link rows are intentionally not accepted as input, so they cannot
    influence participant discovery.
    """
    excluded_user_ids = {_to_uuid(user_id) for user_id in exclude_users}
    eligible_users = _eligible_user_ids(users, excluded_user_ids)
    album_participants = _album_participants(album_users, eligible_users, excluded_user_ids)
    assets_by_id = {_to_uuid(row["id"]): row for row in assets}
    assets_for_album = _album_assets(album_assets)
    patterns = tuple(exclude_name_patterns)

    edges: set[SharedAlbumEdge] = set()
    for album in albums:
        if album.get("deletedAt") is not None:
            continue
        if _album_name_excluded(album, patterns):
            continue

        album_id = _to_uuid(album["id"])
        owner_id = _to_uuid(album["ownerId"])
        if not _user_is_eligible(owner_id, eligible_users, excluded_user_ids):
            continue

        authenticated_participants = album_participants.get(album_id, set())
        if not authenticated_participants:
            continue

        participants = {owner_id, *authenticated_participants}
        for asset_id in assets_for_album.get(album_id, set()):
            asset = assets_by_id.get(asset_id)
            if asset is None or asset.get("deletedAt") is not None:
                continue

            source_user_id = _to_uuid(asset["ownerId"])
            if source_user_id not in participants:
                continue
            if not _user_is_eligible(source_user_id, eligible_users, excluded_user_ids):
                continue

            for target_user_id in participants:
                if target_user_id == source_user_id:
                    continue
                if not _user_is_eligible(target_user_id, eligible_users, excluded_user_ids):
                    continue
                edges.add(SharedAlbumEdge(
                    album_id=album_id,
                    asset_id=asset_id,
                    source_user_id=source_user_id,
                    target_user_id=target_user_id,
                ))

    return sorted(edges)


def _eligible_user_ids(
    users: Iterable[Mapping[str, Any]],
    excluded_user_ids: set[UUID],
) -> set[UUID] | None:
    rows = list(users)
    if not rows:
        return None

    eligible: set[UUID] = set()
    for row in rows:
        user_id = _to_uuid(row["id"])
        if user_id in excluded_user_ids:
            continue
        if row.get("deletedAt") is not None:
            continue
        if "status" in row and row.get("status") != "active":
            continue
        eligible.add(user_id)
    return eligible


def _album_participants(
    album_users: Iterable[Mapping[str, Any]],
    eligible_users: set[UUID] | None,
    excluded_user_ids: set[UUID],
) -> dict[UUID, set[UUID]]:
    participants: dict[UUID, set[UUID]] = {}
    for row in album_users:
        role = row.get("role")
        if role is not None and role not in {"editor", "viewer"}:
            continue
        user_id = _to_uuid(row["userId"])
        if not _user_is_eligible(user_id, eligible_users, excluded_user_ids):
            continue
        participants.setdefault(_to_uuid(row["albumId"]), set()).add(user_id)
    return participants


def _album_assets(album_assets: Iterable[Mapping[str, Any]]) -> dict[UUID, set[UUID]]:
    assets_for_album: dict[UUID, set[UUID]] = {}
    for row in album_assets:
        assets_for_album.setdefault(_to_uuid(row["albumId"]), set()).add(_to_uuid(row["assetId"]))
    return assets_for_album


def _album_name_excluded(album: Mapping[str, Any], patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    name = str(album.get("albumName") or album.get("name") or "").casefold()
    return any(fnmatchcase(name, pattern.casefold()) for pattern in patterns)


def _user_is_eligible(
    user_id: UUID,
    eligible_users: set[UUID] | None,
    excluded_user_ids: set[UUID],
) -> bool:
    if user_id in excluded_user_ids:
        return False
    return eligible_users is None or user_id in eligible_users


def _to_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
