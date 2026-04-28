# Spec: Shared Albums Sync Mode

## Objective

Add an upstream-friendly `shared_albums` sync mode where Immich shared albums are the selection UI for mirrored assets. A household user creates an Immich album such as "Ostern 2025", shares it with other authenticated local users, and each eligible participant receives mirrored assets in their own indexed, searchable, face-enabled library without adding per-album sidecar jobs.

Success means the existing path-prefix configuration keeps working unchanged, while the new mode automatically discovers shared albums, mirrors new album assets to other participants, and cleans up mirror records when album membership or source assets change.

## Non-goals

- Do not replace Immich's album UI or implement a separate selection interface.
- Do not mirror public-link-only or anonymous/external access in v1.
- Do not require hard-coded pairwise user jobs such as `users: [alice, bob]`.
- Do not add a configurable `min_participants` unless schema discovery shows it is needed for safety.
- Do not propagate mirrored-user metadata edits back to the source owner in v1.
- Do not change the existing external-library/upload path-prefix sync behavior except where shared infrastructure is generalized.

## Proposed config contract

The current `sync_jobs` YAML and env-var fallback remain the compatibility path. New deployments can opt into shared-album discovery with a top-level mode:

```yaml
sync_mode: shared_albums

scope:
  mode: all_local_users
  exclude_users: []

albums:
  include: all_shared_albums
  exclude_name_patterns: []
```

Compatibility rules:

- If `sync_mode` is omitted and `sync_jobs` exists, behave exactly as today (`path_prefix` mode).
- If no `config.yaml` exists, keep using legacy env vars exactly as today.
- `sync_mode: shared_albums` and `sync_jobs` should be mutually exclusive in v1 to avoid unclear cleanup ownership.
- `scope.mode: all_local_users` means every non-deleted local Immich user can be a source or target unless excluded.
- `albums.include: all_shared_albums` means every eligible album shared with at least one other authenticated local participant is considered.
- `albums.exclude_name_patterns` are optional case-insensitive patterns for operator-side safety excludes.

Open config item for implementation: the current sync engine needs a target external library and target path prefix per target/source pair. Shared-albums mode should avoid per-album config, but it still needs a deterministic shadow-library/path strategy. Prefer auto-derived per-target libraries/paths if Immich's API/schema allows safe creation; otherwise add one global `shadow_library` section rather than per-album or per-user lists.

## Existing architecture to preserve

- `config.py` loads explicit `SyncJob` entries from YAML or legacy env vars.
- `sync_engine.py` runs new assets, album assignment, incremental faces, person metadata, and cleanup.
- `asset_sync.py` mirrors one fully processed source asset into one target user/library using path remapping.
- `person_sync.py` keeps source person metadata authoritative for mirrored people.
- `_face_sync_asset_map`, `_face_sync_person_map`, and `_face_sync_skipped` provide idempotency and cleanup state.
- `schema.py` validates Immich tables/columns before writes.

Shared-albums mode should reuse the low-level asset/person/ML copy routines where possible, but replace static job enumeration with discovered source-album participant edges.

## Data model and tracking changes

Add tracking that records why a target asset exists, not just source/target asset IDs:

- `_face_sync_album_map`
  - `source_album_id`
  - `source_asset_id`
  - `source_user_id`
  - `target_user_id`
  - `target_asset_id`
  - `target_album_id` or nullable if v1 only mirrors into library/timeline
  - `last_seen_at`
  - unique key on `(source_album_id, source_asset_id, target_user_id)`

Keep `_face_sync_asset_map` as the lower-level asset mirror map so existing face/person cleanup can stay shared. If one target asset can be justified by multiple shared albums, either:

1. allow multiple `_face_sync_album_map` rows pointing at one `_face_sync_asset_map` row, and only delete the target asset when no active album rows remain; or
2. create one target mirrored asset per source-album/target-user edge.

Prefer option 1 to avoid duplicate target assets when the same source photo appears in multiple shared albums.

Schema discovery must validate the Immich v2.7.x album membership tables before implementation. The current repo validates `album` and `album_asset`, but not the album shared-user association or public-link tables. The implementation task should first inspect `information_schema` in a test Immich DB and encode the exact required table/column names in `schema.py`.

## Discovery and eligibility rules

On each sync cycle, discover eligible album edges:

1. Load non-deleted albums whose owner is in scope.
2. Load authenticated local shared users/participants for each album.
3. Exclude public-link-only access and any anonymous/external share mechanism by default.
4. Treat an album as eligible when it has an owner plus at least one authenticated participant.
5. For each album asset owned by a participant or owner, mirror it to every other eligible participant who does not already own that source asset.
6. Never mirror an asset to its current owner.
7. Ignore deleted/trashed/offline source assets and assets without completed metadata, smart search, and face processing, matching the current path-prefix safety gate.

The source of truth for selection is current album membership plus current album assets. The sidecar should not require a config reload when albums, assets, or participants change.

## Sync algorithm

Shared-albums mode should add a discovered-job layer before the existing sync phases:

1. **Discover albums and participants**
   - Query eligible shared albums and participants.
   - Apply user and album excludes.
   - Produce candidate mirror edges: `(source_album_id, source_asset_id, source_user_id, target_user_id)`.

2. **Resolve target location**
   - Find or create the target user's shadow external library/path for the source user's assets.
   - Build an internal `SyncJob`-like object for each source/target/path combination, not for each album.
   - Reuse path remapping when a shadow path exists; otherwise fail closed with a clear validation error.

3. **Mirror new assets**
   - For each candidate edge not already tracked, call the existing asset/ML/person sync flow.
   - If the source asset is already mirrored for the same target user due to another album, reuse the existing target asset and add only the album-map row.
   - Use savepoints per source asset as today.

4. **Maintain optional target albums**
   - If v1 creates participant-owned shadow albums, keep each target album membership aligned with the source shared album.
   - If v1 only mirrors into each participant's library/timeline, skip target album creation and record that as an explicit limitation in README.

5. **Incremental ML/person sync**
   - Reuse current face incremental sync and source-authoritative person metadata sync.

6. **Cleanup**
   - Remove album-map rows when source album assets are removed, participants are removed, albums are deleted, or users are excluded.
   - Delete the target asset only when no remaining album-map row or path-prefix map still justifies it.
   - Remove target album entries before deleting target assets.
   - Remove target assets when source assets are deleted/trashed, matching current cleanup behavior.
   - Backfill a newly added participant by treating all existing album assets as new candidate edges.

## Safety rules

- Back up the Immich database before enabling this mode; direct DB writes remain the core risk.
- Fail closed if album participant schema, target library ownership, or shadow path setup cannot be validated.
- Exclude public-link/anonymous shares by default.
- Scope to non-deleted local users; excluded users are neither sources nor targets.
- Do not delete source originals or source album rows.
- Delete only target assets that are tracked by this sidecar and have no remaining tracked justification.
- Preserve current duplicate detection against a target user's own uploads where applicable.
- Preserve the warning that forced Immich ML jobs may overwrite mirrored ML rows temporarily.

## Face/person metadata conflict policy

Default v1 policy: source owner metadata is authoritative for mirrored people and faces. The sidecar may copy source person names, visibility, thumbnails, face assignments, and merge changes to mirrored users. Mirrored users can merge or rename locally in Immich, but those local changes are not propagated back to the source owner and may be overwritten by later source-owner sync.

This matches the current README's one-way model. A future version could add per-target local overrides, but that requires a separate conflict model and is out of scope for v1.

## Migration and compatibility

- Existing `sync_jobs` and legacy env vars keep their current behavior and table semantics.
- Shared-albums mode should bump the sidecar tracking schema version and add migrations for new tracking tables only.
- Existing `_face_sync_asset_map` rows from path-prefix mode must not be interpreted as shared-album ownership unless linked by the new album-map table.
- A deployment should be able to switch from path-prefix mode to shared-albums mode only after operator review, because cleanup ownership differs.
- README/config examples should document both modes separately.

## Testing strategy

There are no automated tests today; `test_sync.py` is a manual integration test. Shared-albums implementation should add automated tests where feasible around pure logic and SQL construction, plus manual integration coverage:

- Config parsing rejects invalid combinations and preserves legacy behavior.
- Discovery excludes public-link-only albums and deleted users/assets.
- Discovery includes owner-plus-authenticated-participant albums without per-album config.
- New asset in a shared album mirrors to other participants.
- Same source asset in two shared albums creates one target asset and two justification rows.
- Removing an asset from one album does not delete the target if another album still justifies it.
- Removing the final justification deletes the target asset and album membership.
- Adding a participant backfills existing album assets.
- Removing a participant cleans up only that participant's mirrored targets.
- Deleting/trashing the source asset cleans up all mirrored targets.
- Source person rename/merge updates mirrored users; target local edits do not propagate back.

Cheap checks before opening an implementation PR:

```bash
python3 -m py_compile src/*.py test_sync.py
git diff --check
```

Manual integration checks should continue to use `./run-utility.sh test_sync.py` against a disposable Immich instance, never a live household instance.

## Staged implementation tasks

### Phase 1: Schema discovery and config

- Add `sync_mode` parsing while preserving existing YAML/env behavior.
- Add validation for mutually exclusive path-prefix and shared-albums modes.
- Inspect a test Immich v2.7.x database and encode required album participant/public-link schema checks.

### Phase 2: Discovery model

- Add a shared-album discovery module that returns candidate mirror edges.
- Unit-test filtering for users, album names, deleted records, and authenticated participants.
- Decide and document the target shadow library/path strategy.

### Phase 3: Tracking and idempotency

- Add migrations for `_face_sync_album_map` and any needed indexes.
- Reuse existing target assets when multiple albums justify the same source/target pair.
- Keep path-prefix tracking independent.

### Phase 4: Sync integration

- Convert candidate edges into internal sync operations that reuse `sync_asset`, face sync, and person sync.
- Add backfill for new participants and new albums.
- Add target album alignment only if v1 chooses to mirror album structure, otherwise document library-only behavior.

### Phase 5: Cleanup

- Remove stale album-map rows for removed assets, removed participants, deleted albums, and source deletion.
- Delete target assets only after checking there are no remaining justifications.
- Extend utility/reset scripts to understand shared-albums tracking tables.

### Phase 6: Docs and PR hardening

- Update README and `config.yaml.example` with both modes.
- Add migration notes and caveats.
- Run syntax checks, `git diff --check`, and a disposable Immich integration test before proposing upstream.

## Open questions

- What is the exact Immich v2.7.x schema for authenticated album participants and public-link associations? This must be confirmed before code.
- Can the sidecar safely create/manage per-user shadow external libraries via Immich's API, or must operators create one global shadow library structure manually?
- Should v1 create participant-owned target albums mirroring source album names, or is library/timeline/search visibility sufficient for the first upstreamable version?
- How should duplicate detection behave when an album contains an asset the target already has as their own upload and also in multiple shared albums?
- Should target local person renames be overwritten immediately, or only when the source owner's metadata changes?
