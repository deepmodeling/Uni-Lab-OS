

def register():
    # noinspection PyUnresolvedReferences
    from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300Deck
    # noinspection PyUnresolvedReferences
    from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300Plate
    from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300PlateAdapter
    from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300TipRack
    from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300Trash
    from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300TubeRack
    # noinspection PyUnresolvedReferences
    from unilabos.devices.workstation.workstation_base import WorkStationContainer

    from unilabos.devices.liquid_handling.laiyu.laiyu import TransformXYZDeck
    from unilabos.devices.liquid_handling.laiyu.laiyu import TransformXYZContainer

    from unilabos.devices.liquid_handling.rviz_backend import UniLiquidHandlerRvizBackend
    from unilabos.devices.liquid_handling.laiyu.backend.laiyu_v_backend import UniLiquidHandlerLaiyuBackend

