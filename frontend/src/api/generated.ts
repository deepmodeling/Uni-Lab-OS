// 由 scripts/api-sync.mjs 从 openapi/unilabos-openapi.json 生成，请勿手改。
// 刷新：npm run api:sync（需要能 import unilabos 的 Python）。
export interface paths {
    "/api/v1/chemicals": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Chemicals
         * @description 按名称 / 别名 / CAS / 分子式模糊搜索，或按 CAS、InChIKey 精确查找。
         */
        get: operations["list_chemicals_api_v1_chemicals_get"];
        put?: never;
        /** Create Chemical */
        post: operations["create_chemical_api_v1_chemicals_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/chemicals/{chemical_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Chemical */
        get: operations["get_chemical_api_v1_chemicals__chemical_uuid__get"];
        put?: never;
        post?: never;
        /** Delete Chemical */
        delete: operations["delete_chemical_api_v1_chemicals__chemical_uuid__delete"];
        options?: never;
        head?: never;
        /** Update Chemical */
        patch: operations["update_chemical_api_v1_chemicals__chemical_uuid__patch"];
        trace?: never;
    };
    "/api/v1/debug/databases": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Databases
         * @description 列出四库的文件状态与每张表的行数。
         */
        get: operations["list_databases_api_v1_debug_databases_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/debug/databases/{database}/tables/{table}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Browse Table
         * @description 浏览单表：列定义 + 总行数 + 一页行数据（默认最新在前）。
         */
        get: operations["browse_table_api_v1_debug_databases__database__tables__table__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/device-processes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Processes */
        get: operations["list_processes_api_v1_device_processes_get"];
        put?: never;
        /** Create */
        post: operations["create_api_v1_device_processes_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/device-processes/device-classes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Device Classes
         * @description 可配置的设备类：本进程注册表已加载的 + 驱动包台账扫描到的。
         */
        get: operations["device_classes_api_v1_device_processes_device_classes_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/device-processes/{process_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get */
        get: operations["get_api_v1_device_processes__process_id__get"];
        /** Update */
        put: operations["update_api_v1_device_processes__process_id__put"];
        post?: never;
        /** Delete */
        delete: operations["delete_api_v1_device_processes__process_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/device-processes/{process_id}/restart": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Restart */
        post: operations["restart_api_v1_device_processes__process_id__restart_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/device-processes/{process_id}/start": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Start */
        post: operations["start_api_v1_device_processes__process_id__start_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/device-processes/{process_id}/stop": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Stop */
        post: operations["stop_api_v1_device_processes__process_id__stop_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Inventory */
        get: operations["inventory_api_v1_driver_packages_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages/catalog": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Catalog
         * @description 官方索引（HTTPConfig.driver_package_index_url）+ 本地 driver_package_catalog.json 合并后的可安装目录。
         */
        get: operations["catalog_api_v1_driver_packages_catalog_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages/install": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Install */
        post: operations["install_api_v1_driver_packages_install_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages/operations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Operations */
        get: operations["operations_api_v1_driver_packages_operations_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages/operations/{operation_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Operation */
        get: operations["operation_api_v1_driver_packages_operations__operation_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Uninstall */
        delete: operations["uninstall_api_v1_driver_packages__name__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages/{name}/enabled": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Set Enabled */
        put: operations["set_enabled_api_v1_driver_packages__name__enabled_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages/{name}/graphs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Graphs
         * @description 包自带的设备图（data-files ``share/<包>/graph`` 或源码目录 ``graph/``）。
         */
        get: operations["graphs_api_v1_driver_packages__name__graphs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages/{name}/graphs/{graph_name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Graph */
        get: operations["graph_api_v1_driver_packages__name__graphs__graph_name__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/driver-packages/{name}/graphs/{graph_name}/launch": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Launch
         * @description 以受管设备进程启动随包图：同名进程已存在则更新规格后重启。
         */
        post: operations["launch_api_v1_driver_packages__name__graphs__graph_name__launch_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/error-decisions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Error Decisions
         * @description 待人工决策的清单：执行面挂起的失败 attempt + 调度器在重启后接管的
         *     执行态未知 / 决策上下文丢失的 attempt，两者报告同形。
         */
        get: operations["error_decisions_api_v1_error_decisions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/error-decisions/{decision_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Resolve Error Decision */
        post: operations["resolve_error_decision_api_v1_error_decisions__decision_id__post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Runtime Events */
        get: operations["runtime_events_api_v1_events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/graphs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Graphs */
        get: operations["list_graphs_api_v1_graphs_get"];
        put?: never;
        /** Upsert Graph */
        post: operations["upsert_graph_api_v1_graphs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/graphs/live/payload": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Live Payload
         * @description 当前真实拓扑：material + material_link 实时序列化（非快照回放）。
         */
        get: operations["get_live_payload_api_v1_graphs_live_payload_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/graphs/{identity}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Graph */
        get: operations["get_graph_api_v1_graphs__identity__get"];
        put?: never;
        post?: never;
        /** Delete Graph */
        delete: operations["delete_graph_api_v1_graphs__identity__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/graphs/{identity}/payload": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Graph Payload */
        get: operations["get_graph_payload_api_v1_graphs__identity__payload_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Health */
        get: operations["health_api_v1_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/history/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Query Events */
        get: operations["query_events_api_v1_history_events_get"];
        put?: never;
        /** Append Event */
        post: operations["append_event_api_v1_history_events_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/history/events/{event_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Event */
        get: operations["get_event_api_v1_history_events__event_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/history/events/{event_uuid}/replacement": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Append Replacement */
        post: operations["append_replacement_api_v1_history_events__event_uuid__replacement_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/history/events/{event_uuid}/replacement-chain": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Replacement Chain */
        get: operations["replacement_chain_api_v1_history_events__event_uuid__replacement_chain_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/history/payloads": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Store Payload */
        post: operations["store_payload_api_v1_history_payloads_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/history/payloads/{payload_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Payload */
        get: operations["get_payload_api_v1_history_payloads__payload_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/hostlink/log-sources": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Sources */
        get: operations["sources_api_v1_hostlink_log_sources_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/hostlink/logs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Logs */
        get: operations["logs_api_v1_hostlink_logs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/hostlink/peers": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Hostlink Peers */
        get: operations["hostlink_peers_api_v1_hostlink_peers_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/lab/layout": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Layout */
        get: operations["get_layout_api_v1_lab_layout_get"];
        /** Put Layout */
        put: operations["put_layout_api_v1_lab_layout_put"];
        post?: never;
        /** Reset Layout */
        delete: operations["reset_layout_api_v1_lab_layout_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/changes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Changes */
        get: operations["changes_api_v1_materials_changes_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/changes/ack": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Acknowledge Changes */
        post: operations["acknowledge_changes_api_v1_materials_changes_ack_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Events
         * @description 物料变更 SSE 失效通知（与 workflow ``/api/v1/events`` 同范式）。
         *
         *     以 inventory_ledger 的 sequence 为游标推送 ``materials.changed``；
         *     浏览器只把事件当失效信号并回 HTTP 重取，payload 不承载业务正文。
         *     首连（无 Last-Event-ID）静默追平账本尾部，只推送此后的增量；
         *     重连带 Last-Event-ID 时从该游标续传，补齐离线期间的变更。
         */
        get: operations["events_api_v1_materials_events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/instances": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Materials */
        get: operations["list_materials_api_v1_materials_instances_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/instances/by-resource-id/{resource_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Material By Resource Id */
        get: operations["get_material_by_resource_id_api_v1_materials_instances_by_resource_id__resource_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/instances/{material_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Material */
        get: operations["get_material_api_v1_materials_instances__material_uuid__get"];
        put?: never;
        post?: never;
        /** Delete Material */
        delete: operations["delete_material_api_v1_materials_instances__material_uuid__delete"];
        options?: never;
        head?: never;
        /** Patch Material */
        patch: operations["patch_material_api_v1_materials_instances__material_uuid__patch"];
        trace?: never;
    };
    "/api/v1/materials/instances/{material_uuid}/data": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Put Data */
        put: operations["put_data_api_v1_materials_instances__material_uuid__data_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/instances/{material_uuid}/position": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Put Position */
        put: operations["put_position_api_v1_materials_instances__material_uuid__position_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/instances/{material_uuid}/tree": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Tree */
        get: operations["get_tree_api_v1_materials_instances__material_uuid__tree_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/instantiate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Instantiate Material
         * @description 物料出库/实例化：按 registry 资源类实例化草稿 → 权威登记（权威发 uuid）。
         *
         *     微前端出库入口——前端只提供「资源类 + 实例名」，实例化发生在微后端
         *     （Host 进程内已加载 registry/PLR）。产物以 ResourceSlot 引用 {id, uuid}
         *     写回动作参数（unilabos_deduct_resource 选择器），再由 apply_deduct_resource
         *     等动作消费。（def 端点走线程池，registry 实例化为阻塞调用。）
         */
        post: operations["instantiate_material_api_v1_materials_instantiate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/inventory/preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preview Inventory */
        post: operations["preview_inventory_api_v1_materials_inventory_preview_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/links": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Links
         * @description 拓扑边查询：物料/设备节点间的连接关系（node-link 的 link 行）。
         */
        get: operations["list_links_api_v1_materials_links_get"];
        put?: never;
        /**
         * Upsert Link
         * @description 拓扑边 upsert：同两端/handle/类型的边身份稳定，重复提交幂等。
         */
        post: operations["upsert_link_api_v1_materials_links_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/links/{link_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Delete Link */
        delete: operations["delete_link_api_v1_materials_links__link_uuid__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/lots": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Inventory Lots */
        get: operations["list_inventory_lots_api_v1_materials_lots_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/lots/inbound": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Inbound Inventory Lot */
        post: operations["inbound_inventory_lot_api_v1_materials_lots_inbound_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/lots/{lot_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Inventory Lot */
        get: operations["get_inventory_lot_api_v1_materials_lots__lot_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/lots/{lot_uuid}/stocktake": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Stocktake Inventory Lot */
        post: operations["stocktake_inventory_lot_api_v1_materials_lots__lot_uuid__stocktake_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/move": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Move Material */
        post: operations["move_material_api_v1_materials_move_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/notify-device": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Notify Device
         * @description 把权威已完成的物料变更分发到目标设备（本进程直调 / 跨机 HostLink）。
         *
         *     物料创建/变更只发生在微后端；设备侧投影由模块级
         *     downlink.notify_resource_tree_update 触发（add=拉取实例化+assign，
         *     remove=卸载移除），不经任何 host 编排类。同步等待设备回执；调用方
         *     应校验 notified 为 true。（def 端点走线程池，允许阻塞等待。）
         */
        post: operations["notify_device_api_v1_materials_notify_device_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/registry-classes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Registry Classes
         * @description registry 可实例化资源类目录（前端出库选择器的数据源）。
         *
         *     只列 pylabrobot 类型（可被 /instantiate 实例化）；显示名走 registry
         *     display_name 约定，缺省回退资源类 id。
         */
        get: operations["list_registry_classes_api_v1_materials_registry_classes_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/reservations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Inventory Reservations */
        get: operations["list_inventory_reservations_api_v1_materials_reservations_get"];
        put?: never;
        /** Reserve Inventory */
        post: operations["reserve_inventory_api_v1_materials_reservations_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/reservations/batch": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Reserve Task Inventory */
        post: operations["reserve_task_inventory_api_v1_materials_reservations_batch_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/reservations/by-job/{job_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Inventory Reservation By Job */
        get: operations["get_inventory_reservation_by_job_api_v1_materials_reservations_by_job__job_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/reservations/{reservation_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Inventory Reservation */
        get: operations["get_inventory_reservation_api_v1_materials_reservations__reservation_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/reservations/{reservation_uuid}/consume": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Consume Inventory Reservation */
        post: operations["consume_inventory_reservation_api_v1_materials_reservations__reservation_uuid__consume_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/reservations/{reservation_uuid}/quarantine": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Quarantine Inventory Reservation */
        post: operations["quarantine_inventory_reservation_api_v1_materials_reservations__reservation_uuid__quarantine_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/reservations/{reservation_uuid}/release": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Release Inventory Reservation */
        post: operations["release_inventory_reservation_api_v1_materials_reservations__reservation_uuid__release_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/snapshots/apply": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Apply Snapshot */
        post: operations["apply_snapshot_api_v1_materials_snapshots_apply_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/snapshots/compare": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Compare Snapshot */
        post: operations["compare_snapshot_api_v1_materials_snapshots_compare_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/snapshots/delta": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Apply Delta
         * @description 设备增量上报：只带变了的节点 / 段，权威按段合并（乐观锁在节点内）。
         */
        post: operations["apply_delta_api_v1_materials_snapshots_delta_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/templates": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Templates */
        get: operations["list_templates_api_v1_materials_templates_get"];
        put?: never;
        /** Create Template */
        post: operations["create_template_api_v1_materials_templates_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/templates/{template_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Template */
        get: operations["get_template_api_v1_materials_templates__template_uuid__get"];
        /** Put Template */
        put: operations["put_template_api_v1_materials_templates__template_uuid__put"];
        post?: never;
        /** Delete Template */
        delete: operations["delete_template_api_v1_materials_templates__template_uuid__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/transfer": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Transfer Material
         * @description transfer 提交权威位置后同步等设备完成 unload/load 投影；设备在此期间会回头
         *     读权威（tree.get），所以必须走线程池（def），不能占住事件循环。
         */
        post: operations["transfer_material_api_v1_materials_transfer_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/materials/trees": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Create Tree */
        post: operations["create_tree_api_v1_materials_trees_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/ping": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Ping
         * @description HTTP 版 ping-pong：回显客户端时间戳并附服务端时钟，供链路时延 / 时钟偏差诊断。
         *
         *     与控制 WebSocket 的 PingNotice / PongNotice 字段同名；host_node 的 test_latency
         *     对它所连的 Backend（本机进程或分体部署的 --role backend / 云端）逐次调用。
         */
        get: operations["ping_api_v1_ping_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/readiness": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Readiness
         * @description 就绪探针：能接收并执行任务时 HTTP 200，否则 503（Kubernetes readiness 合同）。
         *
         *     与 ``/health``（进程存活，恒 200）不同：Host 子进程启动 / 安静点重启期间、执行适配器
         *     尚未注册时返回 503；全量重置期间维护闸门同样返回 503。响应只含状态摘要。
         */
        get: operations["readiness_api_v1_readiness_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/digest": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Registry Digest
         * @description 权威持有的条目内容哈希索引：Host 上报前先取，只为权威没有的哈希附完整定义。
         */
        get: operations["get_registry_digest_api_v1_registry_digest_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/entries": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Registry Entries
         * @description 条目状态列表；``status`` 过滤 active/pending/removed/unusable。
         */
        get: operations["list_registry_entries_api_v1_registry_entries_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/entries/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Registry Entry
         * @description 条目详情：状态 + 生效 payload + 挂起 payload 与冲突明细。
         */
        get: operations["get_registry_entry_api_v1_registry_entries__name__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/entries/{name}/apply": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Apply Registry Entry
         * @description 把挂起版本切换为生效版本。
         */
        post: operations["apply_registry_entry_api_v1_registry_entries__name__apply_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/entries/{name}/dismiss": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Dismiss Registry Entry
         * @description 忽略挂起版本（历史保留，生效版本不动）。
         */
        post: operations["dismiss_registry_entry_api_v1_registry_entries__name__dismiss_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/entries/{name}/restore/{version}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Restore Registry Entry
         * @description 把历史版本内容还原为新的生效版本（条目版本号继续自增）。
         */
        post: operations["restore_registry_entry_api_v1_registry_entries__name__restore__version__post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/entries/{name}/versions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Registry Entry Versions */
        get: operations["list_registry_entry_versions_api_v1_registry_entries__name__versions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/entries/{name}/versions/{version}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Registry Entry Version */
        get: operations["get_registry_entry_version_api_v1_registry_entries__name__versions__version__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/pending-impacts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Registry Pending Impacts
         * @description 挂起条目影响面：冲突明细 + 受影响 workflow 节点（画布徽标数据源）。
         */
        get: operations["list_registry_pending_impacts_api_v1_registry_pending_impacts_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/reports": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Registry Reports
         * @description 上报批次统计（新增/更新/挂起/移除/复活/不可用计数与明细）。
         */
        get: operations["list_registry_reports_api_v1_registry_reports_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/registry/workflow-templates": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Registry Workflow Templates
         * @description 生效的工作流模板（设备包 ``@workflow``）：前端模板面板与脚本实例化的数据源。
         */
        get: operations["list_registry_workflow_templates_api_v1_registry_workflow_templates_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/reset": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Reset Preview */
        get: operations["reset_preview_api_v1_reset_get"];
        put?: never;
        /** Reset Request */
        post: operations["reset_request_api_v1_reset_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/resource-templates": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Report Resource Templates
         * @description Edge 全量上报（条目级替换：变了才升该条目版本，冲突挂起待确认）。
         */
        post: operations["report_resource_templates_api_v1_resource_templates_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/restart": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Restart Status */
        get: operations["restart_status_api_v1_restart_get"];
        put?: never;
        /**
         * Request Restart
         * @description 登记重启：暂停新派发，active job 清空后按 scope 重启并自动恢复。
         */
        post: operations["request_restart_api_v1_restart_post"];
        /** Cancel Restart */
        delete: operations["cancel_restart_api_v1_restart_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/adapter-commands": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Adapter Commands */
        get: operations["list_adapter_commands_api_v1_runtime_adapter_commands_get"];
        put?: never;
        /** Enqueue Adapter Command */
        post: operations["enqueue_adapter_command_api_v1_runtime_adapter_commands_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/adapter-commands/ack": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Acknowledge Adapter Command */
        post: operations["acknowledge_adapter_command_api_v1_runtime_adapter_commands_ack_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/adapter-commands/claim": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Claim Adapter Commands */
        post: operations["claim_adapter_commands_api_v1_runtime_adapter_commands_claim_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/adapter-commands/{adapter_command_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Adapter Command */
        get: operations["get_adapter_command_api_v1_runtime_adapter_commands__adapter_command_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/backend-events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Backend Events */
        get: operations["list_backend_events_api_v1_runtime_backend_events_get"];
        put?: never;
        /** Enqueue Backend Event */
        post: operations["enqueue_backend_event_api_v1_runtime_backend_events_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/backend-events/ack": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Acknowledge Backend Events */
        post: operations["acknowledge_backend_events_api_v1_runtime_backend_events_ack_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/backend-events/claim": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Claim Backend Events */
        post: operations["claim_backend_events_api_v1_runtime_backend_events_claim_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/backend-events/{event_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Backend Event */
        get: operations["get_backend_event_api_v1_runtime_backend_events__event_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/commands": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Commands */
        get: operations["list_commands_api_v1_runtime_commands_get"];
        put?: never;
        /** Receive Command */
        post: operations["receive_command_api_v1_runtime_commands_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/commands/{command_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Command */
        get: operations["get_command_api_v1_runtime_commands__command_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/endpoints": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Endpoint Snapshots */
        get: operations["list_endpoint_snapshots_api_v1_runtime_endpoints_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/endpoints/{endpoint_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Endpoint Snapshot */
        get: operations["get_endpoint_snapshot_api_v1_runtime_endpoints__endpoint_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/endpoints/{endpoint_uuid}/snapshot": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Upsert Endpoint Snapshot */
        put: operations["upsert_endpoint_snapshot_api_v1_runtime_endpoints__endpoint_uuid__snapshot_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/jobs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Execution Jobs */
        get: operations["list_execution_jobs_api_v1_runtime_jobs_get"];
        put?: never;
        /** Create Execution Job */
        post: operations["create_execution_job_api_v1_runtime_jobs_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/jobs/{job_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Execution Job */
        get: operations["get_execution_job_api_v1_runtime_jobs__job_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/jobs/{job_uuid}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Request Execution Cancel */
        post: operations["request_execution_cancel_api_v1_runtime_jobs__job_uuid__cancel_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/jobs/{job_uuid}/error-gate/decision": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Decide Error Gate */
        post: operations["decide_error_gate_api_v1_runtime_jobs__job_uuid__error_gate_decision_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/jobs/{job_uuid}/error-gate/open": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Open Error Gate */
        post: operations["open_error_gate_api_v1_runtime_jobs__job_uuid__error_gate_open_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/jobs/{job_uuid}/feedback": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Record Execution Feedback */
        post: operations["record_execution_feedback_api_v1_runtime_jobs__job_uuid__feedback_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/jobs/{job_uuid}/transitions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Transition Execution Job */
        post: operations["transition_execution_job_api_v1_runtime_jobs__job_uuid__transitions_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/sessions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Backend Sessions */
        get: operations["list_backend_sessions_api_v1_runtime_sessions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/runtime/sessions/{session_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Backend Session */
        get: operations["get_backend_session_api_v1_runtime_sessions__session_uuid__get"];
        /** Upsert Backend Session */
        put: operations["upsert_backend_session_api_v1_runtime_sessions__session_uuid__put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/scheduler/resources": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Scheduler Resources */
        get: operations["scheduler_resources_api_v1_scheduler_resources_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/status-incidents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Status Incidents */
        get: operations["status_incidents_api_v1_status_incidents_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/status-incidents/{incident_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Decide Status Incident */
        post: operations["decide_status_incident_api_v1_status_incidents__incident_id__post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/telemetry/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Query Events */
        get: operations["query_events_api_v1_telemetry_events_get"];
        put?: never;
        /** Ingest */
        post: operations["ingest_api_v1_telemetry_events_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/telemetry/events/{event_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Event */
        get: operations["get_event_api_v1_telemetry_events__event_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/telemetry/sources/{endpoint_uuid}/cursor": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Source Cursor */
        get: operations["get_source_cursor_api_v1_telemetry_sources__endpoint_uuid__cursor_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/telemetry/states": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Device States */
        get: operations["list_device_states_api_v1_telemetry_states_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/telemetry/states/{endpoint_uuid}/{device_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Device State */
        get: operations["get_device_state_api_v1_telemetry_states__endpoint_uuid___device_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-manual-confirmations/{confirmation_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Manual Confirmation */
        get: operations["get_manual_confirmation_api_v1_workflow_manual_confirmations__confirmation_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-manual-confirmations/{confirmation_uuid}/decision": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Decide Manual Confirmation
         * @description 原子记录人工确认决策；调度器消费后才推进对应 workflow job。
         */
        post: operations["decide_manual_confirmation_api_v1_workflow_manual_confirmations__confirmation_uuid__decision_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-node-jobs/{job_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Workflow Node Job */
        get: operations["get_workflow_node_job_api_v1_workflow_node_jobs__job_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-node-jobs/{job_uuid}/feedback-history": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Node Job Feedback History */
        get: operations["list_node_job_feedback_history_api_v1_workflow_node_jobs__job_uuid__feedback_history_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-node-jobs/{job_uuid}/results": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Node Job Results */
        get: operations["list_node_job_results_api_v1_workflow_node_jobs__job_uuid__results_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-node-runs/{run_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Workflow Node Run */
        get: operations["get_workflow_node_run_api_v1_workflow_node_runs__run_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-publications": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Published Workflows
         * @description 每个工作流的最新发布：输入输出、合同摘要与投影出的节点模板。
         */
        get: operations["list_published_workflows_api_v1_workflow_publications_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-tasks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Workflow Tasks */
        get: operations["list_workflow_tasks_api_v1_workflow_tasks_get"];
        put?: never;
        /** Create Workflow Task */
        post: operations["create_workflow_task_api_v1_workflow_tasks_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-tasks/{task_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Workflow Task */
        get: operations["get_workflow_task_api_v1_workflow_tasks__task_uuid__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-tasks/{task_uuid}/commands": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Command Workflow Task */
        post: operations["command_workflow_task_api_v1_workflow_tasks__task_uuid__commands_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-tasks/{task_uuid}/input": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        /**
         * Edit Workflow Task Input
         * @description 暂停时修改入口参数；已被已下发节点用过的参数冻结（3003）。
         */
        patch: operations["edit_workflow_task_input_api_v1_workflow_tasks__task_uuid__input_patch"];
        trace?: never;
    };
    "/api/v1/workflow-tasks/{task_uuid}/interventions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Task Interventions */
        get: operations["list_task_interventions_api_v1_workflow_tasks__task_uuid__interventions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-tasks/{task_uuid}/jobs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Workflow Node Jobs
         * @description attempt（物理执行）平铺视图；job uuid 与执行器/错误决策的 job_id 一致。
         */
        get: operations["list_workflow_node_jobs_api_v1_workflow_tasks__task_uuid__jobs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-tasks/{task_uuid}/manual-confirmations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Task Manual Confirmations */
        get: operations["list_task_manual_confirmations_api_v1_workflow_tasks__task_uuid__manual_confirmations_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-tasks/{task_uuid}/manual-confirmations/{confirmation_uuid}/decision": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Decide Task Manual Confirmation
         * @description task 维度兼容入口：先校验确认单确实属于该 task。
         */
        post: operations["decide_task_manual_confirmation_api_v1_workflow_tasks__task_uuid__manual_confirmations__confirmation_uuid__decision_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflow-tasks/{task_uuid}/node-runs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Workflow Node Runs
         * @description 节点运行视图：每节点一条，status/return_info 为当前 attempt，attempts 为历史。
         */
        get: operations["list_workflow_node_runs_api_v1_workflow_tasks__task_uuid__node_runs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Workflows */
        get: operations["list_workflows_api_v1_workflows_get"];
        put?: never;
        /** Create Workflow */
        post: operations["create_workflow_api_v1_workflows_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/from-template": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Create Workflow From Template
         * @description 注册表工作流模板 → 可运行工作流（角色绑定 + 类单实例自动解析，幂等 upsert）。
         */
        post: operations["create_workflow_from_template_api_v1_workflows_from_template_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/{workflow_uuid}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Workflow */
        get: operations["get_workflow_api_v1_workflows__workflow_uuid__get"];
        /** Update Workflow */
        put: operations["update_workflow_api_v1_workflows__workflow_uuid__put"];
        post?: never;
        /** Delete Workflow */
        delete: operations["delete_workflow_api_v1_workflows__workflow_uuid__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/{workflow_uuid}/authoring": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Authoring */
        get: operations["get_authoring_api_v1_workflows__workflow_uuid__authoring_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/{workflow_uuid}/authoring/apply": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Apply Authoring */
        post: operations["apply_authoring_api_v1_workflows__workflow_uuid__authoring_apply_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/{workflow_uuid}/authoring/draft": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Save Draft */
        put: operations["save_draft_api_v1_workflows__workflow_uuid__authoring_draft_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/{workflow_uuid}/composite-invocations": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Insert Composite Invocation
         * @description 插入组合调用：子工作流按冻结发布内联展开，父图 revision +1。
         */
        post: operations["insert_composite_invocation_api_v1_workflows__workflow_uuid__composite_invocations_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/{workflow_uuid}/graph": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Graph */
        get: operations["get_graph_api_v1_workflows__workflow_uuid__graph_get"];
        /** Save Graph */
        put: operations["save_graph_api_v1_workflows__workflow_uuid__graph_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/{workflow_uuid}/io": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /**
         * Declare Workflow Io
         * @description 声明输入输出合同与节点输入绑定；返回更新后的整图。
         */
        put: operations["declare_workflow_io_api_v1_workflows__workflow_uuid__io_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/{workflow_uuid}/publications": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Workflow Publications */
        get: operations["list_workflow_publications_api_v1_workflows__workflow_uuid__publications_get"];
        put?: never;
        /**
         * Publish Workflow
         * @description 把当前修订冻结为不可变发布。
         */
        post: operations["publish_workflow_api_v1_workflows__workflow_uuid__publications_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/v1/workflows/{workflow_uuid}/run-preflight": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Preflight Workflow */
        post: operations["preflight_workflow_api_v1_workflows__workflow_uuid__run_preflight_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** AdapterCommandAck */
        AdapterCommandAck: {
            /** Adapter Command Uuid */
            adapter_command_uuid: string;
            /** Ack Event Uuid */
            ack_event_uuid: string;
            /**
             * Acknowledged At Ms
             * @default 0
             */
            acknowledged_at_ms?: number;
        };
        /** AdapterCommandClaim */
        AdapterCommandClaim: {
            /** Endpoint Uuid */
            endpoint_uuid: string;
            /**
             * Now Ms
             * @default 0
             */
            now_ms?: number;
            /**
             * Lease Ms
             * @default 30000
             */
            lease_ms?: number;
            /**
             * Limit
             * @default 100
             */
            limit?: number;
        };
        /** AdapterCommandEnqueue */
        AdapterCommandEnqueue: {
            /** Adapter Command Uuid */
            adapter_command_uuid: string;
            /** Job Uuid */
            job_uuid?: string | null;
            /** Endpoint Uuid */
            endpoint_uuid: string;
            /** Source Command Uuid */
            source_command_uuid?: string | null;
            /** Trigger Event Uuid */
            trigger_event_uuid?: string | null;
            /** Target Adapter Epoch */
            target_adapter_epoch?: string | null;
            /**
             * Command Type
             * @enum {string}
             */
            command_type: "execute" | "cancel" | "release_failed" | "replace_result" | "resume_pending" | "reconcile_state";
            /** Payload Uuid */
            payload_uuid?: string | null;
            /**
             * Available At Ms
             * @default 0
             */
            available_at_ms?: number;
        };
        /** AggregatePrecondition */
        AggregatePrecondition: {
            /**
             * Aggregate Type
             * @enum {string}
             */
            aggregate_type: "resource_template" | "material" | "site" | "lot" | "reservation";
            /** Aggregate Uuid */
            aggregate_uuid: string;
            /** Expected Version */
            expected_version?: number | null;
            /** Expected State Hash */
            expected_state_hash?: string | null;
        };
        /** ApplyRequest */
        ApplyRequest: {
            /** Expected Draft Hash */
            expected_draft_hash: string;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
            /** Expected Candidate Hash */
            expected_candidate_hash: string;
        };
        /** BackendEventAck */
        BackendEventAck: {
            /** Session Uuid */
            session_uuid: string;
            /** Through Sequence */
            through_sequence: number;
            /**
             * Acknowledged At Ms
             * @default 0
             */
            acknowledged_at_ms?: number;
        };
        /** BackendEventClaim */
        BackendEventClaim: {
            /** Session Uuid */
            session_uuid: string;
            /**
             * Now Ms
             * @default 0
             */
            now_ms?: number;
            /**
             * Lease Ms
             * @default 30000
             */
            lease_ms?: number;
            /**
             * Limit
             * @default 100
             */
            limit?: number;
        };
        /** BackendEventEnqueue */
        BackendEventEnqueue: {
            /** Event Uuid */
            event_uuid: string;
            /** Event Type */
            event_type: string;
            /** Aggregate Type */
            aggregate_type: string;
            /** Aggregate Uuid */
            aggregate_uuid: string;
            /** Aggregate Version */
            aggregate_version: number;
            /** Job Uuid */
            job_uuid?: string | null;
            /** Summary */
            summary?: {
                [key: string]: unknown;
            };
            /** Detail Payload Uuid */
            detail_payload_uuid?: string | null;
            /** Traceparent */
            traceparent?: string | null;
            /** Tracestate */
            tracestate?: string | null;
            /**
             * Available At Ms
             * @default 0
             */
            available_at_ms?: number;
        };
        /** BackendSessionUpsert */
        BackendSessionUpsert: {
            /** Session Uuid */
            session_uuid: string;
            /** Edge Uuid */
            edge_uuid: string;
            /** Backend Uri */
            backend_uri: string;
            /** Authority Epoch */
            authority_epoch: string;
            /** Connection Epoch */
            connection_epoch: string;
            /**
             * State
             * @enum {string}
             */
            state: "connecting" | "active" | "reconciling" | "disconnected";
            /**
             * Command Cursor
             * @default 0
             */
            command_cursor?: number;
            /**
             * Event Send Cursor
             * @default 0
             */
            event_send_cursor?: number;
            /**
             * Event Ack Sequence
             * @default 0
             */
            event_ack_sequence?: number;
            /** Connected At Ms */
            connected_at_ms?: number | null;
            /** Disconnected At Ms */
            disconnected_at_ms?: number | null;
            /**
             * Observed At Ms
             * @default 0
             */
            observed_at_ms?: number;
        };
        /** ChemicalPatch */
        ChemicalPatch: {
            /** Expected Version */
            expected_version: number;
            /** Name */
            name?: string | null;
            /** Name En */
            name_en?: string | null;
            /** Aliases */
            aliases?: string[] | null;
            /** Cas */
            cas?: string | null;
            /** Inchi Key */
            inchi_key?: string | null;
            /** Molecular Formula */
            molecular_formula?: string | null;
            /** Smiles */
            smiles?: string | null;
            /** Molecular Weight */
            molecular_weight?: number | null;
            /** Density G Per Ml */
            density_g_per_ml?: number | null;
            /** Physical State */
            physical_state?: ("liquid" | "solid" | "gas" | "unknown") | null;
            /** Description */
            description?: string | null;
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            } | null;
        };
        /**
         * ChemicalSummary
         * @description 内容物引用的化学品定义摘要（只读，取自 ``/api/v1/chemicals``）。
         */
        ChemicalSummary: {
            /** Chemical Uuid */
            chemical_uuid: string;
            /** Name */
            name: string;
            /** Cas */
            cas?: string | null;
            /** Inchi Key */
            inchi_key?: string | null;
            /** Molecular Formula */
            molecular_formula?: string | null;
            /** Smiles */
            smiles?: string | null;
            /** Molecular Weight */
            molecular_weight?: number | null;
            /** Density G Per Ml */
            density_g_per_ml?: number | null;
            /**
             * Physical State
             * @default unknown
             * @enum {string}
             */
            physical_state?: "liquid" | "solid" | "gas" | "unknown";
        };
        /** ChemicalWrite */
        ChemicalWrite: {
            /** Name */
            name: string;
            /** Name En */
            name_en?: string | null;
            /** Aliases */
            aliases?: string[];
            /**
             * Cas
             * @description CAS 登记号，校验格式与校验位；可空，未删除的行之间唯一
             */
            cas?: string | null;
            /**
             * Inchi Key
             * @description 27 位标准 InChIKey；可空，未删除的行之间唯一
             */
            inchi_key?: string | null;
            /** Molecular Formula */
            molecular_formula?: string | null;
            /** Smiles */
            smiles?: string | null;
            /** Molecular Weight */
            molecular_weight?: number | null;
            /** Density G Per Ml */
            density_g_per_ml?: number | null;
            /**
             * Physical State
             * @default unknown
             * @enum {string}
             */
            physical_state?: "liquid" | "solid" | "gas" | "unknown";
            /**
             * Description
             * @default
             */
            description?: string;
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
            /** Chemical Uuid */
            chemical_uuid?: string | null;
        };
        /** CommandEnvelope */
        CommandEnvelope: {
            /** Command Uuid */
            command_uuid: string;
            /** Session Uuid */
            session_uuid: string;
            /** Backend Sequence */
            backend_sequence: number;
            /**
             * Command Type
             * @enum {string}
             */
            command_type: "execute_job" | "cancel_job" | "release_failed" | "replace_result" | "resume_pending" | "inventory_apply" | "reconcile";
            /** Job Uuid */
            job_uuid?: string | null;
            /** Payload Uuid */
            payload_uuid?: string | null;
            /** Payload Sha256 */
            payload_sha256: string;
            /** Summary */
            summary?: {
                [key: string]: unknown;
            };
            /** Traceparent */
            traceparent?: string | null;
            /**
             * Received At Ms
             * @default 0
             */
            received_at_ms?: number;
        };
        /**
         * CompositeInvocationRequest
         * @description 把已发布子工作流作为组合调用插入父图（父图 revision 乐观锁）。
         *
         *     ``child_revision`` 缺省取子工作流最新发布；``arguments`` 为子工作流输入的实参：
         *     静态 JSON 值，或 ``{"kind": "workflow_input", "parameter": 父工作流输入名}``。
         */
        CompositeInvocationRequest: {
            /** Revision */
            revision: number;
            /** Child Workflow Uuid */
            child_workflow_uuid: string;
            /** Child Revision */
            child_revision?: number | null;
            /** Invocation Uuid */
            invocation_uuid?: string | null;
            /** Pose */
            pose?: {
                [key: string]: unknown;
            };
            /** Arguments */
            arguments?: {
                [key: string]: unknown;
            };
        };
        /**
         * DeviceActionCapability
         * @description Endpoint 快照内的 action 能力及当前可用性。
         */
        DeviceActionCapability: {
            /** Device Uuid */
            device_uuid: string;
            /** Action Name */
            action_name: string;
            /** Action Type */
            action_type?: string | null;
            /**
             * Concurrency Mode
             * @enum {string}
             */
            concurrency_mode: "exclusive" | "unbounded";
            /**
             * State
             * @default active
             * @enum {string}
             */
            state?: "active" | "retired";
            /**
             * Availability
             * @default unknown
             * @enum {string}
             */
            availability?: "free" | "busy" | "unknown";
            /** Active Job Uuid */
            active_job_uuid?: string | null;
            /** Descriptor */
            descriptor?: {
                [key: string]: unknown;
            };
            /** Descriptor Hash */
            descriptor_hash: string;
            /** Observed At Ms */
            observed_at_ms: number;
        };
        /**
         * DeviceNodeInput
         * @description 简化的设备节点：id + 注册表类 + 初始化配置；服务补齐 uuid / pose 等字段。
         */
        DeviceNodeInput: {
            /** Id */
            id: string;
            /** Class */
            class: string;
            /**
             * Name
             * @default
             */
            name?: string;
            /** Config */
            config?: {
                [key: string]: unknown;
            };
            /** Pose */
            pose?: {
                [key: string]: unknown;
            } | null;
        };
        /** DeviceProcessWrite */
        DeviceProcessWrite: {
            /** Name */
            name: string;
            /** Devices */
            devices?: components["schemas"]["DeviceNodeInput"][];
            /** Graph Nodes */
            graph_nodes?: {
                [key: string]: unknown;
            }[] | null;
            /** Devices Dirs */
            devices_dirs?: string[];
            /** Package Names */
            package_names?: string[];
            /**
             * External Only
             * @default false
             */
            external_only?: boolean;
            /**
             * Auto Start
             * @default true
             */
            auto_start?: boolean;
            /**
             * Restart Policy
             * @default on-failure
             */
            restart_policy?: string;
            /**
             * Max Restarts
             * @default 5
             */
            max_restarts?: number;
            /** Extra Args */
            extra_args?: string[];
        };
        /**
         * DeviceRoute
         * @description Endpoint 快照内的设备 route，不是独立表记录。
         */
        DeviceRoute: {
            /** Route Uuid */
            route_uuid: string;
            /** Device Uuid */
            device_uuid: string;
            /** Driver Key */
            driver_key: string;
            /**
             * Priority
             * @default 0
             */
            priority?: number;
            /**
             * Enabled
             * @default true
             */
            enabled?: boolean;
            /**
             * Selected
             * @default false
             */
            selected?: boolean;
            /** Config Hash */
            config_hash: string;
            /** Config */
            config?: {
                [key: string]: unknown;
            };
        };
        /**
         * DeviceStateSnapshot
         * @description 随事件提交的完整设备快照；来源位置由外层事件提供。
         */
        DeviceStateSnapshot: {
            /** Device Uuid */
            device_uuid: string;
            /** State */
            state?: {
                [key: string]: unknown;
            };
            /** Properties */
            properties?: {
                [key: string]: unknown;
            };
            /**
             * Connection State
             * @default unknown
             * @enum {string}
             */
            connection_state?: "online" | "offline" | "degraded" | "unknown";
            /** Alarms */
            alarms?: {
                [key: string]: unknown;
            }[];
            /** State Hash */
            state_hash?: string | null;
            /** Observed At Ms */
            observed_at_ms: number;
        };
        /** DraftWriteRequest */
        DraftWriteRequest: {
            /** Python Source */
            python_source: string;
            /** Expected Draft Hash */
            expected_draft_hash: string | null;
            /** Expected Workflow Revision */
            expected_workflow_revision: number;
        };
        /** DriverPackageEnableRequest */
        DriverPackageEnableRequest: {
            /** Enabled */
            enabled: boolean;
        };
        /**
         * DriverPackageInstallRequest
         * @description 安装来源：GitHub 仓库地址（https://github.com/<owner>/<repo>[@ref]）、zip / tar.gz 归档地址或本机目录。
         *
         *     源码树落到 unilabos_data/driver_packages/<name>/<version>/（本机目录原地登记），
         *     依赖用 uv / pip 预装，不 pip install 包体。
         */
        DriverPackageInstallRequest: {
            /** Spec */
            spec: string;
            /**
             * Enable
             * @default true
             */
            enable?: boolean;
            /**
             * Upgrade
             * @description 重新下载源码树，并以 --upgrade 重装其依赖
             * @default false
             */
            upgrade?: boolean;
            /**
             * Name
             * @description 已知的包名（索引条目自带）；源码树没有 pyproject 时用它登记
             * @default
             */
            name?: string;
        };
        /** EndpointSnapshotUpsert */
        EndpointSnapshotUpsert: {
            /** Endpoint Uuid */
            endpoint_uuid: string;
            /**
             * Transport
             * @enum {string}
             */
            transport: "hostlink" | "ros2";
            /** Host Uuid */
            host_uuid: string;
            /** Instance Name */
            instance_name: string;
            /** Authority Epoch */
            authority_epoch: string;
            /** Adapter Epoch */
            adapter_epoch?: string | null;
            /**
             * Reconciliation Generation
             * @default 0
             */
            reconciliation_generation?: number;
            /**
             * State
             * @enum {string}
             */
            state: "online" | "offline" | "reconciling";
            /** Device Routes */
            device_routes?: components["schemas"]["DeviceRoute"][];
            /** Action Capabilities */
            action_capabilities?: components["schemas"]["DeviceActionCapability"][];
            /** Config */
            config?: {
                [key: string]: unknown;
            };
            /** Reconciled At Ms */
            reconciled_at_ms?: number | null;
            /**
             * Observed At Ms
             * @default 0
             */
            observed_at_ms?: number;
        };
        /** ErrorDecision */
        ErrorDecision: {
            /**
             * Action
             * @default abort
             */
            action?: string;
            /** Option */
            option?: {
                [key: string]: unknown;
            } | null;
            /**
             * Reason
             * @default
             */
            reason?: string;
            /** Result */
            result?: unknown;
            /**
             * Scheduler Updated
             * @default true
             */
            scheduler_updated?: boolean;
            /**
             * Job Id
             * @default
             */
            job_id?: string;
            /**
             * Device Id
             * @default
             */
            device_id?: string;
            /** Extra */
            extra?: {
                [key: string]: unknown;
            };
        };
        /** ErrorGateDecision */
        ErrorGateDecision: {
            /** Expected Version */
            expected_version: number;
            /** Decision Command Uuid */
            decision_command_uuid: string;
            /**
             * Action
             * @enum {string}
             */
            action: "release_failed" | "replace_result" | "cancel";
            /** Confirmed Scheduler Revision */
            confirmed_scheduler_revision: number;
            /** Adapter Command Uuid */
            adapter_command_uuid: string;
            /** Payload Uuid */
            payload_uuid?: string | null;
            /** Result Uuid */
            result_uuid?: string | null;
            /** Decision */
            decision?: {
                [key: string]: unknown;
            };
            /**
             * Resolved At Ms
             * @default 0
             */
            resolved_at_ms?: number;
        };
        /** ErrorGateOpen */
        ErrorGateOpen: {
            /** Expected Version */
            expected_version: number;
            /** Error Uuid */
            error_uuid: string;
            /** Error Code */
            error_code: string;
            /** Error Summary */
            error_summary: string;
            /** Required Scheduler Revision */
            required_scheduler_revision: number;
            /** Request Event Uuid */
            request_event_uuid: string;
            /** Detail Payload Uuid */
            detail_payload_uuid?: string | null;
            /** Summary */
            summary?: {
                [key: string]: unknown;
            };
            /**
             * Opened At Ms
             * @default 0
             */
            opened_at_ms?: number;
        };
        /** ExecutionJobCancel */
        ExecutionJobCancel: {
            /** Expected Version */
            expected_version: number;
            /** Cancel Command Uuid */
            cancel_command_uuid: string;
            /** Adapter Command Uuid */
            adapter_command_uuid: string;
            /** Payload Uuid */
            payload_uuid?: string | null;
            /**
             * Requested At Ms
             * @default 0
             */
            requested_at_ms?: number;
        };
        /** ExecutionJobCreate */
        ExecutionJobCreate: {
            /** Job Uuid */
            job_uuid: string;
            /** Task Uuid */
            task_uuid: string;
            /** Node Uuid */
            node_uuid: string;
            /** Attempt Group Uuid */
            attempt_group_uuid: string;
            /** Retry Of Job Uuid */
            retry_of_job_uuid?: string | null;
            /**
             * Attempt No
             * @default 1
             */
            attempt_no?: number;
            /**
             * Attempt Trigger
             * @default initial
             * @enum {string}
             */
            attempt_trigger?: "initial" | "retry_decision" | "recovery" | "loop_iteration";
            /** Execute Command Uuid */
            execute_command_uuid: string;
            /** Device Uuid */
            device_uuid: string;
            /** Action Name */
            action_name: string;
            /** Action Payload Uuid */
            action_payload_uuid: string;
            /** Route Uuid */
            route_uuid?: string | null;
            /** Endpoint Uuid */
            endpoint_uuid?: string | null;
            /** Transport */
            transport?: ("hostlink" | "ros2") | null;
            /** Material Bindings */
            material_bindings?: components["schemas"]["MaterialBinding"][];
            /** Scheduler Revision */
            scheduler_revision: number;
            /**
             * Accepted At Ms
             * @default 0
             */
            accepted_at_ms?: number;
        };
        /** ExecutionJobFeedback */
        ExecutionJobFeedback: {
            /** Expected Version */
            expected_version: number;
            /** Feedback Sequence */
            feedback_sequence: number;
            /**
             * Observed At Ms
             * @default 0
             */
            observed_at_ms?: number;
        };
        /** ExecutionJobTransition */
        ExecutionJobTransition: {
            /** Expected Version */
            expected_version: number;
            /**
             * Status
             * @enum {string}
             */
            status: "dispatch_pending" | "dispatched" | "running" | "terminal_waiting" | "succeeded" | "failed" | "canceled" | "execution_unknown" | "rejected";
            /** Scheduler Status Version */
            scheduler_status_version?: number | null;
            /** Feedback Sequence */
            feedback_sequence?: number | null;
            /** Result Uuid */
            result_uuid?: string | null;
            /** Error Code */
            error_code?: string | null;
            /** Error Summary */
            error_summary?: string | null;
            /**
             * Occurred At Ms
             * @default 0
             */
            occurred_at_ms?: number;
        };
        /**
         * ExternalPayloadWrite
         * @description 大 payload 的不可变外部对象引用。
         */
        ExternalPayloadWrite: {
            /**
             * Protocol Version
             * @default history.v1
             * @constant
             */
            protocol_version?: "history.v1";
            /**
             * Storage Kind
             * @default external
             * @constant
             */
            storage_kind?: "external";
            /** Payload Uuid */
            payload_uuid?: string | null;
            /** Media Type */
            media_type: string;
            /**
             * Encoding
             * @default binary
             */
            encoding?: string;
            /** Compression */
            compression?: string | null;
            /** Byte Length */
            byte_length: number;
            /** Sha256 */
            sha256: string;
            /** External Uri */
            external_uri: string;
            /** Created At Ms */
            created_at_ms?: number | null;
            /** Expires At Ms */
            expires_at_ms?: number | null;
        };
        /** GraphUpsertRequest */
        GraphUpsertRequest: {
            /** Name */
            name: string;
            /** Payload */
            payload: {
                [key: string]: unknown;
            };
            /** Uuid */
            uuid?: string | null;
            /** Tags */
            tags?: unknown[];
            /** Description */
            description?: string | null;
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
            /** Device Site Templates */
            device_site_templates?: {
                [key: string]: unknown[];
            } | null;
            /**
             * On Existing
             * @default replace
             * @enum {string}
             */
            on_existing?: "replace" | "adopt";
        };
        /** GraphWriteRequest */
        GraphWriteRequest: {
            /** Revision */
            revision: number;
            /** Nodes */
            nodes?: components["schemas"]["WorkflowNodeWrite"][];
            /** Edges */
            edges?: components["schemas"]["WorkflowEdgeWrite"][];
            /**
             * Site Binding Mode
             * @description SiteSlot 导入策略；浏览器保存草稿必须传 preserve，确认后参数中存放 site_uuid。
             * @default resolve
             * @enum {string}
             */
            site_binding_mode?: "resolve" | "preserve";
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /**
         * HistoryEventAppend
         * @description 追加一条历史事件；``sequence`` 只由 ``history.db`` 分配。
         */
        HistoryEventAppend: {
            /**
             * Protocol Version
             * @default history.v1
             * @constant
             */
            protocol_version?: "history.v1";
            /** Event Uuid */
            event_uuid?: string | null;
            /**
             * Event Type
             * @enum {string}
             */
            event_type: "job_transition" | "action_availability" | "job_feedback" | "job_result" | "job_log" | "error_snapshot" | "decision_audit";
            /** Job Uuid */
            job_uuid?: string | null;
            /** Endpoint Uuid */
            endpoint_uuid?: string | null;
            /** Device Uuid */
            device_uuid?: string | null;
            /** Action Name */
            action_name?: string | null;
            /** Event Key */
            event_key?: string | null;
            /** Job Sequence */
            job_sequence?: number | null;
            /** State Version */
            state_version?: number | null;
            /** Payload Uuid */
            payload_uuid?: string | null;
            /** Summary */
            summary?: {
                [key: string]: unknown;
            };
            /** Severity */
            severity?: string | null;
            /** Actor Type */
            actor_type?: string | null;
            /** Actor Uuid */
            actor_uuid?: string | null;
            /** Supersedes Event Uuid */
            supersedes_event_uuid?: string | null;
            /** Occurred At Ms */
            occurred_at_ms?: number | null;
            /** Recorded At Ms */
            recorded_at_ms?: number | null;
        };
        /** HistoryEventRecord */
        HistoryEventRecord: {
            /** Sequence */
            sequence?: number | null;
            /** Event Uuid */
            event_uuid: string;
            /**
             * Event Type
             * @enum {string}
             */
            event_type: "job_transition" | "action_availability" | "job_feedback" | "job_result" | "job_log" | "error_snapshot" | "decision_audit";
            /** Job Uuid */
            job_uuid?: string | null;
            /** Endpoint Uuid */
            endpoint_uuid?: string | null;
            /** Device Uuid */
            device_uuid?: string | null;
            /** Action Name */
            action_name?: string | null;
            /** Event Key */
            event_key?: string | null;
            /** Job Sequence */
            job_sequence?: number | null;
            /** State Version */
            state_version?: number | null;
            /** Payload Uuid */
            payload_uuid?: string | null;
            /** Summary */
            summary?: {
                [key: string]: unknown;
            };
            /** Severity */
            severity?: string | null;
            /** Actor Type */
            actor_type?: string | null;
            /** Actor Uuid */
            actor_uuid?: string | null;
            /** Supersedes Event Uuid */
            supersedes_event_uuid?: string | null;
            /** Occurred At Ms */
            occurred_at_ms: number;
            /** Recorded At Ms */
            recorded_at_ms: number;
        };
        /**
         * InlinePayloadWrite
         * @description 由微后端直接保存在 ``history.db`` 的小 payload。
         */
        InlinePayloadWrite: {
            /**
             * Protocol Version
             * @default history.v1
             * @constant
             */
            protocol_version?: "history.v1";
            /**
             * Storage Kind
             * @default inline
             * @constant
             */
            storage_kind?: "inline";
            /** Payload Uuid */
            payload_uuid?: string | null;
            /** Media Type */
            media_type: string;
            /**
             * Encoding
             * @default binary
             */
            encoding?: string;
            /** Compression */
            compression?: string | null;
            /** Inline Payload */
            inline_payload: string;
            /** Created At Ms */
            created_at_ms?: number | null;
            /** Expires At Ms */
            expires_at_ms?: number | null;
        };
        /**
         * InventoryMutation
         * @description 所有写请求共用的幂等信封。
         */
        InventoryMutation: {
            /**
             * Protocol Version
             * @default materials.v1
             * @constant
             */
            protocol_version?: "materials.v1";
            /** Command Uuid */
            command_uuid: string;
            /** Effect Key */
            effect_key: string;
            /** Operation */
            operation: string;
            /**
             * Actor Type
             * @default edge
             */
            actor_type?: string;
            /** Actor Uuid */
            actor_uuid?: string | null;
            /** Job Uuid */
            job_uuid?: string | null;
            /**
             * Observed At Ms
             * @default 0
             */
            observed_at_ms?: number;
            /** Preconditions */
            preconditions?: components["schemas"]["AggregatePrecondition"][];
            /** Payload */
            payload?: {
                [key: string]: unknown;
            };
        };
        /** InventoryPreviewRequest */
        InventoryPreviewRequest: {
            /** Requirements */
            requirements: components["schemas"]["InventoryRequirement"][];
        };
        /**
         * InventoryRequirement
         * @description 调度器冻结的一项库存需求。
         *
         *     两种 kind 区分的是账目形态而不是物料种类：``material`` 表示独立物料实例
         *     （有 uuid、可放到位点的个体，例如枪头盒、孔板、试剂瓶），只改变生命周期而不
         *     扣数量；``lot`` 表示按量计量的 ``inventory_lot`` 库存（散装试剂、散装耗材等），
         *     按 lot FIFO 从 available 预留并在动作开始时扣减。
         */
        InventoryRequirement: {
            /** Key */
            key: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "material" | "lot";
            /** Material Uuid */
            material_uuid?: string | null;
            /** Template Uuid */
            template_uuid?: string | null;
            /** Lot Uuid */
            lot_uuid?: string | null;
            /** Parent Material Uuid */
            parent_material_uuid?: string | null;
            /** Site Uuid */
            site_uuid?: string | null;
            /** Quantity */
            quantity?: number | null;
            /** Unit */
            unit?: string | null;
        };
        JsonValue: unknown;
        /**
         * LabLayoutRead
         * @description ``GET /api/v1/lab/layout`` 响应：从未保存时 ``revision = 0`` 且区域 / 围墙为空。
         */
        LabLayoutRead: {
            /** Layout Key */
            layout_key: string;
            /** Revision */
            revision: number;
            /** Cell Size */
            cell_size: number;
            /** Zones */
            zones?: components["schemas"]["LabZone"][];
            /** Walls */
            walls?: string[];
            /** Created At Ms */
            created_at_ms: number;
            /** Updated At Ms */
            updated_at_ms: number;
        };
        /**
         * LabLayoutWrite
         * @description ``PUT /api/v1/lab/layout`` 请求体：整份替换 + revision 乐观锁。
         *
         *     ``revision`` 是客户端读到的版本：从未保存过时为 0；不匹配返回 409。
         */
        LabLayoutWrite: {
            /** Revision */
            revision: number;
            /** Cell Size */
            cell_size: number;
            /** Zones */
            zones?: components["schemas"]["LabZone"][];
            /** Walls */
            walls?: string[];
        };
        /**
         * LabZone
         * @description 一个区域：稳定 id、展示名、颜色与所占格子。
         */
        LabZone: {
            /** Id */
            id: string;
            /** Name */
            name: string;
            /**
             * Color
             * @default #2e5bff
             */
            color?: string;
            /** Cells */
            cells?: string[];
        };
        /** LedgerAcknowledge */
        LedgerAcknowledge: {
            /** Through Sequence */
            through_sequence: number;
        };
        /**
         * ManualConfirmationDecisionRequest
         * @description 人工确认决策；``confirmed_by`` 缺省时仅 unrestricted 单可用默认操作员。
         */
        ManualConfirmationDecisionRequest: {
            /** Action */
            action: string;
            /** Confirmed By */
            confirmed_by?: string | null;
            /** Comment */
            comment?: string | null;
            /** Decision Idempotency Key */
            decision_idempotency_key?: string | null;
        };
        /**
         * ManualResultReplacement
         * @description 人工结果替换请求；job 和 action 归属从被替换事件继承。
         */
        ManualResultReplacement: {
            /**
             * Protocol Version
             * @default history.v1
             * @constant
             */
            protocol_version?: "history.v1";
            /** Supersedes Event Uuid */
            supersedes_event_uuid: string;
            /** Event Uuid */
            event_uuid?: string | null;
            /** Payload Uuid */
            payload_uuid?: string | null;
            /** Summary */
            summary?: {
                [key: string]: unknown;
            };
            /** Event Key */
            event_key?: string | null;
            /** State Version */
            state_version?: number | null;
            /** Severity */
            severity?: string | null;
            /**
             * Actor Type
             * @default human
             */
            actor_type?: string;
            /** Actor Uuid */
            actor_uuid: string;
            /** Occurred At Ms */
            occurred_at_ms?: number | null;
            /** Recorded At Ms */
            recorded_at_ms?: number | null;
        };
        /** MaterialAggregateRead */
        MaterialAggregateRead: {
            material: components["schemas"]["MaterialIdentityRead"];
            position: components["schemas"]["MaterialPosition"];
            /** Position Version */
            position_version: number;
            data: components["schemas"]["MaterialDataRead"];
            /** Sites */
            sites?: components["schemas"]["SiteRead"][];
            /** State Hash */
            state_hash: string;
        };
        /**
         * MaterialBinding
         * @description Job 接收时固化的物料绑定快照。
         */
        MaterialBinding: {
            /** Key */
            key: string;
            /** Role */
            role: string;
            /** Material Uuid */
            material_uuid?: string | null;
            /** Site Uuid */
            site_uuid?: string | null;
            /** Reservation Uuid */
            reservation_uuid?: string | null;
            /** Quantity */
            quantity?: number | null;
            /** Unit */
            unit?: string | null;
            /** Snapshot */
            snapshot?: {
                [key: string]: unknown;
            };
            /** Snapshot Hash */
            snapshot_hash: string;
        };
        /** MaterialDataRead */
        MaterialDataRead: {
            /** Data */
            data?: {
                [key: string]: unknown;
            };
            /** Substances */
            substances?: components["schemas"]["MaterialSubstance"][];
            /**
             * Sites Initialized
             * @default false
             */
            sites_initialized?: boolean;
            /** Unknown Counter */
            unknown_counter?: number | null;
            /**
             * State Status
             * @default created
             */
            state_status?: string;
            /** Source Event Uuid */
            source_event_uuid?: string | null;
            /** Source Job Uuid */
            source_job_uuid?: string | null;
            /** Source Command Uuid */
            source_command_uuid?: string | null;
            /**
             * Observed At Ms
             * @default 0
             */
            observed_at_ms?: number;
            /** Content Version */
            content_version: number;
            /** State Hash */
            state_hash: string;
            /** Updated At Ms */
            updated_at_ms: number;
            /** Version */
            version: number;
        };
        /** MaterialIdentityRead */
        MaterialIdentityRead: {
            /** Resource Id */
            resource_id: string;
            /** Parent Material Uuid */
            parent_material_uuid?: string | null;
            /** Lot Uuid */
            lot_uuid?: string | null;
            /** Name */
            name: string;
            /**
             * Display Name
             * @default
             */
            display_name?: string;
            /**
             * Description
             * @default
             */
            description?: string;
            /**
             * Resource Type
             * @default resource
             */
            resource_type?: string;
            /**
             * Class Name
             * @default Resource
             */
            class_name?: string;
            /**
             * Machine Name
             * @default
             */
            machine_name?: string;
            /**
             * Barcode
             * @default
             */
            barcode?: string;
            /**
             * Barcode Symbology
             * @default
             */
            barcode_symbology?: string;
            /** Template Name */
            template_name: string;
            /** Resource Schema */
            resource_schema?: {
                [key: string]: unknown;
            };
            /** Model */
            model?: {
                [key: string]: unknown;
            };
            /**
             * Icon Uri
             * @default
             */
            icon_uri?: string;
            /** Config */
            config?: {
                [key: string]: unknown;
            };
            /** Extra */
            extra?: {
                [key: string]: unknown;
            };
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
            /**
             * Lifecycle Status
             * @default active
             * @enum {string}
             */
            lifecycle_status?: "active" | "reserved" | "in_use" | "quarantined" | "consumed" | "retired";
            /** Template Uuid */
            template_uuid: string;
            /** Material Uuid */
            material_uuid: string;
            /**
             * Ordinal
             * @default 0
             */
            ordinal?: number;
            /** Created At Ms */
            created_at_ms: number;
            /** Updated At Ms */
            updated_at_ms: number;
            /** Deleted At Ms */
            deleted_at_ms?: number | null;
            /** Version */
            version: number;
        };
        /**
         * MaterialLinkUpsert
         * @description 拓扑边 upsert 请求：两端为已落权威的 material（含设备行）。
         */
        MaterialLinkUpsert: {
            /** Source Material Uuid */
            source_material_uuid: string;
            /** Target Material Uuid */
            target_material_uuid: string;
            /**
             * Link Type
             * @default
             */
            link_type?: string;
            /**
             * Source Handle
             * @default
             */
            source_handle?: string;
            /**
             * Target Handle
             * @default
             */
            target_handle?: string;
            /** Extra */
            extra?: {
                [key: string]: unknown;
            };
        };
        /** MaterialPosition */
        MaterialPosition: {
            /**
             * Size Depth
             * @default 0
             */
            size_depth?: number;
            /**
             * Size Width
             * @default 0
             */
            size_width?: number;
            /**
             * Size Height
             * @default 0
             */
            size_height?: number;
            /**
             * Scale X
             * @default 0
             */
            scale_x?: number;
            /**
             * Scale Y
             * @default 0
             */
            scale_y?: number;
            /**
             * Scale Z
             * @default 0
             */
            scale_z?: number;
            /**
             * Layout
             * @default x-y
             * @enum {string}
             */
            layout?: "2d" | "x-y" | "z-y" | "x-z";
            /** Position X */
            position_x?: number | null;
            /** Position Y */
            position_y?: number | null;
            /** Position Z */
            position_z?: number | null;
            /**
             * Position3D X
             * @default 0
             */
            position3d_x?: number;
            /**
             * Position3D Y
             * @default 0
             */
            position3d_y?: number;
            /**
             * Position3D Z
             * @default 0
             */
            position3d_z?: number;
            /**
             * Rotation X
             * @default 0
             */
            rotation_x?: number;
            /**
             * Rotation Y
             * @default 0
             */
            rotation_y?: number;
            /**
             * Rotation Z
             * @default 0
             */
            rotation_z?: number;
            /**
             * Cross Section Type
             * @default rectangle
             * @enum {string}
             */
            cross_section_type?: "rectangle" | "circle" | "rounded_rectangle";
            /** Extra */
            extra?: {
                [key: string]: unknown;
            };
        };
        /** MaterialSnapshot */
        MaterialSnapshot: {
            /** Root Material Uuid */
            root_material_uuid: string;
            /** Nodes */
            nodes: components["schemas"]["MaterialAggregateRead"][];
            /** State Hash */
            state_hash?: string | null;
        };
        /**
         * MaterialSubstance
         * @description 一份内容物。
         *
         *     ``chemical_uuid`` 引用化学品定义，写入时三种形态语义不同：
         *
         *     - **省略**（设备快照 / 增量、旧客户端）：沿用同一物料同名（不区分大小写）内容物已有的引用
         *       或"已解除"状态，都没有时按名称 / 英文名 / 别名 / CAS 唯一匹配自动关联；
         *     - **显式 null**：不关联，本次写入不自动匹配。对已关联的内容物这是**解除**：记为已解除，
         *       之后省略引用的写入（设备按名称上报）也不再自动关联，直到再次显式关联；
         *     - **显式 uuid**：必须指向未删除的化学品（该物料上已有的引用可原样保留），并清除"已解除"。
         *
         *     ``chemical`` 是读取时附带的只读摘要，写入时忽略，可带可不带；引用的化学品已删除时为
         *     null（uuid 保留）。``chemical_unlinked`` 是读取时附带的只读状态：为 true 表示该内容物被
         *     显式解除过关联、不会自动匹配；写入时忽略。读取时未关联的内容物不出现 ``chemical_uuid`` /
         *     ``chemical``，未解除过的不出现 ``chemical_unlinked``。
         *
         *     这些字段只在显式给出时才序列化：没有它们的旧载荷与新模型的完整 dump 逐字一致（payload
         *     完整性比对、请求哈希、state_hash 都不变），"省略"与"显式 null"也不会哈希成同一个请求。
         */
        MaterialSubstance: {
            /** Substance Uuid */
            substance_uuid?: string | null;
            /** Name */
            name: string;
            /** Quantity */
            quantity: number;
            /** Quantity Unit */
            quantity_unit: string;
            /**
             * Physical State
             * @default liquid
             * @enum {string}
             */
            physical_state?: "liquid" | "solid" | "gas" | "unknown";
            /** Composition */
            composition?: components["schemas"]["JsonValue"][];
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
            /** Chemical Uuid */
            chemical_uuid?: string | null;
            chemical?: components["schemas"]["ChemicalSummary"] | null;
            /**
             * Chemical Unlinked
             * @default false
             */
            chemical_unlinked?: boolean;
        };
        /** PayloadObjectRecord */
        PayloadObjectRecord: {
            /** Payload Uuid */
            payload_uuid: string;
            /** Media Type */
            media_type: string;
            /** Encoding */
            encoding: string;
            /** Compression */
            compression?: string | null;
            /** Byte Length */
            byte_length: number;
            /** Sha256 */
            sha256: string;
            /**
             * Storage Kind
             * @enum {string}
             */
            storage_kind: "inline" | "external";
            /** Inline Payload */
            inline_payload?: string | null;
            /** External Uri */
            external_uri?: string | null;
            /** Created At Ms */
            created_at_ms: number;
            /** Expires At Ms */
            expires_at_ms?: number | null;
        };
        /** ResetRequest */
        ResetRequest: {
            /** Confirmation Token */
            confirmation_token: string;
            /** Confirmation */
            confirmation: string;
        };
        /** ResetStatus */
        ResetStatus: {
            /** Supported */
            supported: boolean;
            /** Pending */
            pending: boolean;
            /** Confirmation Token */
            confirmation_token: string;
            /** Backup Path */
            backup_path: string;
            /** Detail */
            detail: string;
        };
        /**
         * ResourceTreeNotify
         * @description 前端在权威完成物料变更后，请求 edge hostnode 把变更分发到目标设备。
         */
        ResourceTreeNotify: {
            /**
             * Device Id
             * @description 目标边缘设备 id（可对应 slave edge 上的设备）
             */
            device_id: string;
            /**
             * Action
             * @default add
             * @enum {string}
             */
            action?: "add" | "update" | "remove";
            /** Resource Uuids */
            resource_uuids: string[];
        };
        /** ResourceTreeNotifyResult */
        ResourceTreeNotifyResult: {
            /**
             * Notified
             * @description True=设备已确认投影；False=通知失败；null=设备未注册被跳过
             */
            notified: boolean | null;
        };
        /**
         * RestartRequest
         * @description 安静点重启请求。
         *
         *     mode: quiescent 等执行端安静；immediate 跳过等待立即重启。
         *     scope: auto 按运行形态选择（调度权威进程 → edge，只重启 Host；其它 → process）；
         *     edge 只重启 Host 进程，调度权威与管理端口常驻；process 本进程整体重启。
         */
        RestartRequest: {
            /**
             * Mode
             * @default quiescent
             */
            mode?: string;
            /**
             * Scope
             * @default auto
             */
            scope?: string;
        };
        /** RuntimeLogBatch */
        RuntimeLogBatch: {
            /** Source Id */
            source_id: string;
            /** Stream Id */
            stream_id: string;
            /** Cursor */
            cursor: string;
            /** Lines */
            lines: components["schemas"]["RuntimeLogLine"][];
            /**
             * Has More
             * @default false
             */
            has_more?: boolean;
            /**
             * Reset
             * @default false
             */
            reset?: boolean;
            /**
             * Truncated
             * @default false
             */
            truncated?: boolean;
            /**
             * Path
             * @default
             */
            path?: string;
            /** Pid */
            pid?: number | null;
        };
        /** RuntimeLogLine */
        RuntimeLogLine: {
            /** Offset */
            offset: number;
            /** Text */
            text: string;
        };
        /** RuntimeLogSource */
        RuntimeLogSource: {
            /** Source Id */
            source_id: string;
            /** Name */
            name: string;
            /**
             * Role
             * @enum {string}
             */
            role: "host" | "slave";
            /** Machine Name */
            machine_name: string;
            /**
             * Node Id
             * @default
             */
            node_id?: string;
            /** Pid */
            pid?: number | null;
            /** Device Ids */
            device_ids?: string[];
            /** Online */
            online: boolean;
            /**
             * Managed
             * @default false
             */
            managed?: boolean;
            /**
             * Supported
             * @default true
             */
            supported?: boolean;
            /**
             * Detail
             * @default
             */
            detail?: string;
        };
        /** RuntimeLogSources */
        RuntimeLogSources: {
            /** Sources */
            sources: components["schemas"]["RuntimeLogSource"][];
        };
        /** SiteRead */
        SiteRead: {
            /** Site Uuid */
            site_uuid: string;
            /**
             * Schema Version
             * @default 1
             * @constant
             */
            schema_version?: 1;
            /** Template Name */
            template_name: string;
            /** Site Index */
            site_index: number | string;
            /** Label */
            label: string;
            /**
             * Visible
             * @default true
             */
            visible?: boolean;
            /** Occupied Material Uuid */
            occupied_material_uuid?: string | null;
            /** Pose */
            pose?: {
                [key: string]: unknown;
            };
            /** Allowed Resource Categories */
            allowed_resource_categories?: string[];
            /**
             * Parent Link
             * @default
             */
            parent_link?: string;
            /**
             * Description
             * @default
             */
            description?: string;
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
            /** Extra */
            extra?: {
                [key: string]: unknown;
            };
            /** Owner Material Uuid */
            owner_material_uuid: string;
            /**
             * Ordinal
             * @default 0
             */
            ordinal?: number;
            /** Changed By Job Uuid */
            changed_by_job_uuid?: string | null;
            /** Changed By Command Uuid */
            changed_by_command_uuid?: string | null;
            /** Changed At Ms */
            changed_at_ms: number;
            /** Created At Ms */
            created_at_ms: number;
            /** Updated At Ms */
            updated_at_ms: number;
            /** Deleted At Ms */
            deleted_at_ms?: number | null;
            /** Version */
            version: number;
        };
        /** StatusIncidentDecision */
        StatusIncidentDecision: {
            /**
             * Action
             * @default
             */
            action?: string;
            /** Option */
            option?: {
                [key: string]: unknown;
            } | null;
            /**
             * Reason
             * @default
             */
            reason?: string;
        };
        /**
         * TelemetryEventWrite
         * @description 由一个 endpoint 产生的、带稳定来源位置的事件。
         */
        TelemetryEventWrite: {
            /** Event Uuid */
            event_uuid: string;
            /** Endpoint Uuid */
            endpoint_uuid: string;
            /** Device Uuid */
            device_uuid?: string | null;
            /** Source Epoch */
            source_epoch: string;
            /** Source Generation */
            source_generation: number;
            /** Source Sequence */
            source_sequence: number;
            /**
             * Event Type
             * @enum {string}
             */
            event_type: "state" | "property_sample" | "connection" | "alarm";
            /** Event Key */
            event_key?: string | null;
            /** Payload */
            payload: unknown;
            /** Payload Hash */
            payload_hash?: string | null;
            /** Severity */
            severity?: string | null;
            /** Source Job Uuid */
            source_job_uuid?: string | null;
            /** Source Command Uuid */
            source_command_uuid?: string | null;
            /** Observed At Ms */
            observed_at_ms: number;
            /** Received At Ms */
            received_at_ms: number;
        };
        /** TelemetryIngestRequest */
        TelemetryIngestRequest: {
            /**
             * Protocol Version
             * @default telemetry.v1
             * @constant
             */
            protocol_version?: "telemetry.v1";
            event: components["schemas"]["TelemetryEventWrite"];
            device_state?: components["schemas"]["DeviceStateSnapshot"] | null;
        };
        /** ValidationError */
        ValidationError: {
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
            /** Input */
            input?: unknown;
            /** Context */
            ctx?: Record<string, never>;
        };
        /** WorkflowCreateRequest */
        WorkflowCreateRequest: {
            /** Name */
            name: string;
            /** Tags */
            tags?: unknown[];
            /** Description */
            description?: string | null;
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
            /** Workflow Uuid */
            workflow_uuid?: string | null;
        };
        /**
         * WorkflowEdgeWrite
         * @description Complete WorkflowEdge payload used by full-graph reconciliation.
         */
        WorkflowEdgeWrite: {
            /** Uuid */
            uuid: string;
            /** Source Node Uuid */
            source_node_uuid: string;
            /** Target Node Uuid */
            target_node_uuid: string;
            /** Source Handle Uuid */
            source_handle_uuid: string;
            /** Target Handle Uuid */
            target_handle_uuid: string;
            /** Description */
            description?: string | null;
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
        };
        /**
         * WorkflowFromTemplateRequest
         * @description 把注册表里的工作流模板（设备包 ``@workflow``）按角色绑定实例化成可运行的工作流。
         *
         *     ``bindings`` 是 ``{角色 id: device_id}``：设备角色缺省即其设备 id，类角色在物料
         *     权威里恰有一个该类设备时自动填充，否则必须显式给出。同一模板 + 同一组绑定
         *     反复调用幂等覆盖同一个工作流（脚本 / e2e 的"运行模板"入口）。
         */
        WorkflowFromTemplateRequest: {
            /** Template Uuid */
            template_uuid: string;
            /** Bindings */
            bindings?: {
                [key: string]: string;
            };
            /** Name */
            name?: string | null;
            /**
             * Site Binding Mode
             * @description resolve：程序化导入按目标物料解析 Site 标签；preserve：保留浏览器草稿，等待用户确认。
             * @default resolve
             * @enum {string}
             */
            site_binding_mode?: "resolve" | "preserve";
        };
        /**
         * WorkflowIODeclareRequest
         * @description 声明工作流公共输入输出（写入服务端保留的 ``meta_data.unilab``）。
         *
         *     ``node_input_bindings`` 形如 ``{node_uuid: {handle_uuid: {"parameter": 输入名}}}``，
         *     是完整替换：未列出的节点清空 input_bindings；被绑定输入在节点 param 中的静态值移除。
         *     ``node_params``（可选）按节点合并进 param，用于解绑必填输入时给回静态值。
         */
        WorkflowIODeclareRequest: {
            /** Revision */
            revision: number;
            /** Input Contract */
            input_contract?: {
                [key: string]: unknown;
            } | null;
            /** Output Contract */
            output_contract?: {
                [key: string]: unknown;
            } | null;
            /** Output Bindings */
            output_bindings?: {
                [key: string]: unknown;
            };
            /** Node Input Bindings */
            node_input_bindings?: {
                [key: string]: {
                    [key: string]: unknown;
                };
            };
            /** Node Params */
            node_params?: {
                [key: string]: {
                    [key: string]: unknown;
                };
            };
        };
        /**
         * WorkflowNodeWrite
         * @description Complete WorkflowNode payload used by full-graph reconciliation.
         */
        WorkflowNodeWrite: {
            /** Uuid */
            uuid: string;
            /** Workflow Node Template Uuid */
            workflow_node_template_uuid?: string | null;
            /** Parent Uuid */
            parent_uuid?: string | null;
            /** Material Uuid */
            material_uuid?: string | null;
            /** Name */
            name: string;
            /**
             * Status
             * @default idle
             */
            status?: string;
            /** Type */
            type: string;
            /** Icon */
            icon?: string | null;
            /** Pose */
            pose?: {
                [key: string]: unknown;
            };
            /** Param */
            param?: {
                [key: string]: unknown;
            } | null;
            /** Footer */
            footer?: string | null;
            /** Action Name */
            action_name?: string | null;
            /** Action Type */
            action_type?: string | null;
            /** Execution Policy */
            execution_policy?: {
                [key: string]: unknown;
            };
            /**
             * Disabled
             * @default false
             */
            disabled?: boolean;
            /**
             * Minimized
             * @default false
             */
            minimized?: boolean;
            /** Script */
            script?: string | null;
            /** Description */
            description?: string | null;
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
        };
        /** WorkflowPreflightRequest */
        WorkflowPreflightRequest: {
            /**
             * Run Mode
             * @default normal
             * @enum {string}
             */
            run_mode?: "normal" | "step" | "single_node";
            /** Target Node Uuid */
            target_node_uuid?: string | null;
            /** Input */
            input?: {
                [key: string]: unknown;
            };
            /** Expected Revision */
            expected_revision?: number | null;
        };
        /**
         * WorkflowPublishRequest
         * @description 发布当前修订；revision 必须等于工作流当前 revision（重复发布同一修订幂等）。
         */
        WorkflowPublishRequest: {
            /** Revision */
            revision: number;
        };
        /**
         * WorkflowTaskCommandRequest
         * @description step 放行一个动作；resume 切回自动；pause 转单步并停止派发新动作（已下发的照常跑完）；
         *     cancel 取消任务：不再派发，正在执行的动作收到取消请求，设备真实返回后任务落 canceled
         *     （期间任务为 canceling，设备锁保持）。
         *
         *     版本与幂等键防止跨页面重复放行。
         */
        WorkflowTaskCommandRequest: {
            /**
             * Type
             * @enum {string}
             */
            type: "step" | "resume" | "pause" | "cancel";
            /** Expected Revision */
            expected_revision: number;
            /** Idempotency Key */
            idempotency_key: string;
        };
        /**
         * WorkflowTaskCreateRequest
         * @description 整图运行与单点设备动作共用的提交体。
         *
         *     execution_kind=workflow（默认）：workflow_uuid 必填，走整图编排。
         *     execution_kind=ad_hoc_device_action：device_id + action_name + param 必填，
         *     生成单 job 任务（微前端设备页/画布单点动作）。
         *
         *     idempotency_key 两种任务都可选：同键同语义的重复提交返回既有任务，同键不同语义按冲突拒绝。
         */
        WorkflowTaskCreateRequest: {
            /**
             * Execution Kind
             * @default workflow
             */
            execution_kind?: string;
            /**
             * Workflow Uuid
             * @default
             */
            workflow_uuid?: string;
            /** Expected Revision */
            expected_revision?: number | null;
            /**
             * Run Mode
             * @default normal
             */
            run_mode?: string;
            /** Target Node Uuid */
            target_node_uuid?: string | null;
            /** Description */
            description?: string | null;
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
            /**
             * Device Id
             * @default
             */
            device_id?: string;
            /**
             * Action Name
             * @default
             */
            action_name?: string;
            /**
             * Action Type
             * @default
             */
            action_type?: string;
            /** Param */
            param?: {
                [key: string]: unknown;
            };
            /** Execution Policy */
            execution_policy?: {
                [key: string]: unknown;
            };
            /**
             * Execution Timeout Seconds
             * @default 0
             */
            execution_timeout_seconds?: number;
            /** Idempotency Key */
            idempotency_key?: string | null;
            /** Input */
            input?: {
                [key: string]: unknown;
            };
        };
        /**
         * WorkflowTaskInputEditRequest
         * @description 暂停时修改入口参数：``input`` 为部分更新，``expected_revision`` 是任务 control_revision。
         */
        WorkflowTaskInputEditRequest: {
            /** Expected Revision */
            expected_revision: number;
            /** Input */
            input: {
                [key: string]: unknown;
            };
            /** Idempotency Key */
            idempotency_key?: string | null;
        };
        /** WorkflowUpdateRequest */
        WorkflowUpdateRequest: {
            /** Name */
            name: string;
            /** Tags */
            tags?: unknown[];
            /** Description */
            description?: string | null;
            /** Meta Data */
            meta_data?: {
                [key: string]: unknown;
            };
            /** Workflow Uuid */
            workflow_uuid?: string | null;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    list_chemicals_api_v1_chemicals_get: {
        parameters: {
            query?: {
                q?: string;
                cas?: string;
                inchi_key?: string;
                physical_state?: string;
                page?: number;
                page_size?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_chemical_api_v1_chemicals_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ChemicalWrite"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_chemical_api_v1_chemicals__chemical_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                chemical_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_chemical_api_v1_chemicals__chemical_uuid__delete: {
        parameters: {
            query: {
                expected_version: number;
            };
            header?: never;
            path: {
                chemical_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_chemical_api_v1_chemicals__chemical_uuid__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                chemical_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ChemicalPatch"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_databases_api_v1_debug_databases_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    browse_table_api_v1_debug_databases__database__tables__table__get: {
        parameters: {
            query?: {
                limit?: number;
                offset?: number;
                /** @description 排序列名（默认按 rowid 倒序，即最新写入在前） */
                order?: string | null;
                descending?: boolean;
            };
            header?: never;
            path: {
                database: string;
                table: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_processes_api_v1_device_processes_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    create_api_v1_device_processes_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DeviceProcessWrite"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    device_classes_api_v1_device_processes_device_classes_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    }[];
                };
            };
        };
    };
    get_api_v1_device_processes__process_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                process_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_api_v1_device_processes__process_id__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                process_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DeviceProcessWrite"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_api_v1_device_processes__process_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                process_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    restart_api_v1_device_processes__process_id__restart_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                process_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    start_api_v1_device_processes__process_id__start_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                process_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    stop_api_v1_device_processes__process_id__stop_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                process_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    inventory_api_v1_driver_packages_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    catalog_api_v1_driver_packages_catalog_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    install_api_v1_driver_packages_install_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DriverPackageInstallRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    operations_api_v1_driver_packages_operations_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    }[];
                };
            };
        };
    };
    operation_api_v1_driver_packages_operations__operation_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                operation_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    uninstall_api_v1_driver_packages__name__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    set_enabled_api_v1_driver_packages__name__enabled_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DriverPackageEnableRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    graphs_api_v1_driver_packages__name__graphs_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    }[];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    graph_api_v1_driver_packages__name__graphs__graph_name__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
                graph_name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    launch_api_v1_driver_packages__name__graphs__graph_name__launch_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
                graph_name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    error_decisions_api_v1_error_decisions_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    resolve_error_decision_api_v1_error_decisions__decision_id__post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                decision_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ErrorDecision"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    runtime_events_api_v1_events_get: {
        parameters: {
            query?: never;
            header?: {
                /** @description 不填时只接收新通知；填 0 回放保留的历史，其他序号从其之后续传。 */
                "Last-Event-ID"?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_graphs_api_v1_graphs_get: {
        parameters: {
            query?: {
                page?: number;
                page_size?: number;
                name?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    upsert_graph_api_v1_graphs_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["GraphUpsertRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_live_payload_api_v1_graphs_live_payload_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_graph_api_v1_graphs__identity__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identity: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_graph_api_v1_graphs__identity__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identity: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_graph_payload_api_v1_graphs__identity__payload_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                identity: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    health_api_v1_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: string;
                    };
                };
            };
        };
    };
    query_events_api_v1_history_events_get: {
        parameters: {
            query?: {
                after_sequence?: number;
                limit?: number;
                event_types?: ("job_transition" | "action_availability" | "job_feedback" | "job_result" | "job_log" | "error_snapshot" | "decision_audit")[];
                job_uuid?: string | null;
                endpoint_uuid?: string | null;
                device_uuid?: string | null;
                event_key?: string | null;
                occurred_from_ms?: number | null;
                occurred_through_ms?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HistoryEventRecord"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    append_event_api_v1_history_events_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["HistoryEventAppend"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HistoryEventRecord"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_event_api_v1_history_events__event_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                event_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HistoryEventRecord"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    append_replacement_api_v1_history_events__event_uuid__replacement_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                event_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ManualResultReplacement"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HistoryEventRecord"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    replacement_chain_api_v1_history_events__event_uuid__replacement_chain_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                event_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HistoryEventRecord"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    store_payload_api_v1_history_payloads_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InlinePayloadWrite"] | components["schemas"]["ExternalPayloadWrite"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PayloadObjectRecord"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_payload_api_v1_history_payloads__payload_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                payload_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PayloadObjectRecord"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    sources_api_v1_hostlink_log_sources_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RuntimeLogSources"];
                };
            };
        };
    };
    logs_api_v1_hostlink_logs_get: {
        parameters: {
            query: {
                source_id: string;
                cursor?: string;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RuntimeLogBatch"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    hostlink_peers_api_v1_hostlink_peers_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    get_layout_api_v1_lab_layout_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LabLayoutRead"];
                };
            };
        };
    };
    put_layout_api_v1_lab_layout_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LabLayoutWrite"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LabLayoutRead"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reset_layout_api_v1_lab_layout_delete: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    changes_api_v1_materials_changes_get: {
        parameters: {
            query?: {
                after_sequence?: number;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    acknowledge_changes_api_v1_materials_changes_ack_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LedgerAcknowledge"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    events_api_v1_materials_events_get: {
        parameters: {
            query?: never;
            header?: {
                "Last-Event-ID"?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_materials_api_v1_materials_instances_get: {
        parameters: {
            query?: {
                roots_only?: boolean;
                /** @description 按名称精确搜索；未命中返回 [] */
                name?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_material_by_resource_id_api_v1_materials_instances_by_resource_id__resource_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                resource_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_material_api_v1_materials_instances__material_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                material_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_material_api_v1_materials_instances__material_uuid__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                material_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    patch_material_api_v1_materials_instances__material_uuid__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                material_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    put_data_api_v1_materials_instances__material_uuid__data_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                material_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    put_position_api_v1_materials_instances__material_uuid__position_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                material_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_tree_api_v1_materials_instances__material_uuid__tree_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                material_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    instantiate_material_api_v1_materials_instantiate_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    preview_inventory_api_v1_materials_inventory_preview_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryPreviewRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_links_api_v1_materials_links_get: {
        parameters: {
            query?: {
                material_uuid?: string;
                source_material_uuid?: string;
                target_material_uuid?: string;
                link_type?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    upsert_link_api_v1_materials_links_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MaterialLinkUpsert"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_link_api_v1_materials_links__link_uuid__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                link_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_inventory_lots_api_v1_materials_lots_get: {
        parameters: {
            query?: {
                template_uuid?: string | null;
                unit?: string | null;
                include_quarantined?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    inbound_inventory_lot_api_v1_materials_lots_inbound_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_inventory_lot_api_v1_materials_lots__lot_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                lot_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    stocktake_inventory_lot_api_v1_materials_lots__lot_uuid__stocktake_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                lot_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    move_material_api_v1_materials_move_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    notify_device_api_v1_materials_notify_device_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ResourceTreeNotify"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ResourceTreeNotifyResult"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_registry_classes_api_v1_materials_registry_classes_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    list_inventory_reservations_api_v1_materials_reservations_get: {
        parameters: {
            query?: {
                task_uuid?: string | null;
                status?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reserve_inventory_api_v1_materials_reservations_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reserve_task_inventory_api_v1_materials_reservations_batch_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_inventory_reservation_by_job_api_v1_materials_reservations_by_job__job_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_inventory_reservation_api_v1_materials_reservations__reservation_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                reservation_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    consume_inventory_reservation_api_v1_materials_reservations__reservation_uuid__consume_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                reservation_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    quarantine_inventory_reservation_api_v1_materials_reservations__reservation_uuid__quarantine_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                reservation_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    release_inventory_reservation_api_v1_materials_reservations__reservation_uuid__release_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                reservation_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    apply_snapshot_api_v1_materials_snapshots_apply_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    compare_snapshot_api_v1_materials_snapshots_compare_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MaterialSnapshot"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    apply_delta_api_v1_materials_snapshots_delta_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_templates_api_v1_materials_templates_get: {
        parameters: {
            query?: {
                /** @description 按模板 name 精确筛选：存在性检查 / 按名取 uuid 只回 0 或 1 条。 */
                name?: string | null;
                /** @description 默认只返回名称 / uuid / 类型 / 分类 / 位点 / 版本 / definition_hash 等目录字段（前端选择器、存在性检查、变更判定够用）；true 时附带 registry 全量 definition（全注册表可达十几 MB），只在确实要读 definition 正文时开。 */
                include_definition?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_template_api_v1_materials_templates_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_template_api_v1_materials_templates__template_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    put_template_api_v1_materials_templates__template_uuid__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_template_api_v1_materials_templates__template_uuid__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                template_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    transfer_material_api_v1_materials_transfer_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_tree_api_v1_materials_trees_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryMutation"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    ping_api_v1_ping_get: {
        parameters: {
            query?: {
                client_timestamp?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    readiness_api_v1_readiness_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_registry_digest_api_v1_registry_digest_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    list_registry_entries_api_v1_registry_entries_get: {
        parameters: {
            query?: {
                status?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_registry_entry_api_v1_registry_entries__name__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    apply_registry_entry_api_v1_registry_entries__name__apply_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    dismiss_registry_entry_api_v1_registry_entries__name__dismiss_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    restore_registry_entry_api_v1_registry_entries__name__restore__version__post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
                version: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_registry_entry_versions_api_v1_registry_entries__name__versions_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_registry_entry_version_api_v1_registry_entries__name__versions__version__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
                version: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_registry_pending_impacts_api_v1_registry_pending_impacts_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    list_registry_reports_api_v1_registry_reports_get: {
        parameters: {
            query?: {
                page?: number;
                page_size?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_registry_workflow_templates_api_v1_registry_workflow_templates_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    reset_preview_api_v1_reset_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ResetStatus"];
                };
            };
        };
    };
    reset_request_api_v1_reset_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ResetRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ResetStatus"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    report_resource_templates_api_v1_resource_templates_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    restart_status_api_v1_restart_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    request_restart_api_v1_restart_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RestartRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    cancel_restart_api_v1_restart_delete: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    list_adapter_commands_api_v1_runtime_adapter_commands_get: {
        parameters: {
            query?: {
                endpoint_uuid?: string | null;
                status?: string | null;
                job_uuid?: string | null;
                after_sequence?: number;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    enqueue_adapter_command_api_v1_runtime_adapter_commands_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AdapterCommandEnqueue"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    acknowledge_adapter_command_api_v1_runtime_adapter_commands_ack_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AdapterCommandAck"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    claim_adapter_commands_api_v1_runtime_adapter_commands_claim_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AdapterCommandClaim"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_adapter_command_api_v1_runtime_adapter_commands__adapter_command_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                adapter_command_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_backend_events_api_v1_runtime_backend_events_get: {
        parameters: {
            query?: {
                status?: string | null;
                job_uuid?: string | null;
                aggregate_type?: string | null;
                aggregate_uuid?: string | null;
                after_sequence?: number;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    enqueue_backend_event_api_v1_runtime_backend_events_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BackendEventEnqueue"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    acknowledge_backend_events_api_v1_runtime_backend_events_ack_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BackendEventAck"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    claim_backend_events_api_v1_runtime_backend_events_claim_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BackendEventClaim"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_backend_event_api_v1_runtime_backend_events__event_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                event_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_commands_api_v1_runtime_commands_get: {
        parameters: {
            query?: {
                session_uuid?: string | null;
                status?: string | null;
                job_uuid?: string | null;
                command_type?: string | null;
                after_sequence?: number;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    receive_command_api_v1_runtime_commands_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CommandEnvelope"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_command_api_v1_runtime_commands__command_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                command_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_endpoint_snapshots_api_v1_runtime_endpoints_get: {
        parameters: {
            query?: {
                transport?: string | null;
                state?: string | null;
                host_uuid?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_endpoint_snapshot_api_v1_runtime_endpoints__endpoint_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                endpoint_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    upsert_endpoint_snapshot_api_v1_runtime_endpoints__endpoint_uuid__snapshot_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                endpoint_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EndpointSnapshotUpsert"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_execution_jobs_api_v1_runtime_jobs_get: {
        parameters: {
            query?: {
                status?: string | null;
                device_uuid?: string | null;
                endpoint_uuid?: string | null;
                retry_of_job_uuid?: string | null;
                attempt_group_uuid?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_execution_job_api_v1_runtime_jobs_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ExecutionJobCreate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_execution_job_api_v1_runtime_jobs__job_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    request_execution_cancel_api_v1_runtime_jobs__job_uuid__cancel_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ExecutionJobCancel"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    decide_error_gate_api_v1_runtime_jobs__job_uuid__error_gate_decision_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ErrorGateDecision"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    open_error_gate_api_v1_runtime_jobs__job_uuid__error_gate_open_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ErrorGateOpen"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    record_execution_feedback_api_v1_runtime_jobs__job_uuid__feedback_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ExecutionJobFeedback"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    transition_execution_job_api_v1_runtime_jobs__job_uuid__transitions_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ExecutionJobTransition"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_backend_sessions_api_v1_runtime_sessions_get: {
        parameters: {
            query?: {
                edge_uuid?: string | null;
                state?: string | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_backend_session_api_v1_runtime_sessions__session_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    upsert_backend_session_api_v1_runtime_sessions__session_uuid__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BackendSessionUpsert"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    scheduler_resources_api_v1_scheduler_resources_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
        };
    };
    status_incidents_api_v1_status_incidents_get: {
        parameters: {
            query?: {
                device_id?: string;
                include_terminal?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    decide_status_incident_api_v1_status_incidents__incident_id__post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                incident_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["StatusIncidentDecision"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    query_events_api_v1_telemetry_events_get: {
        parameters: {
            query?: {
                after_sequence?: number;
                endpoint_uuid?: string | null;
                device_uuid?: string | null;
                event_type?: ("state" | "property_sample" | "connection" | "alarm") | null;
                source_epoch?: string | null;
                source_generation?: number | null;
                observed_from_ms?: number | null;
                observed_to_ms?: number | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    ingest_api_v1_telemetry_events_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TelemetryIngestRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_event_api_v1_telemetry_events__event_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                event_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_source_cursor_api_v1_telemetry_sources__endpoint_uuid__cursor_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                endpoint_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_device_states_api_v1_telemetry_states_get: {
        parameters: {
            query?: {
                endpoint_uuid?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_device_state_api_v1_telemetry_states__endpoint_uuid___device_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                endpoint_uuid: string;
                device_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_manual_confirmation_api_v1_workflow_manual_confirmations__confirmation_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                confirmation_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    decide_manual_confirmation_api_v1_workflow_manual_confirmations__confirmation_uuid__decision_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                confirmation_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ManualConfirmationDecisionRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_workflow_node_job_api_v1_workflow_node_jobs__job_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_node_job_feedback_history_api_v1_workflow_node_jobs__job_uuid__feedback_history_get: {
        parameters: {
            query?: {
                limit?: number | null;
                offset?: number;
            };
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_node_job_results_api_v1_workflow_node_jobs__job_uuid__results_get: {
        parameters: {
            query?: {
                limit?: number | null;
                offset?: number;
            };
            header?: never;
            path: {
                job_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_workflow_node_run_api_v1_workflow_node_runs__run_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                run_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_published_workflows_api_v1_workflow_publications_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    list_workflow_tasks_api_v1_workflow_tasks_get: {
        parameters: {
            query?: {
                page?: number;
                page_size?: number;
                workflow_uuid?: string | null;
                status?: string;
                cleanup_status?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_workflow_task_api_v1_workflow_tasks_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowTaskCreateRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_workflow_task_api_v1_workflow_tasks__task_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    command_workflow_task_api_v1_workflow_tasks__task_uuid__commands_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowTaskCommandRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    edit_workflow_task_input_api_v1_workflow_tasks__task_uuid__input_patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowTaskInputEditRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_task_interventions_api_v1_workflow_tasks__task_uuid__interventions_get: {
        parameters: {
            query?: {
                limit?: number | null;
                offset?: number;
            };
            header?: never;
            path: {
                task_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_workflow_node_jobs_api_v1_workflow_tasks__task_uuid__jobs_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_task_manual_confirmations_api_v1_workflow_tasks__task_uuid__manual_confirmations_get: {
        parameters: {
            query?: {
                limit?: number | null;
                offset?: number;
            };
            header?: never;
            path: {
                task_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    decide_task_manual_confirmation_api_v1_workflow_tasks__task_uuid__manual_confirmations__confirmation_uuid__decision_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_uuid: string;
                confirmation_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ManualConfirmationDecisionRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_workflow_node_runs_api_v1_workflow_tasks__task_uuid__node_runs_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_workflows_api_v1_workflows_get: {
        parameters: {
            query?: {
                page?: number;
                page_size?: number;
                name?: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_workflow_api_v1_workflows_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowCreateRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_workflow_from_template_api_v1_workflows_from_template_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowFromTemplateRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_workflow_api_v1_workflows__workflow_uuid__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_workflow_api_v1_workflows__workflow_uuid__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowUpdateRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_workflow_api_v1_workflows__workflow_uuid__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_authoring_api_v1_workflows__workflow_uuid__authoring_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    apply_authoring_api_v1_workflows__workflow_uuid__authoring_apply_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ApplyRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    save_draft_api_v1_workflows__workflow_uuid__authoring_draft_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DraftWriteRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    insert_composite_invocation_api_v1_workflows__workflow_uuid__composite_invocations_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CompositeInvocationRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_graph_api_v1_workflows__workflow_uuid__graph_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    save_graph_api_v1_workflows__workflow_uuid__graph_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["GraphWriteRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    declare_workflow_io_api_v1_workflows__workflow_uuid__io_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowIODeclareRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_workflow_publications_api_v1_workflows__workflow_uuid__publications_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    publish_workflow_api_v1_workflows__workflow_uuid__publications_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowPublishRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    preflight_workflow_api_v1_workflows__workflow_uuid__run_preflight_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                workflow_uuid: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["WorkflowPreflightRequest"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
}
