# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the CyberFinger Fusion Studio
# Build with: build_fusion_studio.bat   (or: pyinstaller fusion_studio.spec)
#
# A folder build: dist\CyberFingerFusionStudio\CyberFingerFusionStudio.exe + its _internal folder. With numpy,
# matplotlib, the MindRove SDK and OpenXR a one-file exe would unpack ~200 MB on every start, and the Studio keeps its
# models next to its modules (the refined EMG posture model is saved there).
# The same exe is the OpenXR helper process (--openxr-provider) and has a self-test (--selftest [report.txt]).

import os
import pkgutil

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

datas, binaries, hiddenimports = [], [], []

# Native libraries loaded at run time: the MindRove SDK (BoardController DLLs), the OpenXR loader (pyopenxr = 'xr')
# and GLFW (the helper's offscreen OpenGL context)
for pkg in ('mindrove', 'xr', 'glfw'):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# pystray backends and every winrt namespace package (BLE), as in cyberfinger_bridge.spec
hiddenimports += collect_submodules('pystray')
winrt_hiddenimports = ['winrt', 'winrt._winrt', 'winrt.system']
try:
    import winrt
    for _importer, modname, _ispkg in pkgutil.walk_packages(winrt.__path__, prefix='winrt.'):
        winrt_hiddenimports.append(modname)
except Exception:
    pass
for m in ('winrt.windows.devices.bluetooth', 'winrt.windows.devices.bluetooth.genericattributeprofile',
          'winrt.windows.devices.enumeration', 'winrt.windows.storage.streams', 'winrt.windows.foundation',
          'winrt.windows.foundation.collections'):
    if m not in winrt_hiddenimports:
        winrt_hiddenimports.append(m)

# The Studio's own modules: most are imported inside try/except or lazily, so PyInstaller must be told about them
# (a missing one would silently switch a feature off; --selftest checks they all load)
studio_modules = [
    'openxr_skeleton', 'openxr_skeleton_provider', 'fusion_studio_selftest',
    'key_postures', 'transition_events', 'finger_fusion', 'steamvr_link', 'mount_calib', 'hybrid_position',
    'gesture_skeleton', 'armband_panel', 'emg_dashboard', 'hand_avatar',
    'gate', 'extrinsic', 'orient_fusion', 'occlusion',
    'cf_protocol', 'cyberfinger_report', 'cyberfinger_control', 'pink_button', 'driver_link', 'haptics_view',
    'matplotlib.backends.backend_tkagg', 'PIL.Image', 'PIL.ImageDraw',
    'xr.utils.gl', 'xr.utils.gl.glfw_util', 'OpenGL.GL', 'OpenGL.platform.win32',
]

# Models and assets, at the paths the modules look for them (next to themselves)
model_files = ['key_postures_model2.npz', 'transition_model.npz', 'posture_pose_model.npz', 'hand_prior.npz',
               'hybrid_position_weights.npz']
studio_datas = [(f, '.') for f in model_files] + [
    ('armband/hand3d.html', 'armband'), ('armband/gesture_model.npz', 'armband'), ('armband/hand_calib.npz', 'armband'),
    ('armband/hand3d.html', '.'),                    # emg_dashboard serves it from its own folder
    ('assets/icon_32x32.png', 'assets'), ('assets/icon_32x32_bw.png', 'assets'), ('assets/icon.png', 'assets'),
]

a = Analysis(
    ['fusion_studio.py'],
    pathex=['armband', os.path.join('..', 'fusion')],
    binaries=binaries,
    datas=studio_datas + datas,
    hiddenimports=studio_modules + hiddenimports + winrt_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['vgamepad', 'pythonosc'],              # gamepad / VRChat OSC modes are not part of the Studio
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CyberFingerFusionStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                                       # UPX can break the OpenGL / SDK DLLs
    console=False,                                   # No console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',                          # Generated from PNG during build
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CyberFingerFusionStudio',
)
