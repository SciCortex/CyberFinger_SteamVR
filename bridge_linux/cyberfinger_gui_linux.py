# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""
CyberFinger Bridge GUI — Linux

Linux counterpart of bridge/cyberfinger_gui.py. Only the platform layers live
here — BLE over bleak/BlueZ, the uinput virtual gamepad, tray icon loading and
the app shell. The protocol, SlimeVR emulation, OpenVR skeleton client, driver
stream and visualisation panels are shared with the Windows bridge in
bridge_common/, so the two stay in step by construction.

Modes:
    VR Mode      — controller buttons + hand skeleton + 6DOF → CyberFinger
                   SteamVR driver (HTSK/CFGP over UDP)
    Gamepad      — virtual Xbox 360 pad via uinput, Resonite mapping
    Gamepad VRC  — virtual Xbox 360 pad via uinput + OSC, VRChat mapping

Independently of the mode it can emulate SlimeVR trackers from the controller
IMUs, and it visualises hand state, IMU orientation and the tracked skeleton.

Prerequisites:
    pip install bleak pystray pillow
    pip install evdev          # for gamepad mode
    pip install openvr         # optional, hand skeleton + 6DOF in VR mode
    pip install pynput         # optional, F12 screenshot in VRChat mode
    pip install python-osc     # optional, VRChat OSC (UseLeft, Grab, Voice)
    sudo modprobe uinput       # load uinput kernel module

    # To use uinput without root:
    sudo groupadd -f uinput
    sudo usermod -aG uinput "$USER"
    echo 'KERNEL=="uinput", GROUP="uinput", MODE="0660"' | \
        sudo tee /etc/udev/rules.d/99-uinput.rules
    sudo udevadm control --reload-rules
    # Then log out and back in

Usage:
    python cyberfinger_gui_linux.py
"""

import asyncio
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext
import time
import sys
import os
import queue
import json
import subprocess

# bridge_common lives beside this package in the repo checkout. APPENDED, never
# inserted: the repo root also holds the OpenVR SDK's `openvr/` directory, and
# ahead of site-packages that empty dir imports as a namespace package that
# shadows pyopenvr — satisfying `import openvr` with a module that has no API.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from bridge_common.graphics import (COLOR_ACCENT, COLOR_ACCENT2, COLOR_BG,
                                    COLOR_BG2, COLOR_BG3, COLOR_FG,
                                    COLOR_FG_DIM, COLOR_GREEN, COLOR_ORANGE,
                                    COLOR_RED, FONT)
from bridge_common.panels import HandPanel, SkeletonPanel
from bridge_common.platform import config_dir
from bridge_common.protocol import (BTN_C, BTN_D, BTN_E, BTN_GRIP, BTN_JCLICK,
                                    BTN_MENU, BTN_STSEL, BTN_TRIGGER,
                                    HandState, describe_imus, fmt_buttons,
                                    parse_report)
from bridge_common.skeleton import HAS_OPENVR, create_skeleton_source
from bridge_common.slimevr import (SLIME_DEFAULT_HOST, SLIME_DEFAULT_PORT,
                                   SlimeVRForwarder)
from bridge_common.vr_controller import FusedVRMode

try:
    from bleak import BleakScanner, BleakClient
    from bleak.backends.bluezdbus.manager import get_global_bluez_manager
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False

try:
    # gi (PyGObject) enables pystray's GTK/AppIndicator backend which supports
    # menus on Linux. If not in the venv, try the system site-packages.
    import gi
except ImportError:
    try:
        _gi_path = subprocess.run(
            ["python3", "-c",
             "import gi, os; print(os.path.dirname(os.path.dirname(gi.__file__)))"],
            capture_output=True, text=True
        ).stdout.strip()
        if _gi_path and _gi_path not in sys.path:
            sys.path.insert(0, _gi_path)
        import gi  # noqa: F811
    except Exception:
        pass

try:
    import pystray
    from PIL import Image
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

try:
    from evdev import UInput, ecodes, AbsInfo
    HAS_EVDEV = True
except ImportError:
    HAS_EVDEV = False

try:
    from pynput.keyboard import Key, Controller as KeyboardController
    _keyboard = KeyboardController()
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False


def resource_path(relative):
    """Get path to a bridge_linux/ resource (icons)."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative)


# ── Tray icon helpers ────────────────────────────────────────────────────

def _load_tray_icon(filename, bg_color):
    """Load a black-silhouette PNG and composite it as white over bg_color."""
    path = resource_path(os.path.join("assets", filename))
    try:
        raw = Image.open(path).convert("RGBA").resize((32, 32), Image.LANCZOS)
        # The PNGs are black silhouettes on transparent — make them
        # white-on-brand-color so they're visible on any tray background.
        bg = Image.new("RGB", (32, 32), bg_color)
        white = Image.new("RGB", (32, 32), (255, 255, 255))
        alpha = raw.split()[3]          # use icon's alpha as mask
        bg.paste(white, mask=alpha)
        return bg
    except Exception as e:
        print(f"[tray] Failed to load {path}: {e}", flush=True)
        return _generate_fallback_icon(bg_color)


def _load_tray_icon_running():
    return _load_tray_icon("icon_32x32.png", (230, 0, 126))


def _load_tray_icon_idle():
    return _load_tray_icon("icon_32x32_bw.png", (80, 80, 80))


def _generate_fallback_icon(color):
    return Image.new("RGB", (32, 32), color)


# ── BLE Manager (bleak/BlueZ, runs in asyncio thread) ────────────────────

class BLEManager:
    """Manages BLE connections using bleak over BlueZ D-Bus."""

    def __init__(self, app):
        self.app = app
        self.left = HandState()
        self.right = HandState()
        self._thread = None
        self._loop = None
        self._running = False
        self._task = None
        self._clients = []  # (label, BleakClient, char_uuid)
        self._warned_report_len = False  # one-shot report-length mismatch warning

    def start(self):
        self._running = True
        self._task = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running() and self._task:
            self._loop.call_soon_threadsafe(self._task.cancel)

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception as e:
            self.app.log(f"BLE thread error: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self._task = None

    async def _run(self):
        self._task = asyncio.current_task()
        try:
            await self._main()
        except asyncio.CancelledError:
            pass
        finally:
            for label, client, char_uuid in self._clients:
                try:
                    if client.is_connected:
                        await client.stop_notify(char_uuid)
                except Exception:
                    pass
                # Do NOT disconnect — the device stays connected as HID.
                # Disconnecting would tear down the physical BLE link and force
                # a slow re-pair/reconnect on the next start.
            self._clients = []
            self.left.connected = False
            self.right.connected = False

    def _get_paired_cyberfinger_devices(self):
        """Return objects with .address/.name for paired CyberFinger devices."""
        try:
            result = subprocess.run(
                ["bluetoothctl", "devices", "Paired"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.splitlines()
        except Exception as e:
            self.app.log(f"bluetoothctl failed: {e}")
            return []

        class _Dev:
            def __init__(self, address, name):
                self.address = address
                self.name = name

        devices = []
        for line in lines:
            # Format: "Device XX:XX:XX:XX:XX:XX DeviceName"
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device":
                address, name = parts[1], parts[2]
                self.app.log(f"  Paired: \"{name}\" ({address})")
                if "cyberfinger" in name.lower():
                    devices.append(_Dev(address, name))
        return devices

    async def _main(self):
        if not HAS_BLEAK:
            self.app.log("ERROR: bleak not installed! pip install bleak")
            self.app.set_status("bleak not installed")
            return

        self.app.log("Looking up paired CyberFinger devices...")
        self.app.set_status("Looking up paired devices...")

        cf_devices = self._get_paired_cyberfinger_devices()

        if not cf_devices:
            self.app.log("No paired CyberFinger devices found!")
            self.app.log("Pair the ESP32s via bluetoothctl first.")
            self.app.set_status("No paired devices found")
            return

        self.app.log(f"Found {len(cf_devices)} CyberFinger device(s)")

        left_dev = right_dev = None
        for dev in cf_devices:
            name = (dev.name or "").lower()
            if "left" in name and not left_dev:
                left_dev = dev
                self.app.log(f"  LEFT  ← \"{dev.name}\" ({dev.address})")
            elif "right" in name and not right_dev:
                right_dev = dev
                self.app.log(f"  RIGHT ← \"{dev.name}\" ({dev.address})")

        if not left_dev and not right_dev:
            self.app.log("No devices with 'left' or 'right' in name!")
            self.app.set_status("Assignment failed")
            return

        self._clients = []
        for label, dev, state in [("LEFT", left_dev, self.left),
                                  ("RIGHT", right_dev, self.right)]:
            if dev is None:
                continue
            try:
                await self._connect_device(label, dev, state)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.app.log(f"Connection error: {e}")

        active = [label for label, *_ in self._clients]
        if not active:
            self.app.log("Failed to connect to any device!")
            self.app.set_status("Connection failed")
            return

        self.app.log(f"Connected: {', '.join(active)}")
        self.app.set_status("Connected")

        try:
            while self._running:
                for label, client, _ in self._clients:
                    if not client.is_connected:
                        self.app.log(f"{label}: Disconnected!")
                        self.app.set_status(f"{label} disconnected")
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    async def _connect_device(self, label, dev, state):
        self.app.log(f"{label}: Connecting to {dev.address}...")

        # bleak 3.x always does an active scan in connect() if _device_path is
        # not set, which fails for already-connected paired devices that aren't
        # advertising.  Look up the real D-Bus path from the manager instead.
        manager = await get_global_bluez_manager()
        addr_key = dev.address.upper().replace(":", "_")
        device_path = next(
            (k for k in manager._properties if k.endswith(f"/dev_{addr_key}")),
            None
        )
        if not device_path:
            self.app.log(f"{label}: Device not found in BlueZ manager")
            return
        self.app.log(f"{label}: D-Bus path: {device_path}")

        client = BleakClient(dev.address, timeout=15.0)
        client._backend._device_path = device_path
        try:
            await client.connect()
        except Exception as e:
            # AlreadyConnected is fine — device is still up as HID, just reuse it
            if "AlreadyConnected" not in str(e):
                self.app.log(f"{label}: Connection failed: {e}")
                return

        if not client.is_connected:
            self.app.log(f"{label}: Not connected")
            return

        state.connected = True
        state.name = dev.name or dev.address
        state.address = dev.address
        # Capability flags are per-link: a unit reconnecting with a different
        # IMU set must not inherit the previous session's slots.
        state.reset_link()

        cf01_char = None
        for service in client.services:
            if "cf00" in str(service.uuid).lower():
                for char in service.characteristics:
                    if "cf01" in str(char.uuid).lower():
                        cf01_char = char
                        break

        if not cf01_char:
            self.app.log(f"{label}: CF01 characteristic not found!")
            self.app.log(f"{label}: Services: {[str(s.uuid) for s in client.services]}")
            return

        hand_idx = 0 if label == "LEFT" else 1

        def on_notify(sender, data):
            self._handle_data(data, hand_idx, state)

        try:
            await client.start_notify(cf01_char.uuid, on_notify)
            self.app.log(f"{label}: Notifications active")
            self._clients.append((label, client, cf01_char.uuid))
        except Exception as e:
            self.app.log(f"{label}: Notify subscribe failed: {e}")

    def _handle_data(self, data, hand_override, state):
        report = parse_report(data)
        if report is None:
            return

        # Unlike the Windows bridge, the hand comes from which paired device
        # this notification arrived on rather than the report's hand byte: each
        # BleakClient is already bound to one unit, assigned by device name at
        # connect time.
        h = hand_override
        hn = "L" if h == 0 else "R"

        if not report.length_ok and not self._warned_report_len:
            self._warned_report_len = True
            self.app.log(f"IMU report length {report.raw_len} != expected "
                         f"{report.expected_len} for "
                         f"present=0x{report.imu_present:02X}")

        buttons_changed, imu_changed = state.apply(report)

        if imu_changed:
            # Byte count and raw bitmask included so a slot going missing can be
            # blamed on the wire or on the parser without a sniffer: the length
            # is what actually arrived, before any truncation fallback ran.
            self.app.log(f"{hn} IMU: {describe_imus(report.imu_present)}"
                         f"  ({report.raw_len}B, "
                         f"present=0x{report.imu_present:02X})")
        if buttons_changed:
            self.app.log(f"{hn} BTN: {fmt_buttons(report.buttons)}")

        self.app.on_input(h, state)


# ── VR Mode ──────────────────────────────────────────────────────────────
#
# Lives in bridge_common/vr_controller.py (FusedVRMode): fuses the runtime hand
# skeleton, device 6DOF and controller buttons into driver_cyberfinger's
# emulated controllers. The old CFGP-only VRMode is subsumed by it — with no
# skeleton source available, FusedVRMode degrades to exactly that behavior.


# ── uinput device factory ────────────────────────────────────────────────

def _make_uinput():
    """Create a UInput Xbox 360-like virtual gamepad. Returns (device, available)."""
    if not HAS_EVDEV:
        return None, False
    try:
        cap = {
            ecodes.EV_ABS: [
                (ecodes.ABS_X,     AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_Y,     AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_RX,    AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_RY,    AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_Z,     AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_RZ,    AbsInfo(value=0, min=0, max=255, fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_HAT0X, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_HAT0Y, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
            ],
            ecodes.EV_KEY: [
                ecodes.BTN_A,
                ecodes.BTN_B,
                ecodes.BTN_X,
                ecodes.BTN_Y,
                ecodes.BTN_TL,
                ecodes.BTN_TR,
                ecodes.BTN_SELECT,
                ecodes.BTN_START,
                ecodes.BTN_THUMBL,
                ecodes.BTN_THUMBR,
                ecodes.BTN_MODE,
            ],
        }
        dev = UInput(cap, name="CyberFinger Virtual Gamepad",
                     vendor=0x045e, product=0x028e, version=0x0110)
        return dev, True
    except PermissionError:
        return None, False
    except Exception:
        return None, False


# ── Gamepad Mode (evdev/uinput virtual Xbox 360, Resonite) ───────────────

class GamepadMode:
    """Creates a virtual Xbox 360-like gamepad via Linux uinput."""

    def __init__(self):
        self.device, self.available = _make_uinput()

    def on_input(self, hand, state):
        pass  # update_gamepad called by app

    def update_gamepad(self, left, right):
        if not self.available or not self.device:
            return

        dev = self.device

        dev.write(ecodes.EV_ABS, ecodes.ABS_X,  left.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_Y,  left.joy_y)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RX, right.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RY, right.joy_y)

        # Triggers (analog)
        dev.write(ecodes.EV_ABS, ecodes.ABS_Z,  int(left.trigger_float  * 255))
        dev.write(ecodes.EV_ABS, ecodes.ABS_RZ, int(right.trigger_float * 255))

        # Right hand buttons (mirrors Windows vgamepad mapping)
        dev.write(ecodes.EV_KEY, ecodes.BTN_A,      1 if (right.buttons & BTN_TRIGGER) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_B,      1 if (right.buttons & BTN_GRIP)    else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_TR,     1 if (right.buttons & BTN_MENU)    else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_THUMBR, 1 if (right.buttons & BTN_JCLICK)  else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_START,  1 if (right.buttons & BTN_STSEL)   else 0)

        # Left hand buttons
        dev.write(ecodes.EV_KEY, ecodes.BTN_X,      1 if (left.buttons & BTN_TRIGGER) else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_Y,      1 if (left.buttons & BTN_GRIP)    else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_TL,     1 if (left.buttons & BTN_MENU)    else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_THUMBL, 1 if (left.buttons & BTN_JCLICK)  else 0)
        dev.write(ecodes.EV_KEY, ecodes.BTN_SELECT, 1 if (left.buttons & BTN_STSEL)   else 0)

        # C/D/E → D-pad + guide (mirrors Windows wButtons bit mapping)
        # Right C→UP, Right D→DOWN, Right E→LEFT, Left C→RIGHT, Left D→GUIDE
        hat_x = 0
        hat_y = 0
        if right.buttons & BTN_C: hat_y = -1   # up
        if right.buttons & BTN_D: hat_y =  1   # down
        if right.buttons & BTN_E: hat_x = -1   # left
        if left.buttons  & BTN_C: hat_x =  1   # right
        dev.write(ecodes.EV_ABS, ecodes.ABS_HAT0X, hat_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_HAT0Y, hat_y)
        dev.write(ecodes.EV_KEY, ecodes.BTN_MODE, 1 if (left.buttons & BTN_D) else 0)

        dev.syn()

    def stop(self):
        self.available = False      # prevent racing BLE callbacks from touching closed fd
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None


# ── Gamepad Mode VRChat (evdev/uinput + OSC) ─────────────────────────────

class GamepadModeVRChat:
    """
    uinput Xbox 360 gamepad with VRChat-optimal button mapping + OSC.

    VRChat gamepad layout (Xbox reference):
      Left  stick        → Move
      Right stick X      → Smooth turn
      Right stick Y      → Look up/down
      RT (right trigger) → Use / Interact (right hand)
      A                  → Jump
      R3                 → Action Menu right
      Start              → Quick Menu

    CyberFinger → Xbox mapping:
      Right TRIGGER  → RT  (use/interact right)
      Left  TRIGGER  → OSC UseLeft (no LT gamepad — avoids duplicate events)
      Right GRIP     → OSC GrabRight (tap=toggle, hold≥200ms=release on lift)
      Left  GRIP     → OSC GrabLeft
      Right MENU     → R3  (Action Menu right)
      Left  MENU     → Start (Quick Menu)
      Right JCLICK / Left JCLICK → A (Jump)
      Right C        → F12 screenshot (rising edge, via pynput)
      Left  C        → X (Mute)
      Right D        → DPAD_RIGHT
      Left  D        → DPAD_LEFT
      Right E        → DPAD_UP
      Left  E        → DPAD_DOWN
      Right ST/SE    → OSC chatbox open (rising edge)
      Left  ST/SE    → OSC Voice mute toggle
    """

    def __init__(self):
        self.device, self.available = _make_uinput()

        self._osc = None
        try:
            from pythonosc import udp_client
            self._osc = udp_client.SimpleUDPClient("127.0.0.1", 9000)
        except ImportError:
            pass

        self._prev_use_l             = False
        self._prev_grab_r            = False
        self._prev_grab_l            = False
        self._grab_right_toggled     = False
        self._grab_left_toggled      = False
        self._grab_right_press_time  = 0.0
        self._grab_left_press_time   = 0.0
        self._prev_c_r               = False
        self._prev_stsel_r           = False
        self._prev_stsel_l           = False

    def _osc_send(self, address, value):
        if self._osc:
            try:
                self._osc.send_message(address, value)
            except Exception:
                pass

    def on_input(self, hand, state):
        pass  # update_gamepad called by app

    def update_gamepad(self, left, right):
        if not self.available or not self.device:
            return

        dev = self.device

        dev.write(ecodes.EV_ABS, ecodes.ABS_X,  left.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_Y,  left.joy_y)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RX, right.joy_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RY, right.joy_y)

        # Right trigger → RT; left trigger → OSC UseLeft only (no LT gamepad)
        trig_r = max(right.trigger_float, 1.0 if (right.buttons & BTN_TRIGGER) else 0.0)
        trig_l = max(left.trigger_float,  1.0 if (left.buttons  & BTN_TRIGGER) else 0.0)
        dev.write(ecodes.EV_ABS, ecodes.ABS_RZ, int(trig_r * 255))
        dev.write(ecodes.EV_ABS, ecodes.ABS_Z,  0)  # suppressed — UseLeft via OSC

        # OSC: UseLeft
        use_l = trig_l > 0.1
        if use_l != self._prev_use_l:
            self._osc_send("/input/UseLeft", int(use_l))
            self._prev_use_l = use_l

        # OSC: GrabRight / GrabLeft (tap=toggle, hold≥200ms=release on lift)
        grab_r = bool(right.buttons & BTN_GRIP)
        grab_l = bool(left.buttons  & BTN_GRIP)

        if grab_r and not self._prev_grab_r:
            self._grab_right_press_time = time.time()
            self._osc_send("/input/GrabRight", 1)
        elif not grab_r and self._prev_grab_r:
            held_ms = (time.time() - self._grab_right_press_time) * 1000
            if held_ms < 200:
                self._grab_right_toggled = not self._grab_right_toggled
                self._osc_send("/input/GrabRight", int(self._grab_right_toggled))
            else:
                self._grab_right_toggled = False
                self._osc_send("/input/GrabRight", 0)

        if grab_l and not self._prev_grab_l:
            self._grab_left_press_time = time.time()
            self._osc_send("/input/GrabLeft", 1)
        elif not grab_l and self._prev_grab_l:
            held_ms = (time.time() - self._grab_left_press_time) * 1000
            if held_ms < 200:
                self._grab_left_toggled = not self._grab_left_toggled
                self._osc_send("/input/GrabLeft", int(self._grab_left_toggled))
            else:
                self._grab_left_toggled = False
                self._osc_send("/input/GrabLeft", 0)

        self._prev_grab_r = grab_r
        self._prev_grab_l = grab_l

        # Jump (either jclick → A)
        jclick = bool(right.buttons & BTN_JCLICK) or bool(left.buttons & BTN_JCLICK)
        dev.write(ecodes.EV_KEY, ecodes.BTN_A, 1 if jclick else 0)

        # Action Menu R (right MENU → R3)
        dev.write(ecodes.EV_KEY, ecodes.BTN_THUMBR, 1 if (right.buttons & BTN_MENU) else 0)

        # Quick Menu (left MENU → Start)
        dev.write(ecodes.EV_KEY, ecodes.BTN_START, 1 if (left.buttons & BTN_MENU) else 0)

        # Mute (left C → X)
        dev.write(ecodes.EV_KEY, ecodes.BTN_X, 1 if (left.buttons & BTN_C) else 0)

        # D-pad
        hat_x = 0
        hat_y = 0
        if right.buttons & BTN_D: hat_x =  1   # right
        if left.buttons  & BTN_D: hat_x = -1   # left
        if right.buttons & BTN_E: hat_y = -1   # up
        if left.buttons  & BTN_E: hat_y =  1   # down
        dev.write(ecodes.EV_ABS, ecodes.ABS_HAT0X, hat_x)
        dev.write(ecodes.EV_ABS, ecodes.ABS_HAT0Y, hat_y)

        dev.syn()

        # Right C → F12 screenshot (rising edge)
        c_r = bool(right.buttons & BTN_C)
        if c_r and not self._prev_c_r and HAS_PYNPUT:
            try:
                _keyboard.press(Key.f12)
                _keyboard.release(Key.f12)
            except Exception:
                pass
        self._prev_c_r = c_r

        # Right ST/SE → open VRChat chatbox (rising edge)
        stsel_r = bool(right.buttons & BTN_STSEL)
        if stsel_r and not self._prev_stsel_r:
            self._osc_send("/chatbox/input", ["", False, False])
        self._prev_stsel_r = stsel_r

        # Left ST/SE → OSC Voice (1 on press, 0 on release)
        stsel_l = bool(left.buttons & BTN_STSEL)
        if stsel_l != self._prev_stsel_l:
            self._osc_send("/input/Voice", int(stsel_l))
        self._prev_stsel_l = stsel_l

    def stop(self):
        self.available = False      # prevent racing BLE callbacks from touching closed fd
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
        for addr in ("/input/UseLeft", "/input/GrabRight", "/input/GrabLeft",
                     "/input/Voice"):
            self._osc_send(addr, 0)


# ── GUI Application ──────────────────────────────────────────────────────

class CyberFingerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("CyberFinger Bridge")
        self.root.configure(bg=COLOR_BG)
        self.root.geometry("680x790")
        self.root.minsize(600, 660)

        menubar = tk.Menu(self.root, tearoff=0)
        app_menu = tk.Menu(menubar, tearoff=0)
        app_menu.add_command(label="Exit", command=self._quit_app)
        menubar.add_cascade(label="CyberFinger", menu=app_menu)
        self.root.config(menu=menubar)

        try:
            icon_path = resource_path(os.path.join("assets", "icon_32x32.png"))
            icon_img = tk.PhotoImage(file=icon_path)
            self.root.iconphoto(True, icon_img)
            self._icon_ref = icon_img
        except Exception:
            pass

        self.log_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self._current_status = "Idle"
        self._window_visible = True

        self._config_dir = config_dir()
        self._config_path = os.path.join(self._config_dir, "settings.json")
        self._config = self._load_config()

        self.ble = BLEManager(self)
        self.vr_mode = None              # created lazily on start (FusedVRMode)
        self.gamepad_mode = None         # created lazily on start
        self.vrchat_gamepad_mode = None  # created lazily on start
        self.active_mode = None
        self.slimevr = None              # created lazily while forwarding is on
        # Config gate ("skeleton_enabled": false in settings.json) exists so
        # the OpenVR client can be ruled in/out when debugging runtime-side
        # trouble without touching code.
        self.skeleton = (create_skeleton_source(
                             self.log, self._config.get("skeleton_bisect", False),
                             self._config.get("skeleton_backend", "auto"))
                         if self._config.get("skeleton_enabled", True) else None)

        self._build_ui()

        if self.skeleton:
            self.skeleton.start()

        self._tray_icon = None
        if HAS_TRAY:
            self._setup_tray()

        self._poll_queues()

        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        if self._config.get("autostart", False):
            self.root.after(500, self._start_bridge)

    def _load_config(self):
        try:
            with open(self._config_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        try:
            os.makedirs(self._config_dir, exist_ok=True)
            with open(self._config_path, "w") as f:
                json.dump(self._config, f)
        except Exception:
            pass

    # ── System Tray ──────────────────────────────────────────────────────

    def _setup_tray(self):
        self._tray_icon_running = _load_tray_icon_running()
        self._tray_icon_idle = _load_tray_icon_idle()

        menu = pystray.Menu(
            pystray.MenuItem("Show/Hide", self._tray_toggle_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start", self._tray_start),
            pystray.MenuItem("Stop", self._tray_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._tray_exit),
        )

        self._tray_icon = pystray.Icon(
            "CyberFingerBridge",
            self._tray_icon_idle,
            "CyberFinger Bridge - Idle",
            menu
        )

        tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        tray_thread.start()

    def _set_tray_running(self, running):
        if self._tray_icon:
            try:
                self._tray_icon.icon = self._tray_icon_running if running else self._tray_icon_idle
            except Exception:
                pass

    def _update_tray_tooltip(self):
        if self._tray_icon:
            mode = self.mode_var.get().upper() if hasattr(self, 'mode_var') else ""
            self._tray_icon.title = f"CyberFinger Bridge - {self._current_status}" + \
                                    (f" ({mode})" if self.active_mode else "")

    def _tray_toggle_window(self, icon=None, item=None):
        if self._window_visible:
            self.root.after(0, self._hide_window)
        else:
            self.root.after(0, self._show_window)

    def _tray_start(self, icon=None, item=None):
        self.root.after(0, self._start_bridge)

    def _tray_stop(self, icon=None, item=None):
        self.root.after(0, self._stop_bridge)

    def _tray_exit(self, icon=None, item=None):
        self.root.after(0, self._quit_app)

    def _hide_window(self):
        self.root.withdraw()
        self._window_visible = False

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self._window_visible = True

    def _on_window_close(self):
        if HAS_TRAY and self._tray_icon:
            self._hide_window()
            self.log("Minimized to system tray")
        else:
            self._quit_app()

    def _quit_app(self):
        self._config["mode"] = self.mode_var.get()
        self._config["autostart"] = self.autostart_var.get()
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()

        self.ble.stop()
        if self.active_mode:
            self.active_mode.stop()
        self._stop_slimevr()
        if self.skeleton:
            self.skeleton.stop()

        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass

        self.root.destroy()

    # ── UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure(".", background=COLOR_BG, foreground=COLOR_FG)
        style.configure("TFrame", background=COLOR_BG)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_FG, font=(FONT, 10))
        style.configure("Title.TLabel", background=COLOR_BG, foreground=COLOR_ACCENT,
                        font=(FONT, 14, "bold"))
        style.configure("Status.TLabel", background=COLOR_BG, foreground=COLOR_FG_DIM,
                        font=(FONT, 9))
        style.configure("Hand.TLabel", background=COLOR_BG2, foreground=COLOR_FG,
                        font=(FONT, 10))
        style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_FG,
                        font=(FONT, 10), focuscolor=COLOR_BG)
        style.map("TRadiobutton",
                  background=[("active", COLOR_BG)],
                  foreground=[("active", COLOR_ACCENT)])
        style.configure("Small.TRadiobutton", background=COLOR_BG, foreground=COLOR_FG,
                        font=(FONT, 9), focuscolor=COLOR_BG)
        style.map("Small.TRadiobutton",
                  background=[("active", COLOR_BG)],
                  foreground=[("active", COLOR_ACCENT)])
        style.configure("TCheckbutton", background=COLOR_BG, foreground=COLOR_FG,
                        font=(FONT, 9), focuscolor=COLOR_BG)
        style.map("TCheckbutton",
                  background=[("active", COLOR_BG)],
                  foreground=[("active", COLOR_ACCENT)])
        style.configure("Accent.TButton", background=COLOR_ACCENT, foreground="white",
                        font=(FONT, 11, "bold"), padding=(20, 8))
        style.map("Accent.TButton",
                  background=[("active", COLOR_ACCENT2), ("disabled", COLOR_BG3)])
        style.configure("Stop.TButton", background=COLOR_RED, foreground="white",
                        font=(FONT, 11, "bold"), padding=(20, 8))
        style.map("Stop.TButton",
                  background=[("active", "#ff4444"), ("disabled", COLOR_BG3)])
        style.configure("Console.TButton", background=COLOR_BG3, foreground=COLOR_FG,
                        font=(FONT, 9), padding=(10, 2))
        style.map("Console.TButton",
                  background=[("active", COLOR_BG2)],
                  foreground=[("active", COLOR_ACCENT)])

        # ── Header ──
        header = ttk.Frame(self.root)
        header.pack(fill=tk.X, padx=16, pady=(12, 4))
        ttk.Label(header, text="⬡ CyberFinger Bridge (Linux)",
                  style="Title.TLabel").pack(side=tk.LEFT)
        self.status_label = ttk.Label(header, text="Idle", style="Status.TLabel")
        self.status_label.pack(side=tk.RIGHT)

        # ── Mode selection + Start/Stop ──
        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill=tk.X, padx=16, pady=(4, 4))

        self.mode_var = tk.StringVar(value=self._config.get("mode", "vr"))
        radio_frame = ttk.Frame(ctrl_frame)
        radio_frame.pack(side=tk.LEFT)
        ttk.Radiobutton(radio_frame, text="VR Mode (controllers + hand tracking → SteamVR)",
                        variable=self.mode_var, value="vr").pack(anchor=tk.W)
        ttk.Radiobutton(radio_frame, text="Gamepad Mode (BLE→uinput Xbox 360, Resonite)",
                        variable=self.mode_var, value="gamepad").pack(anchor=tk.W)
        ttk.Radiobutton(radio_frame, text="Gamepad Mode (BLE→uinput Xbox 360, VRChat)",
                        variable=self.mode_var, value="gamepad_vrc").pack(anchor=tk.W)

        self.stop_btn = ttk.Button(ctrl_frame, text="Stop", style="Stop.TButton",
                                   command=self._stop_bridge, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.start_btn = ttk.Button(ctrl_frame, text="Start", style="Accent.TButton",
                                    command=self._start_bridge)
        self.start_btn.pack(side=tk.RIGHT)

        # ── Options row ──
        opts_frame = ttk.Frame(self.root)
        opts_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.autostart_var = tk.BooleanVar(value=self._config.get("autostart", False))
        ttk.Checkbutton(opts_frame, text="Auto-start on launch",
                        variable=self.autostart_var,
                        command=self._on_autostart_changed).pack(side=tk.LEFT)

        if HAS_TRAY:
            ttk.Label(opts_frame, text="(close button minimizes to tray)",
                     style="Status.TLabel").pack(side=tk.RIGHT)

        # ── SlimeVR row ──
        slime_frame = ttk.Frame(self.root)
        slime_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.slimevr_var = tk.BooleanVar(value=self._config.get("slimevr_enabled", False))
        ttk.Checkbutton(slime_frame, text="Forward IMU to SlimeVR",
                        variable=self.slimevr_var,
                        command=self._on_slimevr_changed).pack(side=tk.LEFT)

        # Body 1 and Body 2 are redundant IMUs at the same location, so only one
        # is forwarded — this picks which, falling back to the other if absent.
        ttk.Label(slime_frame, text="  body IMU:",
                  style="Status.TLabel").pack(side=tk.LEFT)
        self.slimevr_body_var = tk.StringVar(
            value=self._config.get("slimevr_body_imu", "body1"))
        for label, value in (("1", "body1"), ("2", "body2")):
            ttk.Radiobutton(slime_frame, text=label, style="Small.TRadiobutton",
                            variable=self.slimevr_body_var, value=value,
                            command=self._on_slimevr_changed).pack(side=tk.LEFT)

        # ── Hands visualization ──
        hands_frame = ttk.Frame(self.root)
        hands_frame.pack(fill=tk.X, padx=16, pady=4)

        self.left_panel = HandPanel(hands_frame, "LEFT", side=tk.LEFT)
        self.right_panel = HandPanel(hands_frame, "RIGHT", side=tk.RIGHT)

        # ── Bottom bar: console toggle ──
        # Packed before the skeleton row so pack gives it its slice at the
        # bottom and the skeleton area expands into whatever is left.
        bottom = ttk.Frame(self.root)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 8))
        self.console_visible = self._config.get("console_visible", False)
        self.console_btn = ttk.Button(
            bottom, text="▼ Console" if self.console_visible else "▲ Console",
            style="Console.TButton", command=self._toggle_console)
        self.console_btn.pack(side=tk.RIGHT)

        # ── Hand skeleton row (what the VR runtime is tracking) ──
        self.skeleton_area = ttk.Frame(self.root)
        self.skeleton_area.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 4))
        self.left_skeleton = SkeletonPanel(self.skeleton_area, "LEFT", side=tk.LEFT)
        self.right_skeleton = SkeletonPanel(self.skeleton_area, "RIGHT", side=tk.RIGHT)

        # ── Log console — hidden by default, slides up over the skeletons ──
        self.log_frame = ttk.Frame(self.root)
        self.log_text = scrolledtext.ScrolledText(
            self.log_frame, height=8,
            bg=COLOR_BG2, fg=COLOR_FG, insertbackground=COLOR_FG,
            font=(FONT, 9), relief=tk.FLAT, borderwidth=0,
            selectbackground=COLOR_ACCENT, selectforeground="white",
            state=tk.DISABLED, wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.log_text.tag_configure("accent", foreground=COLOR_ACCENT)
        self.log_text.tag_configure("green", foreground=COLOR_GREEN)
        self.log_text.tag_configure("red", foreground=COLOR_RED)

        self._console_frac = 1.0 if self.console_visible else 0.0
        self._console_anim = None
        if self.console_visible:
            self._place_console(1.0)

    def _place_console(self, frac):
        """Overlay the console over the bottom `frac` of the skeleton area."""
        self.log_frame.place(in_=self.skeleton_area, relx=0.0, rely=1.0,
                             anchor="sw", relwidth=1.0,
                             relheight=max(0.02, frac))

    def _toggle_console(self):
        self.console_visible = not self.console_visible
        self._config["console_visible"] = self.console_visible
        self._save_config()
        self.console_btn.configure(
            text="▼ Console" if self.console_visible else "▲ Console")
        if self._console_anim is not None:
            self.root.after_cancel(self._console_anim)
        self._animate_console()

    def _animate_console(self):
        self._console_anim = None
        target = 1.0 if self.console_visible else 0.0
        delta = target - self._console_frac
        if abs(delta) < 0.02:
            self._console_frac = target
            if target > 0.0:
                self._place_console(1.0)
                self.log_text.see(tk.END)
            else:
                self.log_frame.place_forget()
            return
        self._console_frac += max(-0.2, min(0.2, delta))
        self._place_console(self._console_frac)
        self._console_anim = self.root.after(16, self._animate_console)

    def _on_autostart_changed(self):
        self._config["autostart"] = self.autostart_var.get()
        self._save_config()

    def _on_slimevr_changed(self):
        """Persist the SlimeVR options, applying them live if already running."""
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()

        if self.slimevr:
            self.slimevr.set_body_slot(self.slimevr_body_var.get())

        # Only churn the forwarder while the bridge is actually running;
        # otherwise _start_bridge will pick the new setting up.
        if not self.active_mode:
            return
        if self.slimevr_var.get():
            self._start_slimevr()
        else:
            self._stop_slimevr()

    def _start_slimevr(self):
        if self.slimevr:
            return
        host = self._config.get("slimevr_host", SLIME_DEFAULT_HOST)
        port = int(self._config.get("slimevr_port", SLIME_DEFAULT_PORT))
        self.slimevr = SlimeVRForwarder(host, port,
                                        self.slimevr_body_var.get(), self.log)
        self.slimevr.start()
        self.log(f"SlimeVR: announcing trackers to {host}:{port}")

    def _stop_slimevr(self):
        if not self.slimevr:
            return
        self.slimevr.stop()
        self.slimevr = None
        self.log("SlimeVR: forwarding stopped")

    def _start_bridge(self):
        if self.active_mode:
            return

        if not HAS_BLEAK:
            self.log("ERROR: bleak not installed!")
            self.log("Run: pip install bleak")
            return

        mode = self.mode_var.get()
        if mode in ("gamepad", "gamepad_vrc"):
            if not HAS_EVDEV:
                self.log("ERROR: python-evdev not installed!")
                self.log("Run: pip install evdev")
                return
            gp = GamepadMode() if mode == "gamepad" else GamepadModeVRChat()
            if not gp.available:
                self.log("ERROR: Cannot create uinput device!")
                self.log("Run: sudo modprobe uinput")
                self.log("See file header for uinput permissions setup")
                return
            if mode == "gamepad":
                self.gamepad_mode = gp
                self.active_mode = self.gamepad_mode
            else:
                self.vrchat_gamepad_mode = gp
                self.active_mode = self.vrchat_gamepad_mode
        else:  # "vr" and fallback
            self.vr_mode = FusedVRMode(self.skeleton, self.ble, self.log)
            self.vr_mode.start()
            self.active_mode = self.vr_mode

        self._config["mode"] = mode
        self._config["autostart"] = self.autostart_var.get()
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()

        self.log(f"Starting {mode.upper()} mode...")
        if mode == "gamepad_vrc":
            self.log(">>> VRChat: enable OSC via Action Menu → OSC → Enabled")

        if self.slimevr_var.get():
            self._start_slimevr()

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._set_tray_running(True)

        self.ble.start()

    def _stop_bridge(self):
        if not self.active_mode:
            return

        self.ble.stop()
        if self.active_mode:
            self.active_mode.stop()
        self.active_mode = None
        self._stop_slimevr()

        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

        self.left_panel.set_disconnected()
        self.right_panel.set_disconnected()
        self.set_status("Stopped")
        self.log("Bridge stopped")
        self._set_tray_running(False)

        # Recreate for next start
        self.ble = BLEManager(self)
        self.vr_mode = None
        self.gamepad_mode = None
        self.vrchat_gamepad_mode = None

    def on_input(self, hand, state):
        """Called from BLE thread on each input report."""
        if self.active_mode:
            if isinstance(self.active_mode, (GamepadMode, GamepadModeVRChat)):
                self.active_mode.update_gamepad(self.ble.left, self.ble.right)
            else:
                self.active_mode.on_input(hand, state)

        # Runs alongside the active mode, not instead of it — SlimeVR takes the
        # orientation none of the other modes forward.
        if self.slimevr:
            self.slimevr.on_input(hand, state)

    def log(self, msg):
        self.log_queue.put(msg)

    def set_status(self, status):
        self._current_status = status
        self.status_queue.put(status)
        self._update_tray_tooltip()

    def _poll_queues(self):
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state=tk.NORMAL)
                ts = time.strftime("%H:%M:%S")
                self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state=tk.DISABLED)
            except queue.Empty:
                break

        while not self.status_queue.empty():
            try:
                status = self.status_queue.get_nowait()
                color = COLOR_GREEN if status == "Connected" else \
                        COLOR_RED if "error" in status.lower() or "failed" in status.lower() else \
                        COLOR_ORANGE if "Scanning" in status else COLOR_FG_DIM
                self.status_label.configure(text=status, foreground=color)
            except queue.Empty:
                break

        if self.ble:
            self.left_panel.update_state(self.ble.left)
            self.right_panel.update_state(self.ble.right)

        # Skip the skeleton redraw while the console fully covers it.
        if self._console_frac < 1.0:
            if self.skeleton:
                poses = self.skeleton.pose_info
                self.left_skeleton.draw(self.skeleton.hands[0],
                                        self.skeleton.status,
                                        poses[0], poses[1])
                self.right_skeleton.draw(self.skeleton.hands[1],
                                         self.skeleton.status,
                                         poses[1], poses[0])
            else:
                why = ("disabled in settings"
                       if not self._config.get("skeleton_enabled", True)
                       else "pip install openvr")
                self.left_skeleton.draw(None, why)
                self.right_skeleton.draw(None, why)

        self.root.after(33, self._poll_queues)  # ~30fps

    def run(self):
        self.log("CyberFinger Bridge (Linux) ready")
        self.log(f"BLE: {'bleak available' if HAS_BLEAK else 'NOT available (pip install bleak)'}")
        if HAS_EVDEV:
            if os.access('/dev/uinput', os.W_OK):
                self.log("Gamepad: evdev/uinput available")
            else:
                self.log("Gamepad: uinput permission denied — see file header for setup")
        else:
            self.log("Gamepad: NOT available (pip install evdev)")
        if not HAS_TRAY:
            self.log("System tray: not available (pip install pystray pillow)")
        if not HAS_PYNPUT:
            self.log("Keyboard (F12 screenshot): not available (pip install pynput)")
        if not HAS_OPENVR:
            self.log("Hand skeleton: not available (pip install openvr)")
        self.root.mainloop()


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    app = CyberFingerApp()
    app.run()


if __name__ == "__main__":
    main()
