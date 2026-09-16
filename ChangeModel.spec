# -*- mode: python ; coding: utf-8 -*-
# Сборка: Windows -> ChangeModel.exe (onefile), macOS -> ChangeModel.app (bundle).
# Версия приложения берётся из APP_VERSION в gui.py (единый источник).
import os
import sys

from PyInstaller.utils.hooks import collect_all

# В exe встраивается ШАБЛОН: рабочий providers.json создаётся рядом с exe
# из providers.example.json при первом запуске (gui.ensure_data_file).
datas = [('providers.example.json', '.')]
binaries = []
hiddenimports = ['proxy.app', 'models_data', 'generator']
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Иконка необязательна: в репозиторий бинарный файл не входит,
# сборка без него проходит (будет стандартная иконка).
ICON = 'assets/changemodel.ico' if os.path.exists('assets/changemodel.ico') else None


def _app_version() -> str:
    import re
    try:
        src = os.path.join(SPECPATH, 'gui.py')
        with open(src, encoding='utf-8') as f:
            m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', f.read())
        if m:
            return m.group(1)
    except OSError:
        pass
    return '0.0.0'


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if sys.platform == 'darwin':
    # macOS: onedir + .app bundle (для bundle onefile-режим не подходит).
    # UPX выключен: на раннерах его обычно нет, а DMG и так сжимает.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='ChangeModel',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name='ChangeModel',
    )
    app = BUNDLE(
        coll,
        name='ChangeModel.app',
        icon=None,
        bundle_identifier='com.lana-info.changemodel',
        info_plist={
            'CFBundleName': 'ChangeModel',
            'CFBundleShortVersionString': _app_version(),
            'NSHighResolutionCapable': 'True',
        },
    )
else:
    # Windows: портативный onefile exe.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='ChangeModel',
        icon=ICON,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
