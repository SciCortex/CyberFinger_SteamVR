# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""CyberFinger BLE GATT protocol — wire format, decoding and hand state.

Mirrors CyberFingerFW_ESP32/src/vr_gatt.h. Both bridges decode notifications
through parse_report() here, so the firmware's format has exactly one reader.
"""

import struct
import time

# ── Service & characteristic UUIDs ───────────────────────────────────────

VR_SERVICE_UUID = "0000cf00-0000-1000-8000-00805f9b34fb"
VR_INPUT_UUID   = "0000cf01-0000-1000-8000-00805f9b34fb"
VR_CTRL_UUID    = "0000cf02-0000-1000-8000-00805f9b34fb"

# ── Control commands (written by the bridge to 0xCF02) ───────────────────

VR_CMD_ENTER_VR   = 0x01
VR_CMD_EXIT_VR    = 0x02
VR_CMD_QUERY_MODE = 0x03

# ── Notification payload ─────────────────────────────────────────────────

INPUT_REPORT_FMT = "<BBhhBBI"
INPUT_REPORT_SIZE = struct.calcsize(INPUT_REPORT_FMT)  # 12

# The report is VARIABLE-LENGTH (see vr_gatt.h). The first 28 bytes are frozen,
# then imu_present at offset 28, then one appended block per set bit, in bit
# order — absent IMUs are omitted entirely to cut latency. PARSE BY
# imu_present, NEVER BY TOTAL LENGTH: different slot combinations can produce
# the same byte count.
#
#   [0..27]  frozen prefix: hand..seq, then q[4] = PRIMARY body quaternion
#   [28]     imu_present bitmask
#   then, each only if its bit is set:
#     PRIMARY  (0x01): a_body1[3] int16   (6B)  — its quat is the header q
#     SECONDARY(0x02): q_body2[4] f + a_body2[3] int16   (22B)
#     JOINT    (0x04): q_joint[4] f + a_joint[3] int16   (22B)
#
# Legacy pre-imu_present firmware still parses: 12 bytes = base, no IMU;
# exactly 28 bytes = base + primary quat with no presence byte.
INPUT_REPORT_IMU_FMT = "<BBhhBBI4f"
INPUT_REPORT_IMU_SIZE = struct.calcsize(INPUT_REPORT_IMU_FMT)  # 28

IMU_PRESENT_OFFSET = INPUT_REPORT_IMU_SIZE  # 28
QUAT_FMT, QUAT_SIZE = "<4f", 16
ACCEL_FMT, ACCEL_SIZE = "<3h", 6  # raw int16 x/y/z, ACCEL_LSB_PER_G counts

ZERO_ACCEL = (0, 0, 0)
IDENTITY_QUAT = (1.0, 0.0, 0.0, 0.0)

ACCEL_LSB_PER_G = 2048.0  # VR_ACCEL_LSB_PER_G — ±16g on every sensor
GRAVITY_MS2 = 9.80665

# VrImuBit — which quaternion slots carry real data
IMU_BODY_PRIMARY   = 0x01
IMU_BODY_SECONDARY = 0x02
IMU_JOINT          = 0x04

# Appended block size per bit: PRIMARY is accel-only (its quat lives in the
# header); SECONDARY/JOINT carry quat + accel.
IMU_BLOCK_SIZE = {
    IMU_BODY_PRIMARY:   ACCEL_SIZE,
    IMU_BODY_SECONDARY: QUAT_SIZE + ACCEL_SIZE,
    IMU_JOINT:          QUAT_SIZE + ACCEL_SIZE,
}

IMU_SLOT_LABELS = (
    (IMU_BODY_PRIMARY,   "BODY 1"),
    (IMU_BODY_SECONDARY, "BODY 2"),
    (IMU_JOINT,          "JOINT"),
)

# ── Button bits ──────────────────────────────────────────────────────────

BTN_TRIGGER = 0x01  # bit0 — AX  (trigger)
BTN_GRIP    = 0x02  # bit1 — BY  (grip)
BTN_C       = 0x04  # bit2 — CZ
BTN_D       = 0x08  # bit3 — DD
BTN_E       = 0x10  # bit4 — EE
BTN_MENU    = 0x20  # bit5 — BP  (bumper/menu)
BTN_JCLICK  = 0x40  # bit6 — ST  (stick click)
BTN_STSEL   = 0x80  # bit7 — STARTSELECT

BUTTON_NAMES = {
    BTN_TRIGGER: "TRIG",
    BTN_GRIP:    "GRIP",
    BTN_C:       "C",
    BTN_D:       "D",
    BTN_E:       "E",
    BTN_MENU:    "MENU",
    BTN_JCLICK:  "JCLK",
    BTN_STSEL:   "ST/SE",
}

# Panel button rows, in display order.
BUTTON_ROWS = tuple((name, bit) for bit, name in (
    (BTN_TRIGGER, "TRIG"), (BTN_GRIP, "GRIP"), (BTN_C, "C"), (BTN_D, "D"),
    (BTN_E, "E"), (BTN_MENU, "MENU"), (BTN_JCLICK, "JCLK"),
    (BTN_STSEL, "ST/SE"),
))


def fmt_buttons(btn):
    parts = [name for bit, name in BUTTON_NAMES.items() if btn & bit]
    return "+".join(parts) if parts else "none"


def expected_report_len(present):
    """Total bytes a variable-length report with this imu_present should be."""
    n = IMU_PRESENT_OFFSET + 1  # frozen prefix + imu_present byte
    for bit in (IMU_BODY_PRIMARY, IMU_BODY_SECONDARY, IMU_JOINT):
        if present & bit:
            n += IMU_BLOCK_SIZE[bit]
    return n


def linear_accel_ms2(quat, accel_raw):
    """Gravity-corrected acceleration in the SENSOR frame, in m/s².

    Uses the quaternion from the same packet to rotate gravity into the sensor
    frame and subtract it. This is the derivation documented in vr_gatt.h, which
    is SlimeVR's own, so the result feeds straight into PACKET_ACCEL.
    """
    q0, q1, q2, q3 = quat
    gx = 2.0 * (q1 * q3 - q0 * q2)
    gy = 2.0 * (q0 * q1 + q2 * q3)
    gz = q0 * q0 - q1 * q1 - q2 * q2 + q3 * q3
    scale = GRAVITY_MS2 / ACCEL_LSB_PER_G
    return (accel_raw[0] * scale - gx * GRAVITY_MS2,
            accel_raw[1] * scale - gy * GRAVITY_MS2,
            accel_raw[2] * scale - gz * GRAVITY_MS2)


class Report:
    """One decoded CF01 notification.

    `expected_len` is what imu_present implies the payload should have been, or
    None on a legacy report carrying no presence byte. Callers compare it with
    the real length to catch a firmware/bridge format disagreement — an
    OVER-long payload still unpacks cleanly, so without that check it would
    mis-parse every slot in silence.
    """

    __slots__ = ("hand", "buttons", "joy_x", "joy_y", "trigger", "battery",
                 "seq", "imu_present", "quats", "accels", "has_accel",
                 "raw_len", "expected_len")

    def __init__(self, hand, buttons, joy_x, joy_y, trigger, battery, seq,
                 imu_present, quats, accels, has_accel, raw_len, expected_len):
        self.hand = hand
        self.buttons = buttons
        self.joy_x = joy_x
        self.joy_y = joy_y
        self.trigger = trigger
        self.battery = battery
        self.seq = seq
        self.imu_present = imu_present
        self.quats = quats      # (primary, secondary, joint)
        self.accels = accels    # same order, raw int16 triples
        self.has_accel = has_accel
        self.raw_len = raw_len
        self.expected_len = expected_len

    @property
    def length_ok(self):
        return self.expected_len is None or self.raw_len == self.expected_len


def parse_report(data):
    """Decode one CF01 notification into a Report, or None if it is too short.

    The frozen prefix is always present; IMU blocks are walked by imu_present.
    """
    if len(data) < INPUT_REPORT_SIZE:
        return None

    present = 0
    quats = [IDENTITY_QUAT, IDENTITY_QUAT, IDENTITY_QUAT]
    accels = [ZERO_ACCEL, ZERO_ACCEL, ZERO_ACCEL]
    has_accel = False
    expected = None

    if len(data) >= INPUT_REPORT_IMU_SIZE:
        (hand, buttons, joy_x, joy_y, trigger, battery, seq,
         qw, qx, qy, qz) = struct.unpack(
            INPUT_REPORT_IMU_FMT, data[:INPUT_REPORT_IMU_SIZE])
        quats[0] = (qw, qx, qy, qz)

        if len(data) > IMU_PRESENT_OFFSET:
            # Modern variable-length report: presence byte then blocks.
            present = data[IMU_PRESENT_OFFSET]
            expected = expected_report_len(present)
            off = IMU_PRESENT_OFFSET + 1
            try:
                # PRIMARY: accel only; its quaternion is the header q above.
                if present & IMU_BODY_PRIMARY:
                    accels[0] = struct.unpack_from(ACCEL_FMT, data, off)
                    off += ACCEL_SIZE
                    has_accel = True
                # SECONDARY / JOINT: quaternion then accel.
                for bit, slot in ((IMU_BODY_SECONDARY, 1), (IMU_JOINT, 2)):
                    if present & bit:
                        quats[slot] = struct.unpack_from(QUAT_FMT, data, off)
                        off += QUAT_SIZE
                        accels[slot] = struct.unpack_from(ACCEL_FMT, data, off)
                        off += ACCEL_SIZE
                        has_accel = True
            except struct.error:
                # Truncated block — keep what parsed, drop the presence bits we
                # couldn't back with data so downstream stays consistent. The
                # caller reports the mismatch via expected_len/length_ok.
                present = 0
                for bit, slot in ((IMU_BODY_PRIMARY, 0),
                                  (IMU_BODY_SECONDARY, 1), (IMU_JOINT, 2)):
                    if accels[slot] != ZERO_ACCEL or quats[slot] != IDENTITY_QUAT:
                        present |= bit
        else:
            # Legacy 28-byte report: primary quat, no presence byte. An all-zero
            # quaternion is the only signal the IMU failed to come up.
            if any(abs(v) > 1e-6 for v in quats[0]):
                present = IMU_BODY_PRIMARY
    else:
        hand, buttons, joy_x, joy_y, trigger, battery, seq = \
            struct.unpack(INPUT_REPORT_FMT, data[:INPUT_REPORT_SIZE])

    return Report(hand, buttons, joy_x, joy_y, trigger, battery, seq,
                  present, tuple(quats), tuple(accels), has_accel,
                  len(data), expected)


# ── Hand state ───────────────────────────────────────────────────────────

class HandState:
    """Latest input from one controller, as the GUI and the modes read it."""

    def __init__(self):
        self.buttons = 0
        self.joy_x = 0
        self.joy_y = 0
        self.trigger = 0
        self.battery = 100
        self.packet_count = 0
        self.timestamp = 0.0
        self.connected = False
        self.name = ""
        self.address = ""
        # Orientation — absent slots stay identity. imu_present is 0 on older
        # firmware and on units with no working IMU, which is what drives the
        # "no IMU installed" placeholder in the panel.
        self.imu_present = 0
        self.quat = IDENTITY_QUAT        # primary body IMU
        self.quat_body2 = IDENTITY_QUAT  # secondary body IMU
        self.quat_joint = IDENTITY_QUAT  # joint IMU
        # Raw sensor-frame acceleration per slot, ACCEL_LSB_PER_G counts. Only
        # meaningful when has_accel is set — firmware predating the 79-byte
        # report leaves these zero, which is NOT the same as "at rest".
        self.has_accel = False
        self.accel = ZERO_ACCEL
        self.accel_body2 = ZERO_ACCEL
        self.accel_joint = ZERO_ACCEL

    @property
    def joy_x_float(self):
        return max(-1.0, min(1.0, self.joy_x / 32767.0))

    @property
    def joy_y_float(self):
        return max(-1.0, min(1.0, self.joy_y / 32767.0))

    @property
    def trigger_float(self):
        if self.trigger > 10:
            return self.trigger / 255.0
        return 1.0 if (self.buttons & BTN_TRIGGER) else 0.0

    def reset_link(self):
        """Clear per-connection capability flags before (re)attaching a unit."""
        self.imu_present = 0
        self.quat = IDENTITY_QUAT
        self.quat_body2 = IDENTITY_QUAT
        self.quat_joint = IDENTITY_QUAT
        self.has_accel = False
        self.accel = ZERO_ACCEL
        self.accel_body2 = ZERO_ACCEL
        self.accel_joint = ZERO_ACCEL

    @property
    def has_imu(self):
        return self.imu_present != 0

    def active_imus(self):
        """[(label, quat)] for slots this unit actually populates, in wire order."""
        quats = {
            IMU_BODY_PRIMARY:   self.quat,
            IMU_BODY_SECONDARY: self.quat_body2,
            IMU_JOINT:          self.quat_joint,
        }
        return [(label, quats[bit]) for bit, label in IMU_SLOT_LABELS
                if self.imu_present & bit]

    def apply(self, report):
        """Fold a decoded Report into this state. Returns (buttons_changed,
        imu_changed) so the caller can log only real transitions."""
        buttons_changed = report.buttons != self.buttons
        imu_changed = report.imu_present != self.imu_present

        self.imu_present = report.imu_present
        self.quat, self.quat_body2, self.quat_joint = report.quats
        self.has_accel = report.has_accel
        self.accel, self.accel_body2, self.accel_joint = report.accels

        self.buttons = report.buttons
        self.joy_x = report.joy_x
        self.joy_y = report.joy_y
        self.trigger = report.trigger
        self.battery = report.battery
        self.timestamp = time.time()
        self.packet_count += 1

        return buttons_changed, imu_changed


def describe_imus(present):
    """Human-readable slot list for a presence bitmask."""
    names = [label for bit, label in IMU_SLOT_LABELS if present & bit]
    return ", ".join(names) if names else "none detected"
