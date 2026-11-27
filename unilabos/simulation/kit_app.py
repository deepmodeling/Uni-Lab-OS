"""OS 拥有的无渲染 Kit 生命周期，供 Isaac 物理与运动学 worker 共用。"""
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def cpu_kit(experience, output, *, settings=(), forbidden_extensions=()):
    import carb
    import omni.kit.app
    framework = carb.get_framework()
    framework.load_plugins(loaded_file_wildcards=['omni.kit.app.plugin'],
        search_paths=[os.environ['CARB_APP_PATH'] + '/kernel/plugins'])
    app = omni.kit.app.get_app()
    try:
        app.startup('unilab-simulation', os.environ['CARB_APP_PATH'], [
            __file__, str(experience), '--no-window', '--portable',
            '--portable-root', str(Path(output)/'kit'),
            '--/app/renderer/enabled=false', '--/renderer/enabled=false',
            '--/app/settings/persistent=false', '--/app/installSignalHandlers=0',
            *settings])
        manager = app.get_extension_manager()
        forbidden = ('omni.hydra.rtx', 'omni.kit.renderer.core',
                     'omni.kit.viewport.window', *forbidden_extensions)
        if any(manager.get_enabled_extension_id(name) for name in forbidden):
            raise RuntimeError('CPU worker enabled a forbidden extension')
        yield app
    finally:
        try:
            app.shutdown()
        finally:
            framework.unload_all_plugins()
