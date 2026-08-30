# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""
CyberFinger Bridge GUI — Windows

Combines VR (fused controllers + hand skeleton → SteamVR driver) and Gamepad
(BLE→ViGEm Xbox 360) bridge modes into a single application with visual
feedback, system tray icon, and optional auto-start.

Only the Windows-specific layers live here — BLE over WinRT, the ViGEm virtual
gamepad, tray icon loading and the app shell. The protocol, SlimeVR emulation,
OpenVR skeleton client, driver stream and visualisation panels are shared with
the Linux bridge in bridge_common/.
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

# bridge_common lives beside this package in the repo; PyInstaller picks it up
# via `pathex` in the spec, so this only matters when running from source.
# APPENDED, never inserted: the repo root also holds the OpenVR SDK's `openvr/`
# directory, and ahead of site-packages that dir imports as a namespace package
# which shadows pyopenvr — satisfying `import openvr` with an API-less module.
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
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

try:
    from pynput.keyboard import Key, Controller as KeyboardController
    _keyboard = KeyboardController()
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False


def ibuffer_to_bytes(ibuffer):
    from winrt.windows.storage.streams import DataReader
    dr = DataReader.from_buffer(ibuffer)
    length = dr.unconsumed_buffer_length
    result = bytearray()
    for _ in range(length):
        result.append(dr.read_byte())
    return bytes(result)


def resource_path(relative):
    """Get path to a bridge/ resource, works for dev and PyInstaller."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative)


# ── Tray icon image helpers ──────────────────────────────────────────────

def _load_tray_icon_running():
    """Load the color (running) tray icon."""
    try:
        return Image.open(resource_path(os.path.join("assets", "icon_32x32.png")))
    except Exception:
        return _generate_fallback_icon((230, 0, 126))


def _load_tray_icon_idle():
    """Load the B&W (idle/stopped) tray icon."""
    try:
        return Image.open(resource_path(os.path.join("assets", "icon_32x32_bw.png")))
    except Exception:
        return _generate_fallback_icon((128, 128, 128))


def _generate_fallback_icon(color):
    """Generate a simple 32x32 circle icon as fallback."""
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, 30, 30], fill=color, outline=(255, 255, 255, 200), width=1)
    return img


# ── BLE discovery + subscription (runs in asyncio thread) ────────────────

class BLEManager:
    """Manages BLE connections in a background asyncio thread (WinRT GATT)."""

    def __init__(self, app):
        self.app = app
        self.left = HandState()
        self.right = HandState()
        self._thread = None
        self._loop = None
        self._running = False
        self._subscriptions = []
        self._polling_chars = []
        self._ble_devices = []     # track opened BLE device handles
        self._gatt_services = []   # track opened GATT service handles
        self._warned_report_len = False  # one-shot report-length mismatch warning

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            if str(e) != "Event loop stopped before Future completed.":
                self.app.log(f"BLE thread error: {e}")
        finally:
            # Clean up: unsubscribe notifications
            for _, char, token in self._subscriptions:
                try:
                    char.remove_value_changed(token)
                except Exception:
                    pass
            self._subscriptions = []
            self._polling_chars = []
            # Close GATT service handles FIRST (they hold exclusive locks)
            for svc in self._gatt_services:
                try:
                    svc.close()
                except Exception:
                    pass
            self._gatt_services = []
            # Then close BLE device handles
            for ble_dev in self._ble_devices:
                try:
                    ble_dev.close()
                except Exception:
                    pass
            self._ble_devices = []
            # Give Windows time to release BLE handles
            time.sleep(0.5)
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    async def _main(self):
        self.app.log("Scanning for CyberFinger devices...")
        self.app.set_status("Scanning...")

        left_dev, right_dev = await self._find_devices()

        if not left_dev and not right_dev:
            self.app.log("No CyberFinger devices found!")
            self.app.set_status("No devices found")
            return

        self._subscriptions = []
        self._polling_chars = []

        if left_dev:
            mac, name, ble_dev = left_dev
            self._ble_devices.append(ble_dev)
            self.left.name = name
            self.left.address = mac
            self.left.connected = True
            self.left.reset_link()
            result = await self._setup_device("LEFT", ble_dev)
            if result:
                mode, char, token = result
                if mode == "notify":
                    self._subscriptions.append(("LEFT", char, token))
                else:
                    self._polling_chars.append(("LEFT", char))

        if right_dev:
            mac, name, ble_dev = right_dev
            self._ble_devices.append(ble_dev)
            self.right.name = name
            self.right.address = mac
            self.right.connected = True
            self.right.reset_link()
            result = await self._setup_device("RIGHT", ble_dev)
            if result:
                mode, char, token = result
                if mode == "notify":
                    self._subscriptions.append(("RIGHT", char, token))
                else:
                    self._polling_chars.append(("RIGHT", char))

        if not self._subscriptions and not self._polling_chars:
            self.app.log("Failed to establish data channels!")
            self.app.set_status("Connection failed")
            return

        count = len(self._subscriptions) + len(self._polling_chars)
        self.app.log(f"Connected! {count} channel(s) active")
        self.app.set_status("Connected")

        while self._running:
            for _, char in self._polling_chars:
                await self._poll_char(char)
            if self._polling_chars:
                await asyncio.sleep(0.01)
            else:
                await asyncio.sleep(0.1)

    def _handle_data(self, data):
        report = parse_report(data)
        if report is None:
            return

        # The report carries its own hand byte here: WinRT delivers both
        # devices' notifications through one manager, so the packet is the
        # authority on which controller sent it.
        h = min(report.hand, 1)
        state = self.left if h == 0 else self.right
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

    async def _find_devices(self):
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothConnectionStatus

        all_devices = await DeviceInformation.find_all_async()
        self.app.log(f"System devices: {len(all_devices)}")

        cf_ble_entries = []
        for dev in all_devices:
            name = dev.name or ""
            dev_id = dev.id or ""
            if "cyberfinger" in name.lower() and "bthledevice" in dev_id.lower():
                cf_ble_entries.append((name, dev_id))

        self.app.log(f"CyberFinger BLE entries: {len(cf_ble_entries)}")
        if not cf_ble_entries:
            return None, None

        seen = {}
        for enum_name, dev_id in cf_ble_entries:
            try:
                ble_dev = await BluetoothLEDevice.from_id_async(dev_id)
                if not ble_dev:
                    continue
                raw = ble_dev.bluetooth_address
                mac = ":".join(f"{(raw >> (8*i)) & 0xFF:02X}" for i in range(5, -1, -1))
                connected = (ble_dev.connection_status == BluetoothConnectionStatus.CONNECTED)
                if mac not in seen or (connected and not seen[mac][1]):
                    seen[mac] = (enum_name, connected, ble_dev)
            except Exception:
                pass

        if not seen:
            return None, None

        for mac, (enum_name, connected, _) in sorted(seen.items()):
            status = "CONNECTED" if connected else "disconnected"
            self.app.log(f"  {status}: \"{enum_name}\" {mac}")

        left_dev = right_dev = None
        for mac, (enum_name, connected, ble_dev) in seen.items():
            if not connected:
                continue
            nl = enum_name.lower()
            if not left_dev and "left" in nl:
                left_dev = (mac, enum_name, ble_dev)
                self.app.log(f"  LEFT  ← \"{enum_name}\"")
            elif not right_dev and "right" in nl:
                right_dev = (mac, enum_name, ble_dev)
                self.app.log(f"  RIGHT ← \"{enum_name}\"")

        return left_dev, right_dev

    async def _setup_device(self, label, ble_dev):
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus,
            GattClientCharacteristicConfigurationDescriptorValue,
        )

        # Retry GATT service discovery (important for reconnect after stop)
        svc_result = None
        for attempt in range(3):
            try:
                svc_result = await ble_dev.get_gatt_services_async()
                if svc_result.status == GattCommunicationStatus.SUCCESS:
                    break
            except Exception as e:
                self.app.log(f"{label}: GATT attempt {attempt+1}/3 error: {e}")
            self.app.log(f"{label}: GATT services attempt {attempt+1}/3 failed, retrying...")
            await asyncio.sleep(0.5)

        if not svc_result or svc_result.status != GattCommunicationStatus.SUCCESS:
            self.app.log(f"{label}: Failed to get GATT services after 3 attempts")
            return None

        vr_svc = None
        for svc in svc_result.services:
            if "cf00" in str(svc.uuid).lower():
                vr_svc = svc
                break
        if not vr_svc:
            self.app.log(f"{label}: 0xCF00 service not found!")
            return None

        # Track service handle for cleanup (CRITICAL for reconnect)
        self._gatt_services.append(vr_svc)

        # Retry characteristics discovery
        char_result = None
        for attempt in range(3):
            try:
                char_result = await vr_svc.get_characteristics_async()
                if char_result.status == GattCommunicationStatus.SUCCESS:
                    break
            except Exception as e:
                self.app.log(f"{label}: Characteristics attempt {attempt+1}/3 error: {e}")
            await asyncio.sleep(0.3)

        if not char_result or char_result.status != GattCommunicationStatus.SUCCESS:
            self.app.log(f"{label}: Failed to get characteristics (status: {char_result.status if char_result else 'None'})")
            return None

        vr_input = None
        for char in char_result.characteristics:
            if "cf01" in str(char.uuid).lower():
                vr_input = char
                break
        if not vr_input:
            self.app.log(f"{label}: CF01 characteristic not found")
            return None

        # Clear any stale CCCD from previous session
        try:
            await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NONE
            )
        except Exception:
            pass
        await asyncio.sleep(0.1)

        mgr = self

        def on_notify(sender, args):
            try:
                data = ibuffer_to_bytes(args.characteristic_value)
                mgr._handle_data(data)
            except Exception:
                pass

        try:
            cccd_result = await vr_input.write_client_characteristic_configuration_descriptor_async(
                GattClientCharacteristicConfigurationDescriptorValue.NOTIFY
            )
            if cccd_result == GattCommunicationStatus.SUCCESS:
                token = vr_input.add_value_changed(on_notify)
                self.app.log(f"{label}: Notifications active")
                return ("notify", vr_input, token)
            else:
                self.app.log(f"{label}: Using polling mode")
                return ("poll", vr_input, None)
        except Exception:
            self.app.log(f"{label}: Notify failed, polling")
            return ("poll", vr_input, None)

    async def _poll_char(self, char):
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattCommunicationStatus
        try:
            result = await char.read_value_async()
            if result.status == GattCommunicationStatus.SUCCESS:
                data = ibuffer_to_bytes(result.value)
                self._handle_data(data)
        except Exception:
            pass


# ── VR Mode ──────────────────────────────────────────────────────────────
#
# Lives in bridge_common/vr_controller.py (FusedVRMode): fuses the runtime hand
# skeleton, device 6DOF and controller buttons into driver_cyberfinger's
# emulated controllers. The old CFGP-only VRMode is subsumed by it — with no
# skeleton source available, FusedVRMode degrades to exactly that behavior.


# ── Gamepad Mode (ViGEm Xbox 360) ────────────────────────────────────────

class GamepadMode:
    def __init__(self):
        self.gamepad = None
        self.available = False
        try:
            import vgamepad as vg
            self.vg = vg
            self.gamepad = vg.VX360Gamepad()
            self.available = True
        except ImportError:
            pass
        except Exception:
            pass

    def on_input(self, hand, state):
        pass  # update_gamepad called by app

    def update_gamepad(self, left, right):
        if not self.available:
            return
        vg = self.vg
        gp = self.gamepad

        gp.reset()

        # Sticks (Y inverted)
        gp.left_joystick_float(x_value_float=left.joy_x_float, y_value_float=-left.joy_y_float)
        gp.right_joystick_float(x_value_float=right.joy_x_float, y_value_float=-right.joy_y_float)

        # Triggers (analog)
        gp.left_trigger_float(value_float=left.trigger_float)
        gp.right_trigger_float(value_float=right.trigger_float)

        # ── Right hand (original assignments preserved) ──
        if right.buttons & BTN_TRIGGER:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)           # btn 1
        if right.buttons & BTN_GRIP:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_B)           # btn 2
        if right.buttons & BTN_MENU:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER) # btn 6
        if right.buttons & BTN_JCLICK:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB) # btn 10
        if right.buttons & BTN_STSEL:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_START)       # btn 8

        # ── Left hand (original assignments preserved) ──
        if left.buttons & BTN_TRIGGER:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_X)           # btn 3
        if left.buttons & BTN_GRIP:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)           # btn 4
        if left.buttons & BTN_MENU:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER) # btn 5
        if left.buttons & BTN_JCLICK:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)  # btn 9
        if left.buttons & BTN_STSEL:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK)        # btn 7

        # ── New C/D/E buttons — raw wButtons bits (11-16) ──
        # Xbox 360 wButtons is a 16-bit field; bits 11-15 are unused by XInput
        # and pass through ViGEm, appearing as buttons 11-16 in DirectInput.
        # bit 11 = 0x0800 (reserved, unused by XInput)
        # bit 12 = 0x1000 ... already XUSB_GAMEPAD_A — so we use D-pad bits
        # instead, which are free in this mapping (no d-pad inputs assigned):
        # DPAD_UP=0x0001(btn11), DPAD_DOWN=0x0002(btn12), DPAD_LEFT=0x0004(btn13)
        # DPAD_RIGHT=0x0008(btn14), GUIDE=0x0400(btn15), reserved=0x0800(btn16)
        if right.buttons & BTN_C:
            gp.report.wButtons |= 0x0001  # DPAD_UP   → btn 11 (R-C)
        if right.buttons & BTN_D:
            gp.report.wButtons |= 0x0002  # DPAD_DOWN → btn 12 (R-D)
        if right.buttons & BTN_E:
            gp.report.wButtons |= 0x0004  # DPAD_LEFT → btn 13 (R-E)
        if left.buttons & BTN_C:
            gp.report.wButtons |= 0x0008  # DPAD_RIGHT → btn 14 (L-C)
        if left.buttons & BTN_D:
            gp.report.wButtons |= 0x0400  # GUIDE      → btn 15 (L-D)
        if left.buttons & BTN_E:
            gp.report.wButtons |= 0x0800  # reserved   → btn 16 (L-E)

        gp.update()

    def stop(self):
        if self.gamepad:
            self.gamepad.reset()
            self.gamepad.update()


# ── Gamepad Mode — VRChat optimised ──────────────────────────────────────

class GamepadModeVRChat:
    """
    ViGEm Xbox 360 gamepad with VRChat-optimal button mapping.

    VRChat gamepad layout (Xbox reference):
      Left  stick          → Move (head-relative in VR)
      Right stick X        → Smooth turn
      Right stick Y        → Look up/down
      RT (right trigger)   → Use / Interact  (right hand)
      LT (left trigger)    → Use / Interact  (left hand)
      A                    → Jump
      B / Y                → Quick Menu
      R3 (right stick click) → Action Menu
      L3 (left  stick click) → Action Menu (left)
      X                    → Mute toggle

    CyberFinger → Xbox mapping:
      Right TRIGGER  → RT  (use/interact right)
      Left  TRIGGER  → LT  (use/interact left)
      Right GRIP     → A   (jump)
      Left  GRIP     → B   (quick menu)
      Right MENU     → Y   (quick menu right)
      Left  MENU     → X   (mute)
      Right JCLICK   → R3  (action menu right)
      Left  JCLICK   → L3  (action menu left)
      Right C        → RB  (extra / world-specific)
      Left  C        → LB  (extra / world-specific)
      Right D        → DPAD_RIGHT
      Left  D        → DPAD_LEFT
      Right E / Left E → DPAD_UP / DPAD_DOWN
      ST/SE (either) → Start
    """

    def __init__(self):
        self.gamepad = None
        self.available = False
        try:
            import vgamepad as vg
            self.vg = vg
            self.gamepad = vg.VX360Gamepad()
            self.available = True
        except ImportError:
            pass
        except Exception:
            pass

        # OSC client for per-hand UseLeft/UseRight — VRChat gamepad mode has
        # no separate left hand interact, but OSC UseLeft/UseRight works in VR
        # and can run simultaneously alongside the gamepad input.
        self._osc = None
        try:
            from pythonosc import udp_client
            self._osc = udp_client.SimpleUDPClient("127.0.0.1", 9000)
        except ImportError:
            pass

        self._prev_use_r = False
        self._prev_use_l = False
        self._prev_grab_r = False
        self._prev_grab_l = False
        self._grab_right_toggled = False
        self._grab_left_toggled  = False
        self._grab_right_press_time = 0.0
        self._grab_left_press_time  = 0.0
        self._prev_jclick_r = False
        self._prev_jclick_l = False
        self._prev_stsel_l  = False
        self._prev_stsel_r  = False
        self._prev_c_r      = False

    def _osc_send(self, address, value):
        if self._osc:
            try:
                self._osc.send_message(address, value)
            except Exception:
                pass

    def on_input(self, hand, state):
        pass  # update_gamepad called by app

    def update_gamepad(self, left, right):
        if not self.available:
            return
        vg = self.vg
        gp = self.gamepad

        gp.reset()

        # ── Sticks ────────────────────────────────────────────────────
        gp.left_joystick_float(x_value_float=left.joy_x_float,
                               y_value_float=-left.joy_y_float)
        gp.right_joystick_float(x_value_float=right.joy_x_float,
                                y_value_float=-right.joy_y_float)

        # ── Triggers ───────────────────────────────────────────────────
        # Right trigger → RT (gamepad, right hand interact)
        # Left  trigger → OSC /input/UseLeft only (no LT gamepad — avoids
        #                 duplicate/conflicting events with right hand)
        trig_r = max(right.trigger_float, 1.0 if (right.buttons & BTN_TRIGGER) else 0.0)
        trig_l = max(left.trigger_float,  1.0 if (left.buttons  & BTN_TRIGGER) else 0.0)
        gp.right_trigger_float(value_float=trig_r)
        gp.left_trigger_float(value_float=0.0)      # suppressed — UseLeft via OSC

        # ── OSC: UseLeft (left trigger) — no gamepad equivalent ────────
        use_l = trig_l > 0.1
        if use_l != self._prev_use_l:
            self._osc_send("/input/UseLeft", int(use_l))
            self._prev_use_l = use_l

        # ── OSC: GrabRight / GrabLeft (button 2 = GRIP) ────────────────
        # Tap  (<200ms): toggle grab state
        # Hold (≥200ms): release on finger-up
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

        # ── Stick clicks ───────────────────────────────────────────────
        # Right jclick → Jump (gamepad A)
        # Left  jclick → Jump (gamepad A, same button — either hand jumps)
        jclick_r = bool(right.buttons & BTN_JCLICK)
        jclick_l = bool(left.buttons  & BTN_JCLICK)
        if jclick_r or jclick_l:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_A)
        self._prev_jclick_r = jclick_r
        self._prev_jclick_l = jclick_l

        # ── Right hand (gamepad) ────────────────────────────────────────
        if right.buttons & BTN_MENU:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB)   # Action menu R
        # ── Right C → F12 screenshot (rising edge) ─────────────────────
        c_r = bool(right.buttons & BTN_C)
        if c_r and not self._prev_c_r:
            if HAS_PYNPUT:
                try:
                    _keyboard.press(Key.f12)
                    _keyboard.release(Key.f12)
                except Exception:
                    pass
        self._prev_c_r = c_r
        if right.buttons & BTN_D:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT)
        if right.buttons & BTN_E:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP)
        # Right ST/SE → open VRChat chatbox keyboard (rising edge)
        stsel_r = bool(right.buttons & BTN_STSEL)
        if stsel_r and not self._prev_stsel_r:
            # b=False opens the keyboard, n=False suppresses notification SFX
            self._osc_send("/chatbox/input", ["", False, False])
        self._prev_stsel_r = stsel_r

        # ── Left hand (gamepad + OSC) ────────────────────────────────────
        # Left MENU → Start (Quick Menu, gamepad)
        if left.buttons & BTN_MENU:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_START)
        # BTN_GRIP → OSC GrabLeft (no gamepad event)
        if left.buttons & BTN_C:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_X)             # Mute
        if left.buttons & BTN_D:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT)
        if left.buttons & BTN_E:
            gp.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN)
        # ST/SE → /input/Voice mute toggle: 1 on press, 0 on release
        stsel_l = bool(left.buttons & BTN_STSEL)
        if stsel_l != self._prev_stsel_l:
            self._osc_send("/input/Voice", int(stsel_l))
        self._prev_stsel_l = stsel_l

        gp.update()

    def stop(self):
        if self.gamepad:
            self.gamepad.reset()
            self.gamepad.update()
        for addr in ("/input/UseLeft", "/input/GrabRight", "/input/GrabLeft",
                     "/input/QuickMenuToggleRight", "/input/Voice"):
            self._osc_send(addr, 0)


# ── GUI Application ──────────────────────────────────────────────────────

class CyberFingerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("CyberFinger Bridge")
        self.root.configure(bg=COLOR_BG)
        self.root.geometry("680x790")
        self.root.minsize(600, 660)

        # Set window icon (color version)
        try:
            icon_path = resource_path(os.path.join("assets", "icon_32x32.png"))
            icon_img = tk.PhotoImage(file=icon_path)
            self.root.iconphoto(True, icon_img)
            self._icon_ref = icon_img  # prevent GC
        except Exception:
            pass

        self.log_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self._current_status = "Idle"
        self._window_visible = True

        # Config persistence
        self._config_dir = config_dir()
        self._config_path = os.path.join(self._config_dir, "settings.json")
        self._config = self._load_config()

        self.ble = BLEManager(self)
        self.vr_mode = None              # created lazily on start (FusedVRMode)
        self.gamepad_mode = None         # created lazily on first use
        self.vrchat_gamepad_mode = None  # created lazily on first use
        self.active_mode = None
        self.slimevr = None              # created lazily while forwarding is on
        # Config gate ("skeleton_enabled": false in settings.json) exists so
        # the OpenVR client can be ruled in/out when debugging SteamVR-side
        # trouble without touching code.
        self.skeleton = (create_skeleton_source(
                             self.log, self._config.get("skeleton_bisect", False),
                             self._config.get("skeleton_backend", "auto"))
                         if self._config.get("skeleton_enabled", True) else None)

        self._build_ui()

        if self.skeleton:
            self.skeleton.start()

        # System tray icon
        self._tray_icon = None
        if HAS_TRAY:
            self._setup_tray()

        self._poll_queues()

        # X button minimizes to tray (if available), otherwise saves and quits
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Auto-start if enabled
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
            "CyberFinger Bridge — Idle",
            menu
        )

        tray_thread = threading.Thread(target=self._tray_icon.run, daemon=True)
        tray_thread.start()

    def _set_tray_running(self, running):
        """Switch tray icon between running (color) and idle (B&W)."""
        if self._tray_icon:
            try:
                self._tray_icon.icon = self._tray_icon_running if running else self._tray_icon_idle
            except Exception:
                pass

    def _update_tray_tooltip(self):
        if self._tray_icon:
            mode = self.mode_var.get().upper() if hasattr(self, 'mode_var') else ""
            self._tray_icon.title = f"CyberFinger Bridge — {self._current_status}" + \
                                    (f" ({mode})" if self.active_mode else "")

    def _tray_toggle_window(self, icon=None, item=None):
        """Left-click on tray icon: toggle window visibility."""
        if self._window_visible:
            self.root.after(0, self._hide_window)
        else:
            self.root.after(0, self._show_window)

    def _tray_start(self, icon=None, item=None):
        self.root.after(0, self._start_bridge)

    def _tray_stop(self, icon=None, item=None):
        self.root.after(0, self._stop_bridge)

    def _tray_exit(self, icon=None, item=None):
        """Exit from tray context menu — full shutdown."""
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
        """X button pressed — minimize to tray if available, else quit."""
        if HAS_TRAY and self._tray_icon:
            self._hide_window()
            self.log("Minimized to system tray")
        else:
            self._quit_app()

    def _quit_app(self):
        """Full application shutdown."""
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
        ttk.Label(header, text="⬡ CyberFinger Bridge", style="Title.TLabel").pack(side=tk.LEFT)
        self.status_label = ttk.Label(header, text="Idle", style="Status.TLabel")
        self.status_label.pack(side=tk.RIGHT)

        # ── Mode selection + Start/Stop ──
        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill=tk.X, padx=16, pady=(4, 4))

        # Radio buttons stacked vertically on the left
        self.mode_var = tk.StringVar(value=self._config.get("mode", "vr"))
        radio_frame = ttk.Frame(ctrl_frame)
        radio_frame.pack(side=tk.LEFT)
        ttk.Radiobutton(radio_frame, text="VR Mode (controllers + hand tracking → SteamVR)",
                        variable=self.mode_var, value="vr").pack(anchor=tk.W)
        ttk.Radiobutton(radio_frame, text="Gamepad Mode (BLE→Xbox 360, Resonite)",
                        variable=self.mode_var, value="gamepad").pack(anchor=tk.W)
        ttk.Radiobutton(radio_frame, text="Gamepad Mode (BLE→Xbox 360, VRChat)",
                        variable=self.mode_var, value="gamepad_vrc").pack(anchor=tk.W)

        # Start/Stop buttons on the right
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
            return  # Already running

        mode = self.mode_var.get()
        if mode in ("gamepad", "gamepad_vrc"):
            try:
                import vgamepad  # noqa — just check it's importable
            except ImportError:
                self.log("ERROR: vgamepad not available!")
                self.log("Install: pip install vgamepad")
                self.log("Also need ViGEmBus driver")
                return

        self._config["mode"] = mode
        self._config["autostart"] = self.autostart_var.get()
        self._config["slimevr_enabled"] = self.slimevr_var.get()
        self._config["slimevr_body_imu"] = self.slimevr_body_var.get()
        self._save_config()

        if mode == "gamepad":
            self.gamepad_mode = GamepadMode()
            self.active_mode = self.gamepad_mode
        elif mode == "gamepad_vrc":
            self.vrchat_gamepad_mode = GamepadModeVRChat()
            self.active_mode = self.vrchat_gamepad_mode
        else:  # "vr" and fallback
            self.vr_mode = FusedVRMode(self.skeleton, self.ble, self.log)
            self.vr_mode.start()
            self.active_mode = self.vr_mode
        self.log(f"Starting {mode.upper()} mode...")
        if mode == "gamepad_vrc":
            self.log(">>> VRChat: enable OSC via Action Menu → OSC → Enabled")
            self.log(">>> VRChat window must be focused for Use/Grab to work")

        if self.slimevr_var.get():
            self._start_slimevr()

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._set_tray_running(True)

        self.ble.start()

    def _stop_bridge(self):
        if not self.active_mode:
            return  # Not running

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
        self.vr_mode = None              # recreated lazily on next start
        self.gamepad_mode = None         # recreated lazily on next start
        self.vrchat_gamepad_mode = None  # recreated lazily on next start

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
        """Process log/status messages on the main thread."""
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
        self.log("CyberFinger Bridge ready")
        try:
            import vgamepad  # noqa
            self.log("Gamepad mode: available")
        except ImportError:
            self.log("Gamepad mode: not available (install vgamepad + ViGEmBus)")
        if not HAS_TRAY:
            self.log("System tray: not available (install pystray pillow)")
        if not HAS_PYNPUT:
            self.log("Keyboard (F12 screenshot): not available (install pynput)")
        if not HAS_OPENVR:
            self.log("Hand skeleton: not available (pip install openvr)")
        self.root.mainloop()


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    app = CyberFingerApp()
    app.run()


if __name__ == "__main__":
    main()
