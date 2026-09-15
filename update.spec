# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 - 独立更新助手 update.exe
入口:updater_app/updater_main.py
特点:
    1. 与主程序完全独立的 onedir windowed 程序(无控制台,自更新全链路零黑窗)
       onedir 模式:文件直接落在 dist/update/ 目录,运行时零解包,启动速度远快于 onefile
    2. 只打包更新助手实际依赖:PySide6基础组件 + service/infrastructure + updater_app.model.config_manager
    3. 产物 dist/update/,随主程序发布在同一软件目录(整个 update 文件夹拷贝即可)
    4. upx=False:Qt 等大 DLL 用 UPX 压缩得不偿失,关闭后启动更快
"""

block_cipher = None

a = Analysis(
    ['updater_app/updater_main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('app_icon.png', '.'),
    ],
    hiddenimports=[
        'PySide6.QtWidgets',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtNetwork',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 更新助手不需要的 Qt 模块
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtQuick3D',
        'PySide6.Qt3DCore',
        'PySide6.Qt3DRender',
        'PySide6.QtBluetooth',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'PySide6.QtDBus',
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        'PySide6.QtNetworkAuth',
        'PySide6.QtNfc',
        'PySide6.QtOpenGL',
        'PySide6.QtOpenGLWidgets',
        'PySide6.QtPdf',
        'PySide6.QtPositioning',
        'PySide6.QtPrintSupport',
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtQuickWidgets',
        'PySide6.QtSensors',
        'PySide6.QtSerialPort',
        'PySide6.QtSql',
        'PySide6.QtSvg',
        'PySide6.QtTest',
        'PySide6.QtWebChannel',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebSockets',
        'PySide6.QtXml',
        # 主程序业务/界面模块不参与更新助手打包
        'runtime',
        'main',
        'tkinter',
        'unittest',
        'pydoc',
        'doctest',
        'pdb',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# onedir 模式:EXE 不内嵌二进制,由 COLLECT 统一收集到 dist/update/ 目录
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='update',
    contents_directory='lib',  # onedir依赖目录由默认_internal改为更直观的lib
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
    icon='app_icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='update',
)
