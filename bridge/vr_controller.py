# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Fused virtual SteamVR controller — the bridge side of "VR mode".

Combines the available hand data into driver_cyberfinger's emulated
controllers (CYBERFINGER_L/R) over UDP:

  - 31-bone hand skeleton + finger curls (SteamVR hand tracking, read by
    OpenVRSkeletonSource) → HTSK packets → VRDriverInput skeleton component
  - controller buttons / joystick / trigger / battery (BLE HandState) → CFGP
    packets → controller inputs
  - 6DOF pose: the hand device's pose in the RAW tracking universe → the
    HTSK pos/quat fields with flags bit0 set. The driver prefers this over
    its legacy "find a device whose serial contains Hand_Left/Hand_Right"
    path, which stays as a fallback. Sending raw-universe poses is what
    makes the driver's grip correction (SteamVR Settings > CyberFinger)
    meaningful: it converts wrist frame → controller grip frame.

Wire formats mirror src/HandTrackingReceiver.h exactly (little-endian,
packed): HTSK v1 is 924 bytes, CFGP is 12 bytes, both to the same UDP port
(default 27015, driver setting handtracking_udp_port). The driver treats
skeleton data as stale after 150 ms and gamepad data after 250 ms, so both
are streamed continuously from a thread here rather than event-driven.

Future work (deliberately out of scope for now): fusing the controller IMU
orientations with the camera skeleton — per-source confidence, drift/depth
uncertainty estimation, and graceful handover when the hands leave camera
view. The seam for it is _compose(): today it passes the camera skeleton
through verbatim; a fusion estimator replaces that one function without
touching the wire protocol or the GUI.
"""

import socket
import struct
import threading
import time

HTSK_FMT = "<IBBBB3f4f217f5f"
HTSK_MAGIC = 0x4B535448  # 'HTSK'
CFGP_FMT = "<IBBhhBB"
CFGP_MAGIC = 0x50474643  # 'CFGP'
POSE_VALID_FLAG = 0x01   # HTSK flags bit0 — pos/quat carry a real pose

assert struct.calcsize(HTSK_FMT) == 924, "HTSK layout drifted from the driver"
assert struct.calcsize(CFGP_FMT) == 12, "CFGP layout drifted from the driver"

# BLE controller report bits (see cyberfinger_gui BTN_*) → driver CFGP bits
# (MergedController::UpdateInputs). The old VRMode passed the raw device
# bitmask through, which by coincidence mapped D→joystick-click and dropped
# the real stick click; this table makes the mapping deliberate.
#
#   TRIG   0x01 → driver 0x01 trigger
#   GRIP   0x02 → driver 0x02 grip
#   C      0x04 → driver 0x04 B button
#   E      0x10 → driver 0x10 A button
#   JCLICK 0x40 → driver 0x08 joystick click
#   (D, MENU, ST/SEL have no driver-side target yet)
_BUTTON_MAP = (
    (0x01, 0x01),
    (0x02, 0x02),
    (0x04, 0x04),
    (0x10, 0x10),
    (0x40, 0x08),
)

IDENTITY_BONE = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def map_buttons(ble_buttons):
    out = 0
    for src, dst in _BUTTON_MAP:
        if ble_buttons & src:
            out |= dst
    return out


class FusedVRMode:
    """Streams HTSK + CFGP per hand to the cyberfinger driver.

    Duck-typed like the other bridge modes: on_input()/stop(), plus start().
    on_input is a no-op — the send loop runs on its own thread and pulls the
    freshest state itself, because the driver needs continuous packets even
    while a controller or the camera skeleton drops out.
    """

    RATE_HZ = 60.0

    def __init__(self, skeleton, ble, log=None, host="127.0.0.1", port=27015):
        self.skeleton = skeleton      # OpenVRSkeletonSource or None
        self.ble = ble                # BLEManager with .left/.right HandState
        self._log = log or (lambda msg: None)
        self.target = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._running = False
        self._thread = None

    def start(self):
        if self._running:
            return
        if self.skeleton is not None:
            self.skeleton.feed_enabled = True
        self._log_pose_source_candidates()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._log(f"VR fusion: streaming to driver at {self.target[0]}:{self.target[1]}")

    def on_input(self, hand, state):
        pass  # send loop pulls state itself

    def stop(self):
        self._running = False
        if self.skeleton is not None:
            self.skeleton.feed_enabled = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self.sock.close()
        except Exception:
            pass

    # ── send loop ──

    def _loop(self):
        period = 1.0 / self.RATE_HZ
        while self._running:
            for hand, state in ((0, self.ble.left), (1, self.ble.right)):
                try:
                    self._send_cfgp(hand, state)
                    feed = (self.skeleton.driver_feed[hand]
                            if self.skeleton is not None else None)
                    if feed is not None:
                        self._send_htsk(hand, feed)
                except Exception:
                    pass  # transient socket errors must not kill the stream
            time.sleep(period)

    def _send_cfgp(self, hand, state):
        pkt = struct.pack(CFGP_FMT, CFGP_MAGIC, hand,
                          map_buttons(state.buttons),
                          state.joy_x, state.joy_y,
                          state.trigger, state.battery)
        self.sock.sendto(pkt, self.target)

    def _send_htsk(self, hand, feed):
        bones, curls, confidence, pose, pose_valid = self._compose(hand, feed)
        flat = [v for bone in bones for v in bone]
        pos, quat = pose
        flags = POSE_VALID_FLAG if pose_valid else 0
        pkt = struct.pack(HTSK_FMT, HTSK_MAGIC, 1, hand, confidence, flags,
                          *pos, *quat, *flat, *curls)
        self.sock.sendto(pkt, self.target)

    @staticmethod
    def _compose(hand, feed):
        """Produce the bone set to send. Today: camera skeleton verbatim.

        This is the seam where IMU+skeleton fusion goes later — blending the
        controller IMU orientations into the wrist/finger chain with per-source
        uncertainty, and carrying the skeleton (and wrist pose) through
        camera dropouts. Signature stays:
        (bones31x7, curls5, confidence, (pos, quat), pose_valid).
        """
        bones, curls, confidence, pose, pose_valid = feed
        if len(bones) < 31:
            bones = tuple(bones) + (IDENTITY_BONE,) * (31 - len(bones))
        return bones, curls, confidence, pose, pose_valid

    # ── diagnostics ──

    def _log_pose_source_candidates(self):
        """The driver poses its controllers from an external device whose
        serial contains Hand_Left/Hand_Right. Log what's actually visible so
        a mismatch (controllers stuck at the fallback position) is
        explainable from the console."""
        skel = self.skeleton
        if skel is None or getattr(skel, "_system", None) is None:
            return
        try:
            import openvr
            found = []
            for idx in range(openvr.k_unMaxTrackedDeviceCount):
                if not skel._system.isTrackedDeviceConnected(idx):
                    continue
                try:
                    serial = skel._system.getStringTrackedDeviceProperty(
                        idx, openvr.Prop_SerialNumber_String)
                except Exception:
                    continue
                if "hand" in serial.lower() and "CYBERFINGER" not in serial:
                    found.append(serial)
            if found:
                self._log("VR fusion: pose source candidates: "
                          + ", ".join(found)
                          + " (driver matches serials containing "
                            "'Hand_Left'/'Hand_Right')")
            else:
                self._log("VR fusion: no external hand-serial device found — "
                          "driver will use its fixed fallback pose")
        except Exception:
            pass
