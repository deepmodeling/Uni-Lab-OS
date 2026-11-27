"""PostgreSQL 已知结构升级，与原 SQLite 迁移的数据处理保持一致。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg import Connection


def _table_exists(connection: Connection, table: str) -> bool:
    # information_schema.tables 同时包含视图。sample 等名字合并后变成视图，
    # 不能再被当成待搬运的旧表，否则第二次启动会把视图删掉。
    return connection.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' AND table_name=%s",
        (table,),
    ).fetchone() is not None


def _column_exists(connection: Connection, table: str, column: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column),
    ).fetchone() is not None


def _add_column_if_missing(
    connection: Connection, table: str, column: str, definition: str
) -> None:
    """补充一个已知的增量字段；字段定义来自代码固定契约。"""

    if not _column_exists(connection, table, column):
        connection.execute(
            f"ALTER TABLE public.{table} ADD COLUMN {column} {definition}"
        )


def _compact_workflow_debug_configuration(connection: Connection) -> None:
    """把旧 PostgreSQL 调试配置表并入 ``workflow_task``。"""

    if not _table_exists(connection, "workflow_task"):
        return
    for column, definition in (
        ("debug_create_time", "TEXT"),
        ("debug_update_time", "TEXT"),
        ("debug_start_node_uuids", "TEXT"),
        ("debug_breakpoint_node_uuids", "TEXT"),
        (
            "debug_execution_policy",
            "TEXT CHECK (debug_execution_policy IS NULL OR debug_execution_policy IN ('step', 'continue'))",
        ),
        (
            "debug_status",
            "TEXT CHECK (debug_status IS NULL OR debug_status IN ('paused', 'running', 'completed', 'stopped'))",
        ),
    ):
        _add_column_if_missing(connection, "workflow_task", column, definition)
    if not _table_exists(connection, "workflow_task_debug_configuration"):
        return

    orphan = connection.execute(
        """
        SELECT 1
        FROM public.workflow_task_debug_configuration AS debug
        LEFT JOIN public.workflow_task AS task
          ON task.uuid = debug.workflow_task_uuid
        WHERE task.uuid IS NULL
        LIMIT 1
        """
    ).fetchone()
    if orphan is not None:
        raise RuntimeError("workflow_task_debug_configuration 存在孤儿配置")

    conflict = connection.execute(
        """
        SELECT 1
        FROM public.workflow_task AS task
        JOIN public.workflow_task_debug_configuration AS debug
          ON debug.workflow_task_uuid = task.uuid
        WHERE task.debug_create_time IS NOT NULL
          AND (
              task.debug_create_time IS DISTINCT FROM debug.create_time
              OR task.debug_update_time IS DISTINCT FROM debug.update_time
              OR task.debug_start_node_uuids IS DISTINCT FROM debug.start_node_uuids
              OR task.debug_breakpoint_node_uuids IS DISTINCT FROM debug.breakpoint_node_uuids
              OR task.debug_execution_policy IS DISTINCT FROM debug.execution_policy
              OR task.debug_status IS DISTINCT FROM debug.status
          )
        LIMIT 1
        """
    ).fetchone()
    if conflict is not None:
        raise RuntimeError("workflow_task 调试配置与旧子表数据冲突")

    connection.execute(
        """
        UPDATE public.workflow_task AS task
        SET debug_create_time = debug.create_time,
            debug_update_time = debug.update_time,
            debug_start_node_uuids = debug.start_node_uuids,
            debug_breakpoint_node_uuids = debug.breakpoint_node_uuids,
            debug_execution_policy = debug.execution_policy,
            debug_status = debug.status
        FROM public.workflow_task_debug_configuration AS debug
        WHERE debug.workflow_task_uuid = task.uuid
          AND task.debug_create_time IS NULL
        """
    )
    connection.execute("DROP TABLE public.workflow_task_debug_configuration")


_WORKFLOW_DEFINITION_TABLES = (
    "workflow_inventory_requirement",
    "workflow_authoring",
    "workflow_source_registration",
    "workflow_edge",
    "workflow_node",
    "workflow",
    "workflow_handle_template",
    "workflow_node_template",
)


def _drop_foreign_keys_to_workflow_definitions(connection: Connection) -> None:
    """移除旧运行表指向工作流定义表的外键，允许安全清理空壳。"""

    placeholders = ",".join("%s" for _ in _WORKFLOW_DEFINITION_TABLES)
    constraints = connection.execute(
        f"""
        SELECT child.relname, constraint_info.conname
        FROM pg_constraint AS constraint_info
        JOIN pg_class AS child
          ON child.oid = constraint_info.conrelid
        JOIN pg_namespace AS child_schema
          ON child_schema.oid = child.relnamespace
        JOIN pg_class AS parent
          ON parent.oid = constraint_info.confrelid
        JOIN pg_namespace AS parent_schema
          ON parent_schema.oid = parent.relnamespace
        WHERE constraint_info.contype = 'f'
          AND child_schema.nspname = 'public'
          AND parent_schema.nspname = 'public'
          AND parent.relname IN ({placeholders})
        """,
        tuple(_WORKFLOW_DEFINITION_TABLES),
    ).fetchall()
    for child, constraint_name in constraints:
        quoted_child = str(child).replace('"', '""')
        quoted_constraint = str(constraint_name).replace('"', '""')
        connection.execute(
            f'ALTER TABLE public."{quoted_child}" '
            f'DROP CONSTRAINT "{quoted_constraint}"'
        )


def _drop_empty_workflow_definition_tables(connection: Connection) -> bool:
    """删除旧 PostgreSQL 运行库遗留的空工作流定义表。

    运行库不再创建定义表，但升级旧库时必须先确认整组表为空；存在历史数据时
    保留它们，避免为了清理 schema 而静默丢弃旧版本数据。
    """

    present = [
        name for name in _WORKFLOW_DEFINITION_TABLES if _table_exists(connection, name)
    ]
    if not present:
        return True
    if any(
        connection.execute(f"SELECT 1 FROM public.{name} LIMIT 1").fetchone() is not None
        for name in present
    ):
        return False
    _drop_foreign_keys_to_workflow_definitions(connection)
    for name in _WORKFLOW_DEFINITION_TABLES:
        if name in present:
            connection.execute(f"DROP TABLE public.{name}")
    return True


def upgrade(
    connection: Connection,
    scope: str,
    *,
    persist_workflow_definitions: bool = True,
) -> None:
    """在初始化事务和锁内升级；仅识别已知缺列，不猜测任意数据转换。"""
    if scope == "authority" and not _column_exists(
        connection, "local_edge_job", "resolution_outcome_json"
    ):
        connection.execute(
            "ALTER TABLE public.local_edge_job ADD COLUMN resolution_outcome_json TEXT"
        )
    if scope == "authority":
        for column, definition in (
            ("execution_source", "TEXT NOT NULL DEFAULT 'workflow'"),
            ("claim_uuid", "TEXT"),
            ("attempt", "INTEGER"),
            ("fences_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            _add_column_if_missing(connection, "local_edge_job", column, definition)
        for column in ("traceparent", "tracestate"):
            _add_column_if_missing(
                connection, "local_edge_command", column, "TEXT NOT NULL DEFAULT ''"
            )
        _collapse_local_edge_meta(connection)
    if scope == "edge" and not _column_exists(
        connection, "edge_resource_fence", "task_uuid"
    ):
        connection.execute(
            "ALTER TABLE public.edge_resource_fence "
            "ADD COLUMN task_uuid TEXT NOT NULL DEFAULT ''"
        )
        # 与 SQLite v4 迁移相同：仍有作业镜像时恢复归属，已退役的保持空值。
        connection.execute(
            "UPDATE public.edge_resource_fence f SET task_uuid=COALESCE("
            "(SELECT j.task_uuid FROM public.edge_job_runtime j "
            "WHERE j.job_uuid=f.job_uuid), '')"
        )
    if scope == "edge":
        for column in ("trace_id", "traceparent", "tracestate"):
            _add_column_if_missing(
                connection, "edge_event_outbox", column, "TEXT NOT NULL DEFAULT ''"
            )
        for column, definition in (
            ("claim_uuid", "TEXT NOT NULL DEFAULT ''"),
            ("attempt", "INTEGER NOT NULL DEFAULT 1"),
            ("fences_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("trace_id", "TEXT NOT NULL DEFAULT ''"),
            ("traceparent", "TEXT NOT NULL DEFAULT ''"),
            ("tracestate", "TEXT NOT NULL DEFAULT ''"),
            ("execution_source", "TEXT NOT NULL DEFAULT 'workflow'"),
        ):
            _add_column_if_missing(connection, "edge_job_runtime", column, definition)
        if _table_exists(connection, "edge_job_outcome_pending"):
            _add_column_if_missing(
                connection,
                "edge_job_outcome_pending",
                "unknown_command_ids_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
        _fold_edge_pending_outcome(connection)
    if scope == "device":
        _fold_device_property_latest(connection)
    if scope == "workflow" and _table_exists(connection, "workflow_station_event_outbox"):
        pending = connection.execute(
            "SELECT 1 FROM public.workflow_station_event_outbox "
            "WHERE acked_at IS NULL LIMIT 1"
        ).fetchone()
        if pending is None:
            connection.execute("DROP TABLE public.workflow_station_event_outbox")
    if scope == "history":
        _add_column_if_missing(connection, "workflow_runs", "started_at", "DOUBLE PRECISION")
    if scope == "workflow":
        _compact_workflow_debug_configuration(connection)
        if not persist_workflow_definitions:
            _drop_empty_workflow_definition_tables(connection)
        else:
            _add_column_if_missing(
                connection, "workflow", "workflow_type", "TEXT NOT NULL DEFAULT 'normal'"
            )
            _add_column_if_missing(
                connection,
                "workflow_node",
                "manual_confirmation",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            _add_column_if_missing(
                connection, "workflow_authoring", "writeback_generation", "TEXT"
            )
    if scope == "inventory":
        _add_column_if_missing(
            connection, "material", "type", "TEXT NOT NULL DEFAULT ''"
        )
        _add_column_if_missing(
            connection,
            "resource_template",
            "available_sites",
            "TEXT NOT NULL DEFAULT '[]'",
        )
        _compact_inventory_tables(connection)
        _backfill_active_ingress_resources(connection)
        _backfill_blank_material_type(connection)
        # 同 SQLite v20：物料按 UUID 标识，旧根名称唯一索引不得阻止离站。
        connection.execute("DROP INDEX IF EXISTS public.ux_material_root_name_active")
        _widen_legacy_resource_scope_check(connection, "station_execution_lock_lease")
    elif scope == "workflow":
        _add_column_if_missing(
            connection, "job_device_tenancy_transition", "release_tenancy_uuid", "TEXT"
        )
        _backfill_release_tenancy_uuid(connection)
        _widen_legacy_resource_scope_check(connection, "execution_lock_lease")
        _widen_legacy_resource_scope_check(connection, "execution_lock_waiter")
        _backfill_execution_lock_identities(connection)


_LEGACY_INVENTORY_TABLES = (
    "material_inventory",
    "relative_position",
    "material_content_version",
    "resource_template_inventory",
    "station_ingress_reservation_site",
    "sample",
    "current_substance",
    "reagent",
    "lab_zone",
    "lab_placement",
)

_VIEWS_DEPENDING_ON_LEGACY_TABLES = (
    "inventory_resource_template",
    "material_instance",
    "substance_content",
    "resource_relation",
)


def _drop_views_blocking_legacy_tables(connection: Connection) -> None:
    """先卸掉仍引用旧子表的兼容视图，否则 PostgreSQL 拒绝删表。"""

    if not any(_table_exists(connection, name) for name in _LEGACY_INVENTORY_TABLES):
        return
    for name in _VIEWS_DEPENDING_ON_LEGACY_TABLES:
        connection.execute(f"DROP VIEW IF EXISTS public.{name} CASCADE")


def _compact_inventory_tables(connection: Connection) -> None:
    """把已有 PostgreSQL 库存子表并进主表。新库没有旧表时直接返回。"""

    _drop_views_blocking_legacy_tables(connection)
    if _table_exists(connection, "material"):
        for column, definition in (
            ("legacy_cloud_id", "TEXT NOT NULL DEFAULT ''"),
            ("legacy_template_id", "TEXT NOT NULL DEFAULT ''"),
            ("lot_id", "TEXT NOT NULL DEFAULT ''"),
            ("inventory_status", "TEXT NOT NULL DEFAULT 'warehouse'"),
            ("disposition", "TEXT NOT NULL DEFAULT 'active'"),
            ("aggregate_version", "INTEGER NOT NULL DEFAULT 0"),
            ("content_version", "INTEGER NOT NULL DEFAULT 0"),
            ("pose_uuid", "TEXT"),
            ("pose_description", "TEXT"),
            ("pose_meta_data", "TEXT NOT NULL DEFAULT '{}'"),
            ("position_x", "DOUBLE PRECISION"),
            ("position_y", "DOUBLE PRECISION"),
            ("position_z", "DOUBLE PRECISION"),
            ("depth", "DOUBLE PRECISION"),
            ("length", "DOUBLE PRECISION"),
            ("width", "DOUBLE PRECISION"),
            ("scale_x", "DOUBLE PRECISION"),
            ("scale_y", "DOUBLE PRECISION"),
            ("scale_z", "DOUBLE PRECISION"),
            ("rotation_x", "DOUBLE PRECISION"),
            ("rotation_y", "DOUBLE PRECISION"),
            ("rotation_z", "DOUBLE PRECISION"),
        ):
            _add_column_if_missing(connection, "material", column, definition)
    if _table_exists(connection, "material_inventory"):
        connection.execute(
            """
            UPDATE public.material AS material SET
                legacy_cloud_id = inventory.legacy_cloud_id,
                legacy_template_id = inventory.legacy_template_id,
                lot_id = inventory.lot_id,
                inventory_status = inventory.inventory_status,
                disposition = inventory.disposition,
                aggregate_version = inventory.aggregate_version
            FROM public.material_inventory AS inventory
            WHERE inventory.material_uuid = material.uuid
            """
        )
        connection.execute("DROP TABLE public.material_inventory")
    if _table_exists(connection, "material_content_version"):
        connection.execute(
            """
            UPDATE public.material AS material
            SET content_version = content.version
            FROM public.material_content_version AS content
            WHERE content.material_uuid = material.uuid
            """
        )
        connection.execute("DROP TABLE public.material_content_version")
    if _table_exists(connection, "relative_position"):
        connection.execute(
            """
            UPDATE public.material AS material SET
                pose_uuid = pose.uuid,
                pose_description = pose.description,
                pose_meta_data = pose.meta_data,
                position_x = pose.position_x,
                position_y = pose.position_y,
                position_z = pose.position_z,
                depth = pose.depth,
                length = pose.length,
                width = pose.width,
                scale_x = pose.scale_x,
                scale_y = pose.scale_y,
                scale_z = pose.scale_z,
                rotation_x = pose.rotation_x,
                rotation_y = pose.rotation_y,
                rotation_z = pose.rotation_z
            FROM public.relative_position AS pose
            WHERE pose.material_uuid = material.uuid AND pose.deleted_at IS NULL
            """
        )
        connection.execute("DROP TABLE public.relative_position")
    if _table_exists(connection, "resource_template"):
        _add_column_if_missing(
            connection, "resource_template", "aggregate_version", "INTEGER NOT NULL DEFAULT 0"
        )
    if _table_exists(connection, "resource_template_inventory"):
        connection.execute(
            """
            UPDATE public.resource_template AS template
            SET aggregate_version = inventory.aggregate_version
            FROM public.resource_template_inventory AS inventory
            WHERE inventory.resource_template_uuid = template.uuid
            """
        )
        connection.execute("DROP TABLE public.resource_template_inventory")
    if _table_exists(connection, "station_ingress_reservation"):
        _add_column_if_missing(connection, "station_ingress_reservation", "site_uuid", "TEXT")
        _add_column_if_missing(
            connection,
            "station_ingress_reservation",
            "site_active",
            "INTEGER NOT NULL DEFAULT 0",
        )
    if _table_exists(connection, "station_ingress_reservation_site"):
        connection.execute(
            """
            UPDATE public.station_ingress_reservation AS reservation SET
                site_uuid = selected.site_uuid,
                site_active = selected.active
            FROM public.station_ingress_reservation_site AS selected
            WHERE selected.reservation_uuid = reservation.uuid
            """
        )
        connection.execute("DROP TABLE public.station_ingress_reservation_site")
    if _table_exists(connection, "lab_meta") and _column_exists(
        connection, "lab_meta", "meta_key"
    ):
        connection.execute(
            """
            CREATE TABLE public.lab_meta_row (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                lab_name TEXT NOT NULL DEFAULT '',
                lab_domain TEXT NOT NULL DEFAULT '',
                resource_graph_bootstrap_source TEXT,
                resource_graph_bootstrap_fingerprint TEXT,
                resource_graph_bootstrap_fingerprint_version TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO public.lab_meta_row(
                id, lab_name, lab_domain, resource_graph_bootstrap_source,
                resource_graph_bootstrap_fingerprint,
                resource_graph_bootstrap_fingerprint_version
            )
            SELECT 1,
                COALESCE(MAX(CASE WHEN meta_key='lab_name' THEN meta_value END), ''),
                COALESCE(MAX(CASE WHEN meta_key='lab_domain' THEN meta_value END), ''),
                MAX(CASE WHEN meta_key='resource_graph_bootstrap_source' THEN meta_value END),
                MAX(CASE WHEN meta_key='resource_graph_bootstrap_fingerprint' THEN meta_value END),
                MAX(CASE WHEN meta_key='resource_graph_bootstrap_fingerprint_version' THEN meta_value END)
            FROM public.lab_meta
            """
        )
        connection.execute("DROP TABLE public.lab_meta")
        connection.execute("ALTER TABLE public.lab_meta_row RENAME TO lab_meta")
    if _table_exists(connection, "container_content"):
        for source, kind, columns in (
            (
                "sample",
                "sample",
                "uuid, create_time, update_time, deleted_at, description, meta_data, "
                "material_uuid, code, name, sample_type, source, quantity, quantity_unit, "
                "collected_at, expires_at",
            ),
            (
                "current_substance",
                "substance",
                "uuid, create_time, update_time, deleted_at, description, meta_data, "
                "material_uuid, name, composition, quantity, quantity_unit, physical_state, "
                "revision, observed_at",
            ),
            (
                "reagent",
                "reagent",
                "uuid, create_time, update_time, deleted_at, description, meta_data, "
                "material_uuid, reagent_info_uuid, concentration_value, concentration_unit, "
                "quantity, quantity_unit, revision, physical_state, density_g_per_ml, "
                "density_source",
            ),
        ):
            if not _table_exists(connection, source):
                continue
            connection.execute(
                f"""
                INSERT INTO public.container_content(
                    {columns}, kind
                )
                SELECT {columns}, %s FROM public.{source}
                """,
                (kind,),
            )
            connection.execute(f"DROP TABLE public.{source}")
    for name in ("lab_placement", "lab_zone"):
        if _table_exists(connection, name):
            connection.execute(f"DROP TABLE public.{name}")


def _fold_edge_pending_outcome(connection: Connection) -> None:
    if not _table_exists(connection, "edge_job_runtime"):
        return
    for column, definition in (
        ("outcome", "TEXT"),
        ("return_info_json", "TEXT"),
        ("error_info_json", "TEXT"),
        ("unknown_command_ids_json", "TEXT"),
        ("outcome_updated_at", "DOUBLE PRECISION"),
    ):
        _add_column_if_missing(connection, "edge_job_runtime", column, definition)
    if not _table_exists(connection, "edge_job_outcome_pending"):
        return
    connection.execute(
        """
        UPDATE public.edge_job_runtime AS runtime SET
            outcome = pending.outcome,
            return_info_json = pending.return_info_json,
            error_info_json = pending.error_info_json,
            unknown_command_ids_json = pending.unknown_command_ids_json,
            outcome_updated_at = pending.updated_at
        FROM public.edge_job_outcome_pending AS pending
        WHERE pending.job_uuid = runtime.job_uuid
        """
    )
    connection.execute("DROP TABLE public.edge_job_outcome_pending")


def _fold_device_property_latest(connection: Connection) -> None:
    if not _table_exists(connection, "device_property_latest"):
        return
    if not _table_exists(connection, "device_property_history"):
        return
    connection.execute(
        """
        INSERT INTO public.device_property_history(
            device_id, property, value, value_type, recorded_at
        )
        SELECT latest.device_id, latest.property, latest.value,
               latest.value_type, latest.updated_at
        FROM public.device_property_latest AS latest
        WHERE NOT EXISTS (
            SELECT 1 FROM public.device_property_history AS history
            WHERE history.device_id = latest.device_id
              AND history.property = latest.property
              AND history.value = latest.value
              AND history.value_type = latest.value_type
              AND history.id = (
                  SELECT MAX(id) FROM public.device_property_history
                  WHERE device_id = latest.device_id AND property = latest.property
              )
        )
        """
    )
    connection.execute("DROP TABLE public.device_property_latest")


def _collapse_local_edge_meta(connection: Connection) -> None:
    if not _table_exists(connection, "local_edge_meta"):
        return
    if not _column_exists(connection, "local_edge_meta", "key"):
        return
    connection.execute(
        """
        CREATE TABLE public.local_edge_meta_row (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            execution_process_uuid TEXT,
            execution_restart_pending TEXT,
            sequence INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        INSERT INTO public.local_edge_meta_row(
            id, execution_process_uuid, execution_restart_pending, sequence
        )
        SELECT 1,
            MAX(CASE WHEN key='execution_process_uuid' THEN value END),
            MAX(CASE WHEN key='execution_restart_pending' THEN value END),
            COALESCE(MAX(CASE WHEN key='sequence' THEN value::integer END), 0)
        FROM public.local_edge_meta
        """
    )
    connection.execute("DROP TABLE public.local_edge_meta")
    connection.execute("ALTER TABLE public.local_edge_meta_row RENAME TO local_edge_meta")


def _backfill_active_ingress_resources(connection: Connection) -> None:
    """重启库存库时恢复活动入口预留的四类持续资源锁。

    SQLite 的入口迁移在建表后执行同一组回填。PostgreSQL 结构快照只能表达
    DDL，因此这里保留该数据迁移的显式 SQL，并用 ``ON CONFLICT`` 保证重复
    启动幂等。
    """

    connection.execute(
        """
        UPDATE station_ingress_reservation_resource AS resource
        SET active = 0
        WHERE resource.active = 1
          AND resource.reservation_uuid IN (
              SELECT reservation.uuid
              FROM station_ingress_reservation AS reservation
              WHERE reservation.state NOT IN ('reserved', 'in_transit')
                 OR reservation.site_active <> 1
                 OR reservation.site_uuid IS NULL
          )
        """
    )
    connection.execute(
        """
        INSERT INTO station_ingress_reservation_resource
            (reservation_uuid, lock_key, active)
        SELECT reservation.uuid,
               'material/' || site.material_uuid || '/site/' || site.uuid || '/exclusive',
               1
          FROM station_ingress_reservation AS reservation
          JOIN site ON site.uuid = reservation.site_uuid AND site.deleted_at IS NULL
         WHERE reservation.site_active = 1
           AND reservation.state IN ('reserved', 'in_transit')
        UNION
        SELECT reservation.uuid, '/devices/' || site.material_uuid, 1
          FROM station_ingress_reservation AS reservation
          JOIN site ON site.uuid = reservation.site_uuid AND site.deleted_at IS NULL
         WHERE reservation.site_active = 1
           AND reservation.state IN ('reserved', 'in_transit')
        UNION
        SELECT reservation.uuid,
               'material/' || reservation.carrier_material_uuid || '/exclusive',
               1
          FROM station_ingress_reservation AS reservation
         WHERE reservation.site_active = 1
           AND reservation.state IN ('reserved', 'in_transit')
        UNION
        SELECT reservation.uuid, '/devices/' || reservation.carrier_material_uuid, 1
          FROM station_ingress_reservation AS reservation
         WHERE reservation.site_active = 1
           AND reservation.state IN ('reserved', 'in_transit')
        ON CONFLICT (reservation_uuid, lock_key) DO UPDATE
            SET active = EXCLUDED.active
        """
    )


def _execute(connection, statement: str, parameters: tuple = ()) -> None:
    """同一条回填语句同时给 psycopg 和 SQLite 用。"""

    if not type(connection).__module__.startswith("psycopg"):
        statement = statement.replace("%s", "?")
    connection.execute(statement, parameters)


def _widen_legacy_resource_scope_check(connection: Connection, table: str) -> None:
    """去掉已知的三值 scope 检查，随后由结构核对补上包含 resource 的检查。

    只处理这一处已经在 SQLite 迁移里放宽过的约束。其他定义冲突仍拒绝启动。
    """

    rows = connection.execute(
        """
        SELECT c.conname, pg_get_constraintdef(c.oid)
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'public' AND t.relname = %s AND c.contype = 'c'
        """,
        (table,),
    ).fetchall()
    for name, definition in rows:
        compact = "".join(str(definition).split()).lower()
        legacy = all(
            token in compact for token in ("device", "material", "material_site")
        )
        if legacy and "resource" not in compact:
            connection.execute(
                f'ALTER TABLE public."{table}" DROP CONSTRAINT "{name}"'
            )


def _backfill_blank_material_type(connection) -> None:
    """用模板类型补齐仍为空的物料类型。重复执行不覆盖已有类型。"""

    _execute(
        connection,
        "UPDATE material SET type=("
        "SELECT resource_type FROM resource_template "
        "WHERE resource_template.uuid=material.resource_template_uuid"
        ") WHERE type=''",
    )


def _backfill_release_tenancy_uuid(connection) -> None:
    """为旧的设备占用交接补上释放租约身份。已有值保持不变。"""

    _execute(
        connection,
        """
        UPDATE job_device_tenancy_transition SET release_tenancy_uuid=(
            SELECT t.uuid FROM task_device_tenancy t
             WHERE t.workflow_task_uuid=job_device_tenancy_transition.workflow_task_uuid
               AND t.material_uuid=job_device_tenancy_transition.material_uuid
               AND t.device_lock_key=job_device_tenancy_transition.release_device_lock_key
               AND t.acquired_at<=job_device_tenancy_transition.create_time
             ORDER BY t.acquired_at DESC LIMIT 1)
         WHERE release_device_lock_key IS NOT NULL
           AND release_tenancy_uuid IS NULL
        """,
    )


def backfill_execution_lock_identities(connection) -> None:
    """从规范锁键恢复旧 Lease 的冗余物料和库位身份。"""

    import sqlite3

    from unilabos.workflow.resource_lock_key import parse_canonical_resource_lock_key

    rows = connection.execute(
        """
        SELECT uuid, lock_key, scope, material_uuid, site_uuid, state
          FROM execution_lock_lease
         WHERE deleted_at IS NULL
         ORDER BY uuid
        """
    ).fetchall()
    active_states = {"reserved", "running", "uncertain"}
    for uuid, lock_key, scope, material_uuid, site_uuid, state in rows:
        identity = parse_canonical_resource_lock_key(lock_key)
        material_uuid = str(material_uuid or "").strip() or None
        site_uuid = str(site_uuid or "").strip() or None
        invalid = (
            identity is None
            or identity.scope != str(scope or "")
            or (material_uuid is not None and material_uuid != identity.material_uuid)
            or (site_uuid is not None and site_uuid != identity.site_uuid)
        )
        if invalid:
            if str(state) in active_states:
                raise sqlite3.IntegrityError(
                    "活动执行锁租约包含非规范或不一致的资源身份"
                )
            continue
        if material_uuid != identity.material_uuid or site_uuid != identity.site_uuid:
            _execute(
                connection,
                """
                UPDATE execution_lock_lease
                   SET material_uuid = %s, site_uuid = %s
                 WHERE uuid = %s
                """,
                (identity.material_uuid, identity.site_uuid, uuid),
            )


def _backfill_execution_lock_identities(connection: Connection) -> None:
    backfill_execution_lock_identities(connection)
