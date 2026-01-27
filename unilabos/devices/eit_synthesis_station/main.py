from .manager.station_manager import SynthesisStationManager
from .config.setting import Settings
from pathlib import Path
import json

if __name__ == "__main__":

    # 测试代码

    settings = Settings.from_env()
    manager = SynthesisStationManager(settings)

    #---------------提交任务流程-------------------

    # 0. 设定文件名称
    ROOT = Path(__file__).resolve().parent
    task_tpl = ROOT / "sheet" / "task_reaction.xlsx"
    chem_db = ROOT / "sheet" / "chemical_list.xlsx"
    template_in = ROOT / "sheet" / "batch_in_tray.xlsx"


    # 1. 设备初始化
    # manager.device_init()

    # 2. 工站化学品库和本地化学品库数据对齐
    # manager.align_chemicals_with_file(chem_db)

    # 3. 上传任务到工站
    # manager.create_task_by_file(str(task_tpl), str(chem_db))

    # 4. 进行物料核算
    # manager.check_resource_for_task(str(task_tpl), str(chem_db))

    # # 5. 上料
    # manager.batch_in_tray_by_file(str(template_in))

    # 6. 开始任务
    manager.start_task(675)

    # 7. 等待任务完成并回传执行信息
    manager.wait_task_with_ops()

    # 8. 查询任务物料，并执行下料操作, 同时下料空托盘
    manager.batch_out_task_and_empty_trays()

    #---------------工站状态查询-------------------

    # 1. 查询站内所有物料信息
    # manager.get_resource_info()

    # 2. 查询站内所有设设备状态
    # manager.list_device_status()

    # 3. 查询工站运行状态
    # manager.station_state()

    # 4. 查询手套箱状态
    # manager.get_glovebox_env()
            
    # 下料
    # manager.batch_out_tray(layout_list=[{"layout_code": "T-1-2", "dst_layout_code": "TB-2-2"}])


    
    #————————————————额外功能————————————————————

    # 2. 设备初始化
    # manager.device_init()

    # # 获取站内所有化学品信息,导出到csv文件
    # manager.export_chemical_list_to_file("chemicals_list_export.csv")

    # # 通过csv进行化学品录入
    # manager.sync_chemicals_from_file("add_chemical_list.csv")

    # 本地化学品库去重整理
    # manager.deduplicate_chemical_library_by_file(chem_db)

    # 本地化学品库数据完整性检验
    # manager.check_chemical_library_by_file(chem_db)