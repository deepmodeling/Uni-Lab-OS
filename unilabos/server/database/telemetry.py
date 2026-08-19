"""设备遥测投影与追加流的 ``telemetry.db`` v1 schema。

本库只保存设备属性、连接和告警遥测。action 可用性、endpoint 执行状态与
job 生命周期属于 ``runtime.db``，不得在这里形成第二套执行权威。

完整/增量状态上报必须在同一个 SQLite 事务中写入 ingest batch、report、
sample 和 latest。这样读者只能看到整批写入前或写入后的状态；full report
中不再出现的 latest 属性，也由同一事务删除。跨库的 endpoint/job/payload
只保留规范 UUID，不建立 SQLite 外键。
"""

from unilabos.server.database.schema import (
    SCHEMA_MIGRATION_TABLE,
    DatabaseSpec,
    TableSpec,
)


TELEMETRY_TABLES = (
    SCHEMA_MIGRATION_TABLE,
    TableSpec(
        "telemetry_source_cursor",
        """
        CREATE TABLE IF NOT EXISTS telemetry_source_cursor (
            endpoint_uuid TEXT PRIMARY KEY CHECK (TRIM(endpoint_uuid) <> ''),
            transport TEXT NOT NULL CHECK (transport IN ('hostlink','ros2')),
            adapter_epoch TEXT NOT NULL CHECK (TRIM(adapter_epoch) <> ''),
            epoch_generation INTEGER NOT NULL CHECK (epoch_generation >= 0),
            last_adapter_sequence INTEGER NOT NULL
                CHECK (last_adapter_sequence >= 0),
            last_batch_uuid TEXT NOT NULL CHECK (TRIM(last_batch_uuid) <> ''),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_telemetry_source_cursor_updated
            ON telemetry_source_cursor(transport, updated_at_ms DESC)
            """,
        ),
    ),
    TableSpec(
        "telemetry_ingest_batch",
        """
        CREATE TABLE IF NOT EXISTS telemetry_ingest_batch (
            batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(batch_uuid) <> ''),
            source_event_uuid TEXT NOT NULL UNIQUE
                CHECK (TRIM(source_event_uuid) <> ''),
            endpoint_uuid TEXT NOT NULL CHECK (TRIM(endpoint_uuid) <> ''),
            transport TEXT NOT NULL CHECK (transport IN ('hostlink','ros2')),
            adapter_epoch TEXT NOT NULL CHECK (TRIM(adapter_epoch) <> ''),
            epoch_generation INTEGER NOT NULL CHECK (epoch_generation >= 0),
            adapter_sequence INTEGER NOT NULL CHECK (adapter_sequence >= 0),
            status TEXT NOT NULL CHECK (status IN ('committed','rejected')),
            item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
            payload_uuid TEXT,
            payload_hash TEXT NOT NULL CHECK (TRIM(payload_hash) <> ''),
            rejection_reason TEXT,
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            UNIQUE(endpoint_uuid, adapter_epoch, adapter_sequence),
            CHECK (
                (status = 'committed' AND rejection_reason IS NULL)
                OR (
                    status = 'rejected'
                    AND rejection_reason IS NOT NULL
                    AND TRIM(rejection_reason) <> ''
                )
            )
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_telemetry_ingest_batch_retention
            ON telemetry_ingest_batch(received_at_ms, batch_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_telemetry_ingest_batch_endpoint
            ON telemetry_ingest_batch(endpoint_uuid, batch_id DESC)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_telemetry_ingest_batch_monotonic
            BEFORE INSERT ON telemetry_ingest_batch
            WHEN EXISTS (
                SELECT 1
                FROM telemetry_source_cursor AS cursor
                WHERE cursor.endpoint_uuid = NEW.endpoint_uuid
                  AND (
                    NEW.transport <> cursor.transport
                    OR NEW.epoch_generation < cursor.epoch_generation
                    OR (
                        NEW.epoch_generation = cursor.epoch_generation
                        AND (
                            NEW.adapter_epoch <> cursor.adapter_epoch
                            OR NEW.adapter_sequence <= cursor.last_adapter_sequence
                        )
                    )
                    OR (
                        NEW.epoch_generation > cursor.epoch_generation
                        AND NEW.adapter_epoch = cursor.adapter_epoch
                    )
                  )
            )
            BEGIN
                SELECT RAISE(ABORT, 'stale telemetry ingest batch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_telemetry_source_cursor_insert
            BEFORE INSERT ON telemetry_source_cursor
            WHEN NOT EXISTS (
                SELECT 1
                FROM telemetry_ingest_batch AS batch
                WHERE batch.batch_uuid = NEW.last_batch_uuid
                  AND batch.endpoint_uuid = NEW.endpoint_uuid
                  AND batch.transport = NEW.transport
                  AND batch.adapter_epoch = NEW.adapter_epoch
                  AND batch.epoch_generation = NEW.epoch_generation
                  AND batch.adapter_sequence = NEW.last_adapter_sequence
            )
            BEGIN
                SELECT RAISE(ABORT, 'telemetry cursor must reference its batch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_telemetry_source_cursor_update
            BEFORE UPDATE ON telemetry_source_cursor
            WHEN NEW.transport <> OLD.transport
              OR NEW.epoch_generation < OLD.epoch_generation
              OR (
                NEW.epoch_generation = OLD.epoch_generation
                AND (
                    NEW.adapter_epoch <> OLD.adapter_epoch
                    OR NEW.last_adapter_sequence <= OLD.last_adapter_sequence
                )
              )
              OR (
                NEW.epoch_generation > OLD.epoch_generation
                AND NEW.adapter_epoch = OLD.adapter_epoch
              )
              OR NEW.updated_at_ms < OLD.updated_at_ms
              OR NEW.version <= OLD.version
              OR NOT EXISTS (
                SELECT 1
                FROM telemetry_ingest_batch AS batch
                WHERE batch.batch_uuid = NEW.last_batch_uuid
                  AND batch.endpoint_uuid = NEW.endpoint_uuid
                  AND batch.transport = NEW.transport
                  AND batch.adapter_epoch = NEW.adapter_epoch
                  AND batch.epoch_generation = NEW.epoch_generation
                  AND batch.adapter_sequence = NEW.last_adapter_sequence
              )
            BEGIN
                SELECT RAISE(ABORT, 'telemetry cursor cannot move backwards');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_telemetry_ingest_batch_immutable
            BEFORE UPDATE ON telemetry_ingest_batch
            BEGIN
                SELECT RAISE(ABORT, 'telemetry ingest batch is immutable');
            END
            """,
        ),
    ),
    TableSpec(
        "device_state_report",
        """
        CREATE TABLE IF NOT EXISTS device_state_report (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(report_uuid) <> ''),
            batch_uuid TEXT NOT NULL,
            item_index INTEGER NOT NULL CHECK (item_index >= 0),
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            source_job_uuid TEXT,
            report_mode TEXT NOT NULL CHECK (report_mode IN ('full','delta')),
            source_state_version INTEGER CHECK (source_state_version >= 0),
            property_count INTEGER NOT NULL CHECK (property_count >= 0),
            state_hash TEXT NOT NULL CHECK (TRIM(state_hash) <> ''),
            payload_uuid TEXT,
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            UNIQUE(batch_uuid, item_index),
            UNIQUE(report_uuid, device_uuid),
            FOREIGN KEY(batch_uuid) REFERENCES telemetry_ingest_batch(batch_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_state_report_timeline
            ON device_state_report(device_uuid, observed_at_ms DESC, report_id DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_state_report_job
            ON device_state_report(source_job_uuid, observed_at_ms, report_id)
            WHERE source_job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_state_report_retention
            ON device_state_report(received_at_ms, report_id)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_state_report_committed_batch
            BEFORE INSERT ON device_state_report
            WHEN NOT EXISTS (
                SELECT 1
                FROM telemetry_ingest_batch AS batch
                WHERE batch.batch_uuid = NEW.batch_uuid
                  AND batch.status = 'committed'
            )
            BEGIN
                SELECT RAISE(ABORT, 'state report requires committed ingest batch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_state_report_immutable
            BEFORE UPDATE ON device_state_report
            BEGIN
                SELECT RAISE(ABORT, 'device state report is immutable');
            END
            """,
        ),
    ),
    TableSpec(
        "device_property_latest",
        """
        CREATE TABLE IF NOT EXISTS device_property_latest (
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            property_key TEXT NOT NULL CHECK (TRIM(property_key) <> ''),
            value_type TEXT NOT NULL CHECK (TRIM(value_type) <> ''),
            value_json TEXT NOT NULL CHECK (json_valid(value_json)),
            value_hash TEXT NOT NULL CHECK (TRIM(value_hash) <> ''),
            quality TEXT NOT NULL DEFAULT 'good'
                CHECK (quality IN ('good','uncertain','bad')),
            report_uuid TEXT NOT NULL CHECK (TRIM(report_uuid) <> ''),
            source_endpoint_uuid TEXT NOT NULL
                CHECK (TRIM(source_endpoint_uuid) <> ''),
            source_transport TEXT NOT NULL
                CHECK (source_transport IN ('hostlink','ros2')),
            source_job_uuid TEXT,
            adapter_epoch TEXT NOT NULL CHECK (TRIM(adapter_epoch) <> ''),
            epoch_generation INTEGER NOT NULL CHECK (epoch_generation >= 0),
            adapter_sequence INTEGER NOT NULL CHECK (adapter_sequence >= 0),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            PRIMARY KEY(device_uuid, property_key)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_property_latest_job
            ON device_property_latest(source_job_uuid, device_uuid)
            WHERE source_job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_property_latest_source
            ON device_property_latest(source_endpoint_uuid, device_uuid)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_property_latest_insert_source
            BEFORE INSERT ON device_property_latest
            WHEN NOT EXISTS (
                SELECT 1
                FROM device_state_report AS report
                JOIN telemetry_ingest_batch AS batch
                  ON batch.batch_uuid = report.batch_uuid
                JOIN device_property_sample AS sample
                  ON sample.report_uuid = report.report_uuid
                 AND sample.device_uuid = report.device_uuid
                WHERE report.report_uuid = NEW.report_uuid
                  AND report.device_uuid = NEW.device_uuid
                  AND report.source_job_uuid IS NEW.source_job_uuid
                  AND report.observed_at_ms = NEW.observed_at_ms
                  AND report.received_at_ms = NEW.received_at_ms
                  AND batch.endpoint_uuid = NEW.source_endpoint_uuid
                  AND batch.transport = NEW.source_transport
                  AND batch.adapter_epoch = NEW.adapter_epoch
                  AND batch.epoch_generation = NEW.epoch_generation
                  AND batch.adapter_sequence = NEW.adapter_sequence
                  AND sample.property_key = NEW.property_key
                  AND sample.value_type = NEW.value_type
                  AND sample.value_json = NEW.value_json
                  AND sample.value_hash = NEW.value_hash
                  AND sample.quality = NEW.quality
            )
            BEGIN
                SELECT RAISE(ABORT, 'latest property must match report sample');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_property_latest_update_source
            BEFORE UPDATE ON device_property_latest
            WHEN NOT EXISTS (
                SELECT 1
                FROM device_state_report AS report
                JOIN telemetry_ingest_batch AS batch
                  ON batch.batch_uuid = report.batch_uuid
                JOIN device_property_sample AS sample
                  ON sample.report_uuid = report.report_uuid
                 AND sample.device_uuid = report.device_uuid
                WHERE report.report_uuid = NEW.report_uuid
                  AND report.device_uuid = NEW.device_uuid
                  AND report.source_job_uuid IS NEW.source_job_uuid
                  AND report.observed_at_ms = NEW.observed_at_ms
                  AND report.received_at_ms = NEW.received_at_ms
                  AND batch.endpoint_uuid = NEW.source_endpoint_uuid
                  AND batch.transport = NEW.source_transport
                  AND batch.adapter_epoch = NEW.adapter_epoch
                  AND batch.epoch_generation = NEW.epoch_generation
                  AND batch.adapter_sequence = NEW.adapter_sequence
                  AND sample.property_key = NEW.property_key
                  AND sample.value_type = NEW.value_type
                  AND sample.value_json = NEW.value_json
                  AND sample.value_hash = NEW.value_hash
                  AND sample.quality = NEW.quality
            )
            BEGIN
                SELECT RAISE(ABORT, 'latest property must match report sample');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_property_latest_monotonic
            BEFORE UPDATE ON device_property_latest
            WHEN NEW.observed_at_ms < OLD.observed_at_ms
              OR NEW.epoch_generation < OLD.epoch_generation
              OR (
                NEW.epoch_generation = OLD.epoch_generation
                AND (
                    NEW.adapter_epoch <> OLD.adapter_epoch
                    OR NEW.adapter_sequence <= OLD.adapter_sequence
                )
              )
              OR NEW.version <= OLD.version
            BEGIN
                SELECT RAISE(ABORT, 'latest property cannot move backwards');
            END
            """,
        ),
    ),
    TableSpec(
        "device_property_sample",
        """
        CREATE TABLE IF NOT EXISTS device_property_sample (
            sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_uuid TEXT NOT NULL,
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            property_key TEXT NOT NULL CHECK (TRIM(property_key) <> ''),
            value_type TEXT NOT NULL CHECK (TRIM(value_type) <> ''),
            value_json TEXT NOT NULL CHECK (json_valid(value_json)),
            value_hash TEXT NOT NULL CHECK (TRIM(value_hash) <> ''),
            quality TEXT NOT NULL CHECK (quality IN ('good','uncertain','bad')),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            UNIQUE(report_uuid, property_key),
            FOREIGN KEY(report_uuid, device_uuid)
                REFERENCES device_state_report(report_uuid, device_uuid)
                ON DELETE CASCADE
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_property_sample_timeline
            ON device_property_sample(
                device_uuid, property_key, observed_at_ms DESC, sample_id DESC
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_property_sample_retention
            ON device_property_sample(received_at_ms, sample_id)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_property_sample_immutable
            BEFORE UPDATE ON device_property_sample
            BEGIN
                SELECT RAISE(ABORT, 'device property sample is immutable');
            END
            """,
        ),
    ),
    TableSpec(
        "device_connection_latest",
        """
        CREATE TABLE IF NOT EXISTS device_connection_latest (
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            endpoint_uuid TEXT NOT NULL CHECK (TRIM(endpoint_uuid) <> ''),
            transport TEXT NOT NULL CHECK (transport IN ('hostlink','ros2')),
            connection_state TEXT NOT NULL CHECK (
                connection_state IN ('online','offline','degraded','unknown')
            ),
            session_uuid TEXT,
            source_event_uuid TEXT NOT NULL CHECK (TRIM(source_event_uuid) <> ''),
            adapter_epoch TEXT NOT NULL CHECK (TRIM(adapter_epoch) <> ''),
            epoch_generation INTEGER NOT NULL CHECK (epoch_generation >= 0),
            adapter_sequence INTEGER NOT NULL CHECK (adapter_sequence >= 0),
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            last_seen_at_ms INTEGER NOT NULL CHECK (last_seen_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            PRIMARY KEY(device_uuid, endpoint_uuid)
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_connection_latest_state
            ON device_connection_latest(
                connection_state, last_seen_at_ms DESC, device_uuid
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_connection_latest_endpoint
            ON device_connection_latest(endpoint_uuid, connection_state, device_uuid)
            """,
        ),
    ),
    TableSpec(
        "device_connection_event",
        """
        CREATE TABLE IF NOT EXISTS device_connection_event (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            batch_uuid TEXT NOT NULL,
            item_index INTEGER NOT NULL CHECK (item_index >= 0),
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            previous_state TEXT CHECK (
                previous_state IN ('online','offline','degraded','unknown')
            ),
            new_state TEXT NOT NULL CHECK (
                new_state IN ('online','offline','degraded','unknown')
            ),
            reason TEXT,
            observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            UNIQUE(batch_uuid, item_index),
            CHECK (previous_state IS NULL OR previous_state <> new_state),
            FOREIGN KEY(batch_uuid) REFERENCES telemetry_ingest_batch(batch_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_connection_event_timeline
            ON device_connection_event(
                device_uuid, observed_at_ms DESC, event_id DESC
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_connection_event_retention
            ON device_connection_event(received_at_ms, event_id)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_connection_event_committed_batch
            BEFORE INSERT ON device_connection_event
            WHEN NOT EXISTS (
                SELECT 1
                FROM telemetry_ingest_batch AS batch
                WHERE batch.batch_uuid = NEW.batch_uuid
                  AND batch.status = 'committed'
            )
            BEGIN
                SELECT RAISE(ABORT, 'connection event requires committed ingest batch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_connection_event_immutable
            BEFORE UPDATE ON device_connection_event
            BEGIN
                SELECT RAISE(ABORT, 'device connection event is immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_connection_latest_insert_source
            BEFORE INSERT ON device_connection_latest
            WHEN NOT EXISTS (
                SELECT 1
                FROM device_connection_event AS event
                JOIN telemetry_ingest_batch AS batch
                  ON batch.batch_uuid = event.batch_uuid
                WHERE event.event_uuid = NEW.source_event_uuid
                  AND event.device_uuid = NEW.device_uuid
                  AND event.new_state = NEW.connection_state
                  AND event.observed_at_ms = NEW.observed_at_ms
                  AND batch.endpoint_uuid = NEW.endpoint_uuid
                  AND batch.transport = NEW.transport
                  AND batch.adapter_epoch = NEW.adapter_epoch
                  AND batch.epoch_generation = NEW.epoch_generation
                  AND batch.adapter_sequence = NEW.adapter_sequence
            )
            BEGIN
                SELECT RAISE(ABORT, 'latest connection must match connection event');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_connection_latest_update_source
            BEFORE UPDATE ON device_connection_latest
            WHEN NOT EXISTS (
                SELECT 1
                FROM device_connection_event AS event
                JOIN telemetry_ingest_batch AS batch
                  ON batch.batch_uuid = event.batch_uuid
                WHERE event.event_uuid = NEW.source_event_uuid
                  AND event.device_uuid = NEW.device_uuid
                  AND event.new_state = NEW.connection_state
                  AND event.observed_at_ms = NEW.observed_at_ms
                  AND batch.endpoint_uuid = NEW.endpoint_uuid
                  AND batch.transport = NEW.transport
                  AND batch.adapter_epoch = NEW.adapter_epoch
                  AND batch.epoch_generation = NEW.epoch_generation
                  AND batch.adapter_sequence = NEW.adapter_sequence
            )
            BEGIN
                SELECT RAISE(ABORT, 'latest connection must match connection event');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_connection_latest_monotonic
            BEFORE UPDATE ON device_connection_latest
            WHEN NEW.observed_at_ms < OLD.observed_at_ms
              OR NEW.epoch_generation < OLD.epoch_generation
              OR (
                NEW.epoch_generation = OLD.epoch_generation
                AND (
                    NEW.adapter_epoch <> OLD.adapter_epoch
                    OR NEW.adapter_sequence <= OLD.adapter_sequence
                )
              )
              OR NEW.updated_at_ms < OLD.updated_at_ms
              OR NEW.version <= OLD.version
            BEGIN
                SELECT RAISE(ABORT, 'latest connection cannot move backwards');
            END
            """,
        ),
    ),
    TableSpec(
        "device_alarm",
        """
        CREATE TABLE IF NOT EXISTS device_alarm (
            alarm_uuid TEXT PRIMARY KEY CHECK (TRIM(alarm_uuid) <> ''),
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            source_endpoint_uuid TEXT,
            source_transport TEXT CHECK (source_transport IN ('hostlink','ros2')),
            source_job_uuid TEXT,
            alarm_code TEXT NOT NULL CHECK (TRIM(alarm_code) <> ''),
            severity TEXT NOT NULL CHECK (
                severity IN ('info','warning','error','critical')
            ),
            state TEXT NOT NULL CHECK (state IN ('active','acknowledged','cleared')),
            summary TEXT NOT NULL CHECK (TRIM(summary) <> ''),
            payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
            last_event_uuid TEXT NOT NULL CHECK (TRIM(last_event_uuid) <> ''),
            opened_at_ms INTEGER NOT NULL CHECK (opened_at_ms >= 0),
            acknowledged_at_ms INTEGER CHECK (acknowledged_at_ms >= 0),
            cleared_at_ms INTEGER CHECK (cleared_at_ms >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            CHECK (
                (source_endpoint_uuid IS NULL AND source_transport IS NULL)
                OR (source_endpoint_uuid IS NOT NULL AND source_transport IS NOT NULL)
            ),
            CHECK (updated_at_ms >= opened_at_ms),
            CHECK (
                acknowledged_at_ms IS NULL OR acknowledged_at_ms >= opened_at_ms
            ),
            CHECK (cleared_at_ms IS NULL OR cleared_at_ms >= opened_at_ms),
            CHECK (
                (state = 'active' AND cleared_at_ms IS NULL)
                OR (
                    state = 'acknowledged'
                    AND acknowledged_at_ms IS NOT NULL
                    AND cleared_at_ms IS NULL
                )
                OR (state = 'cleared' AND cleared_at_ms IS NOT NULL)
            )
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_alarm_active
            ON device_alarm(device_uuid, severity, opened_at_ms DESC)
            WHERE state IN ('active','acknowledged')
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_alarm_job
            ON device_alarm(source_job_uuid, opened_at_ms)
            WHERE source_job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_alarm_endpoint
            ON device_alarm(source_endpoint_uuid, state, device_uuid)
            WHERE source_endpoint_uuid IS NOT NULL
            """,
        ),
    ),
    TableSpec(
        "device_alarm_event",
        """
        CREATE TABLE IF NOT EXISTS device_alarm_event (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE CHECK (TRIM(event_uuid) <> ''),
            alarm_uuid TEXT NOT NULL CHECK (TRIM(alarm_uuid) <> ''),
            device_uuid TEXT NOT NULL CHECK (TRIM(device_uuid) <> ''),
            batch_uuid TEXT,
            item_index INTEGER CHECK (item_index >= 0),
            source_kind TEXT NOT NULL CHECK (
                source_kind IN ('adapter','backend','user','system')
            ),
            source_command_uuid TEXT,
            source_actor_uuid TEXT,
            source_job_uuid TEXT,
            event_type TEXT NOT NULL CHECK (
                event_type IN ('opened','updated','acknowledged','cleared','reopened')
            ),
            previous_state TEXT CHECK (
                previous_state IN ('active','acknowledged','cleared')
            ),
            new_state TEXT NOT NULL CHECK (
                new_state IN ('active','acknowledged','cleared')
            ),
            severity TEXT NOT NULL CHECK (
                severity IN ('info','warning','error','critical')
            ),
            summary TEXT NOT NULL CHECK (TRIM(summary) <> ''),
            payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
            occurred_at_ms INTEGER NOT NULL CHECK (occurred_at_ms >= 0),
            received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
            UNIQUE(batch_uuid, item_index),
            CHECK (
                (source_kind = 'adapter' AND batch_uuid IS NOT NULL)
                OR (source_kind <> 'adapter' AND batch_uuid IS NULL)
            ),
            CHECK (
                (batch_uuid IS NULL AND item_index IS NULL)
                OR (batch_uuid IS NOT NULL AND item_index IS NOT NULL)
            ),
            CHECK (
                (event_type = 'opened' AND previous_state IS NULL AND new_state = 'active')
                OR (event_type = 'acknowledged' AND new_state = 'acknowledged')
                OR (event_type = 'cleared' AND new_state = 'cleared')
                OR (event_type = 'reopened' AND new_state = 'active')
                OR event_type = 'updated'
            ),
            FOREIGN KEY(batch_uuid) REFERENCES telemetry_ingest_batch(batch_uuid)
                ON DELETE RESTRICT
        )
        """,
        (
            """
            CREATE INDEX IF NOT EXISTS idx_device_alarm_event_timeline
            ON device_alarm_event(alarm_uuid, occurred_at_ms, event_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_alarm_event_device
            ON device_alarm_event(
                device_uuid, occurred_at_ms DESC, event_id DESC
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_alarm_event_job
            ON device_alarm_event(source_job_uuid, occurred_at_ms, event_id)
            WHERE source_job_uuid IS NOT NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_device_alarm_event_retention
            ON device_alarm_event(received_at_ms, event_id)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_alarm_event_committed_batch
            BEFORE INSERT ON device_alarm_event
            WHEN NEW.source_kind = 'adapter'
              AND NOT EXISTS (
                SELECT 1
                FROM telemetry_ingest_batch AS batch
                WHERE batch.batch_uuid = NEW.batch_uuid
                  AND batch.status = 'committed'
              )
            BEGIN
                SELECT RAISE(ABORT, 'adapter alarm event requires committed batch');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS trg_device_alarm_event_immutable
            BEFORE UPDATE ON device_alarm_event
            BEGIN
                SELECT RAISE(ABORT, 'device alarm event is immutable');
            END
            """,
        ),
    ),
    TableSpec(
        "telemetry_maintenance",
        """
        CREATE TABLE IF NOT EXISTS telemetry_maintenance (
            maintenance_key TEXT PRIMARY KEY CHECK (
                maintenance_key IN (
                    'telemetry_ingest_batch',
                    'device_state_report',
                    'device_property_sample',
                    'device_connection_event',
                    'device_alarm_event'
                )
            ),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
            keep_days INTEGER CHECK (keep_days > 0),
            max_rows INTEGER CHECK (max_rows > 0),
            delete_batch_size INTEGER NOT NULL DEFAULT 1000
                CHECK (delete_batch_size BETWEEN 1 AND 100000),
            last_pruned_row_id INTEGER NOT NULL DEFAULT 0
                CHECK (last_pruned_row_id >= 0),
            last_cutoff_at_ms INTEGER CHECK (last_cutoff_at_ms >= 0),
            last_pruned_at_ms INTEGER CHECK (last_pruned_at_ms >= 0),
            last_deleted_rows INTEGER NOT NULL DEFAULT 0
                CHECK (last_deleted_rows >= 0),
            updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0),
            CHECK (keep_days IS NOT NULL OR max_rows IS NOT NULL)
        )
        """,
    ),
)


TELEMETRY_DATABASE = DatabaseSpec(
    key="telemetry",
    filename="telemetry.db",
    role="high-frequency device telemetry projection",
    version=1,
    synchronous="NORMAL",
    tables=TELEMETRY_TABLES,
)


__all__ = ["TELEMETRY_DATABASE", "TELEMETRY_TABLES"]
