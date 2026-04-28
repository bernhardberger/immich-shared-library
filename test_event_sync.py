from __future__ import annotations

import unittest
from uuid import UUID

EVENT_IMPORT_ERROR = None

try:
    from src import event_sync
except ModuleNotFoundError as exc:
    if exc.name == "asyncpg":
        EVENT_IMPORT_ERROR = exc
    else:
        raise


SOURCE_ASSET = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TARGET_ASSET_1 = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
TARGET_ASSET_2 = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
UNMAPPED_ASSET = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


class FakeConnection:
    def __init__(self, claimed_rows=None, mapped_sources=None):
        self.claimed_rows = claimed_rows or []
        self.mapped_sources = mapped_sources or []
        self.fetch_calls = []
        self.execute_calls = []
        self.transaction_entries = 0
        self.failed_transaction = False

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        if "UPDATE _face_sync_event_queue" in sql:
            return self.claimed_rows
        if "FROM _face_sync_album_map" in sql:
            return [{"source_asset_id": source_id} for source_id in self.mapped_sources]
        return []

    async def execute(self, sql, *args):
        if self.failed_transaction and "status = 'error'" in sql:
            raise RuntimeError("current transaction is aborted")
        self.execute_calls.append((sql, args))
        return "UPDATE 1"

    async def fetchval(self, sql, *args):
        self.execute_calls.append((sql, args))
        return True

    def transaction(self):
        return FakeTransaction(self, [])


class FakeTransaction:
    def __init__(self, conn, entered):
        self.conn = conn
        self.entered = entered

    async def __aenter__(self):
        if hasattr(self.conn, "transaction_entries"):
            self.conn.transaction_entries += 1
        self.entered.append(self.conn)
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None and hasattr(self.conn, "failed_transaction"):
            self.conn.failed_transaction = False
        return False


@unittest.skipIf(
    EVENT_IMPORT_ERROR is not None,
    f"missing dependency: {EVENT_IMPORT_ERROR.name if EVENT_IMPORT_ERROR else ''}",
)
class EventSyncSqlTest(unittest.TestCase):
    def test_event_queue_ddl_contains_required_table_function_trigger_and_suppression_check(self) -> None:
        ddl = event_sync.EVENT_QUEUE_DDL + event_sync.METADATA_TRIGGER_DDL

        self.assertIn("CREATE TABLE IF NOT EXISTS _face_sync_event_queue", ddl)
        self.assertIn("id bigserial PRIMARY KEY", ddl)
        self.assertIn("event_type text NOT NULL", ddl)
        self.assertIn("status text NOT NULL DEFAULT 'pending'", ddl)
        self.assertIn("attempts integer NOT NULL DEFAULT 0", ddl)
        self.assertIn("idx_face_sync_event_queue_pending", ddl)
        self.assertIn("idx_face_sync_event_queue_coalesce", ddl)
        self.assertIn("CREATE OR REPLACE FUNCTION immich_shared_sidecar_enqueue_asset_exif_event", ddl)
        self.assertIn("CREATE TRIGGER immich_shared_sidecar_asset_exif_metadata", ddl)
        self.assertIn("current_setting('immich_shared_sidecar.suppress_events', true) = 'on'", ddl)

    def test_metadata_trigger_watches_only_first_slice_columns_and_notifies(self) -> None:
        ddl = event_sync.METADATA_TRIGGER_DDL

        self.assertIn('UPDATE OF description, "dateTimeOriginal", "timeZone", latitude, longitude', ddl)
        self.assertIn("pg_notify('face_sync_events'", ddl)
        self.assertNotIn("asset_face", ddl)
        self.assertNotIn("personId", ddl)
        self.assertNotIn("album_asset", ddl)
        self.assertNotIn("album_user", ddl)


@unittest.skipIf(
    EVENT_IMPORT_ERROR is not None,
    f"missing dependency: {EVENT_IMPORT_ERROR.name if EVENT_IMPORT_ERROR else ''}",
)
class EventBatchProcessorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_reconcile = event_sync.reconcile_shared_album_metadata
        self.original_logger_disabled = event_sync.logger.disabled
        event_sync.logger.disabled = True
        self.reconcile_calls = []

        async def fake_reconcile(conn, api, *, source_asset_ids=None):
            self.reconcile_calls.append(set(source_asset_ids or []))
            return {"metadata_assets_updated": len(source_asset_ids or [])}

        event_sync.reconcile_shared_album_metadata = fake_reconcile

    async def asyncTearDown(self):
        event_sync.reconcile_shared_album_metadata = self.original_reconcile
        event_sync.logger.disabled = self.original_logger_disabled

    async def test_event_batch_coalesces_events_marks_rows_done_and_reconciles_logical_sources(self) -> None:
        conn = FakeConnection(
            claimed_rows=[
                {"id": 1, "entity_id": SOURCE_ASSET, "source_asset_id": None},
                {"id": 2, "entity_id": TARGET_ASSET_1, "source_asset_id": None},
                {"id": 3, "entity_id": TARGET_ASSET_2, "source_asset_id": SOURCE_ASSET},
                {"id": 4, "entity_id": UNMAPPED_ASSET, "source_asset_id": None},
            ],
            mapped_sources=[SOURCE_ASSET, SOURCE_ASSET],
        )

        stats = await event_sync.process_pending_metadata_events(conn, api=object(), batch_size=10)

        self.assertEqual(stats["events_claimed"], 4)
        self.assertEqual(stats["logical_sources_reconciled"], 1)
        self.assertEqual(self.reconcile_calls, [{SOURCE_ASSET}])
        done_calls = [call for call in conn.execute_calls if "status = 'done'" in call[0]]
        self.assertEqual(len(done_calls), 1)
        self.assertEqual(set(done_calls[0][1][0]), {1, 2, 3, 4})

    async def test_event_errors_are_marked_error_after_attempt_increment_on_claim(self) -> None:
        async def failing_reconcile(conn, api, *, source_asset_ids=None):
            raise RuntimeError("boom")

        event_sync.reconcile_shared_album_metadata = failing_reconcile
        conn = FakeConnection(
            claimed_rows=[{"id": 1, "entity_id": TARGET_ASSET_1, "source_asset_id": None}],
            mapped_sources=[SOURCE_ASSET],
        )

        stats = await event_sync.process_pending_metadata_events(conn, api=object(), batch_size=10)

        self.assertEqual(stats["events_claimed"], 1)
        self.assertEqual(stats["events_error"], 1)
        claim_sql = conn.fetch_calls[0][0]
        self.assertIn("attempts = attempts + 1", claim_sql)
        error_calls = [call for call in conn.execute_calls if "status = 'error'" in call[0]]
        self.assertEqual(len(error_calls), 1)
        self.assertEqual(error_calls[0][1][0], [1])
        self.assertIn("boom", error_calls[0][1][1])

    async def test_event_errors_are_marked_after_reconciliation_aborts_nested_transaction(self) -> None:
        async def failing_reconcile(conn, api, *, source_asset_ids=None):
            conn.failed_transaction = True
            raise RuntimeError("simulated aborted transaction")

        event_sync.reconcile_shared_album_metadata = failing_reconcile
        conn = FakeConnection(
            claimed_rows=[{"id": 1, "entity_id": TARGET_ASSET_1, "source_asset_id": None}],
            mapped_sources=[SOURCE_ASSET],
        )

        stats = await event_sync.process_pending_metadata_events(conn, api=object(), batch_size=10)

        self.assertEqual(stats["events_claimed"], 1)
        self.assertEqual(stats["events_error"], 1)
        self.assertEqual(conn.transaction_entries, 1)
        error_calls = [call for call in conn.execute_calls if "status = 'error'" in call[0]]
        self.assertEqual(len(error_calls), 1)
        self.assertEqual(error_calls[0][1][0], [1])


@unittest.skipIf(
    EVENT_IMPORT_ERROR is not None,
    f"missing dependency: {EVENT_IMPORT_ERROR.name if EVENT_IMPORT_ERROR else ''}",
)
class EventDaemonTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_transaction = event_sync.transaction
        self.original_reconcile = event_sync.reconcile_shared_album_metadata
        self.original_sleep = event_sync.asyncio.sleep
        self.original_install_metadata_trigger = event_sync.install_metadata_trigger
        self.original_install_event_queue = event_sync.install_event_queue
        self.original_logger_disabled = event_sync.logger.disabled
        event_sync.logger.disabled = True
        self.entered_transactions = []
        self.reconcile_calls = []
        self.installs = []

        async def fake_reconcile(conn, api, *, source_asset_ids=None):
            self.reconcile_calls.append(source_asset_ids)
            return {"metadata_assets_updated": len(source_asset_ids or [])}

        async def fake_sleep(_seconds):
            return None

        async def fake_install_metadata_trigger(conn):
            self.installs.append("metadata_trigger")

        async def fake_install_event_queue(conn):
            self.installs.append("event_queue")

        event_sync.reconcile_shared_album_metadata = fake_reconcile
        event_sync.asyncio.sleep = fake_sleep
        event_sync.install_metadata_trigger = fake_install_metadata_trigger
        event_sync.install_event_queue = fake_install_event_queue

    async def asyncTearDown(self):
        event_sync.transaction = self.original_transaction
        event_sync.reconcile_shared_album_metadata = self.original_reconcile
        event_sync.asyncio.sleep = self.original_sleep
        event_sync.install_metadata_trigger = self.original_install_metadata_trigger
        event_sync.install_event_queue = self.original_install_event_queue
        event_sync.logger.disabled = self.original_logger_disabled

    async def test_daemon_loop_processes_event_batches_with_transaction_backed_connections(self) -> None:
        conn = FakeConnection(
            claimed_rows=[{"id": 1, "entity_id": TARGET_ASSET_1, "source_asset_id": None}],
            mapped_sources=[SOURCE_ASSET],
        )

        def fake_transaction():
            return FakeTransaction(conn, self.entered_transactions)

        event_sync.transaction = fake_transaction

        await event_sync.run_event_daemon(
            api=object(),
            poll_interval_seconds=0,
            debounce_seconds=0,
            batch_size=10,
            full_reconcile_interval_seconds=3600,
            stop_after_iterations=1,
        )

        self.assertEqual(self.entered_transactions, [conn])
        self.assertTrue(any("pg_try_advisory_xact_lock" in call[0] for call in conn.execute_calls))
        self.assertEqual(self.reconcile_calls, [{SOURCE_ASSET}])
        self.assertEqual(self.installs, [])

    async def test_daemon_loop_invokes_periodic_full_reconciliation_when_interval_elapses(self) -> None:
        conn = FakeConnection()

        def fake_transaction():
            return FakeTransaction(conn, self.entered_transactions)

        event_sync.transaction = fake_transaction

        await event_sync.run_event_daemon(
            api=object(),
            poll_interval_seconds=0,
            debounce_seconds=0,
            batch_size=10,
            full_reconcile_interval_seconds=0,
            stop_after_iterations=1,
        )

        self.assertIn(None, self.reconcile_calls)
        self.assertEqual(self.installs, [])

    def test_module_entrypoint_helper_imports_without_side_effects(self) -> None:
        self.assertTrue(callable(event_sync.main))
        self.assertEqual(self.installs, [])


if __name__ == "__main__":
    unittest.main()
