# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Code shared by the Windows and Linux CyberFinger bridges.

Everything above the platform transports lives here — the BLE report format,
SlimeVR emulation, the OpenVR skeleton client, the driver stream and the
visualisation panels. The two GUIs (bridge/cyberfinger_gui.py and
bridge_linux/cyberfinger_gui_linux.py) keep only what is genuinely
platform-specific: BLE (WinRT vs bleak/BlueZ), the virtual gamepad (ViGEm vs
uinput), tray icon loading and the app shell.

Platform differences that shared code needs are isolated in platform.py, so no
module here branches on sys.platform except that one.
"""
