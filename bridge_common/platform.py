# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""The one place shared code is allowed to branch on the host OS.

Everything else in bridge_common asks here instead of testing sys.platform
itself, so adding a third platform means editing this file and nothing else.
"""

import os
import sys

IS_WINDOWS = sys.platform.startswith("win")

# Default monospace family for the GUIs. Consolas ships with Windows; Linux
# resolves the generic "monospace" alias through fontconfig.
MONO_FONT = "Consolas" if IS_WINDOWS else "monospace"

# Key naming the launcher binary in a .vrmanifest — OpenVR picks the entry
# matching the running platform.
VRMANIFEST_BINARY_KEY = "binary_path_windows" if IS_WINDOWS else "binary_path_linux"


def config_dir():
    """Per-user directory for settings.json and the generated .vrmanifest."""
    if IS_WINDOWS:
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                            "CyberFingerBridge")
    config_home = os.environ.get("XDG_CONFIG_HOME",
                                 os.path.expanduser("~/.config"))
    return os.path.join(config_home, "cyberfinger-bridge")


# Steam's install root moves around on Linux (classic path, XDG data dir,
# Flatpak), so that side is probed rather than hardcoded.
_STEAMVR_SETTINGS_CANDIDATES = (
    (os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                  "Steam", "config", "steamvr.vrsettings"),)
    if IS_WINDOWS else (
        os.path.expanduser("~/.steam/steam/config/steamvr.vrsettings"),
        os.path.expanduser("~/.local/share/Steam/config/steamvr.vrsettings"),
        os.path.expanduser("~/.steam/root/config/steamvr.vrsettings"),
        os.path.expanduser("~/.var/app/com.valvesoftware.Steam/.local/share/"
                           "Steam/config/steamvr.vrsettings"),
    )
)


def steamvr_settings_path():
    """Path to steamvr.vrsettings — the first that exists, else the usual one.

    Resolved per call, not cached: Steam may be installed, or a runtime first
    started, after the bridge launches. Under a runtime with no such file
    (xrizer in front of Monado/WiVRn, say) callers just find it absent.
    """
    for path in _STEAMVR_SETTINGS_CANDIDATES:
        if os.path.exists(path):
            return path
    return _STEAMVR_SETTINGS_CANDIDATES[0]


def shared_asset(name):
    """Absolute path to a file in bridge_common/assets.

    In a PyInstaller build the spec copies these into the bundle's flat
    `assets` directory, so look there first; from source they sit next to this
    module.
    """
    if hasattr(sys, "_MEIPASS"):
        bundled = os.path.join(sys._MEIPASS, "assets", name)
        if os.path.exists(bundled):
            return bundled
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "assets", name)
