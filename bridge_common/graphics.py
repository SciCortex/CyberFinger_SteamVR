# SPDX-FileCopyrightText: 2026 DrSciCortex

#
# SPDX-License-Identifier: GPL-3.0-only

"""Brand palette and the minimal 3D maths the panels draw with.

Orthographic projection only, no external dependencies — enough to render an
orientation triad and a hand skeleton on a tkinter Canvas.
"""

import math

from .platform import MONO_FONT

# ── Brand colors ─────────────────────────────────────────────────────────

COLOR_BG       = "#1a1a1a"
COLOR_BG2      = "#242424"
COLOR_BG3      = "#2e2e2e"
COLOR_FG       = "#e0e0e0"
COLOR_FG_DIM   = "#888888"
COLOR_ACCENT   = "#e6007e"  # CyberFinger pink
COLOR_ACCENT2  = "#ff2d9b"
COLOR_GREEN    = "#00e676"
COLOR_RED      = "#ff1744"
COLOR_ORANGE   = "#ff9100"
COLOR_BLUE     = "#448aff"

# Re-exported so panels and both GUIs name one font.
FONT = MONO_FONT

# Fixed camera angles — a three-quarter view so all three axes stay distinct.
VIEW_YAW   = math.radians(35.0)
VIEW_PITCH = math.radians(20.0)


def blend(c1, c2, t):
    """Blend two #rrggbb colors; t=0 → c1, t=1 → c2."""
    t = max(0.0, min(1.0, t))
    a, b = int(c1[1:], 16), int(c2[1:], 16)
    parts = []
    for shift in (16, 8, 0):
        va = (a >> shift) & 255
        vb = (b >> shift) & 255
        parts.append(int(va + (vb - va) * t))
    return "#%02x%02x%02x" % tuple(parts)


def matrix_to_quat(m):
    """3x3 rotation matrix (row tuples) → unit quaternion (w, x, y, z)."""
    t = m[0][0] + m[1][1] + m[2][2]
    if t > 0:
        s = math.sqrt(t + 1.0) * 2.0
        return (0.25 * s,
                (m[2][1] - m[1][2]) / s,
                (m[0][2] - m[2][0]) / s,
                (m[1][0] - m[0][1]) / s)
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        return ((m[2][1] - m[1][2]) / s, 0.25 * s,
                (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s)
    if m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        return ((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s,
                0.25 * s, (m[1][2] + m[2][1]) / s)
    s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
    return ((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s,
            (m[1][2] + m[2][1]) / s, 0.25 * s)


def quat_to_matrix(q):
    """Unit quaternion (w, x, y, z) → 3x3 rotation matrix as row tuples."""
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n < 1e-9:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    w, x, y, z = w / n, x / n, y / n, z / n
    return (
        (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z),       2.0 * (x * z + w * y)),
        (2.0 * (x * y + w * z),       1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x)),
        (2.0 * (x * z - w * y),       2.0 * (y * z + w * x),       1.0 - 2.0 * (x * x + y * y)),
    )


def quat_to_euler_deg(q):
    """Unit quaternion (w, x, y, z) → (roll, pitch, yaw) in degrees."""
    w, x, y, z = q

    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # Clamp guards against gimbal-lock inputs drifting just past ±1.
    sinp = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sinp)

    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


def rotate_vec(m, v):
    """Apply a 3x3 row-major matrix to a 3-vector."""
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def relative_pose(hmd, dev):
    """(device world rotation, head-local position, distance, head-local
    velocity relative to the head).

    Both arguments are (position, rotation rows, velocity) in one world frame.
    Head frame follows the OpenVR/OpenXR device convention: +x right, +y up,
    -z forward — what the dome inset projects. Shared by both skeleton
    backends so the panels get identical semantics whichever one is running.
    """
    (hpos, hrot, hvel), (dpos, drot, dvel) = hmd, dev
    rel = tuple(dpos[i] - hpos[i] for i in range(3))
    relv = tuple(dvel[i] - hvel[i] for i in range(3))
    # Rows of hrot are the head axes in world space, so head-local is R^T · v.
    local = tuple(sum(hrot[r][i] * rel[r] for r in range(3)) for i in range(3))
    local_v = tuple(sum(hrot[r][i] * relv[r] for r in range(3)) for i in range(3))
    dist = math.sqrt(sum(v * v for v in rel))
    return drot, local, dist, local_v


def unrotate_vec(m, v):
    """R^T · v — express a world vector in the frame whose rows are m."""
    return tuple(sum(m[r][i] * v[r] for r in range(3)) for i in range(3))


def project(v, cx, cy, scale):
    """World point → (screen_x, screen_y, depth). Larger depth is nearer."""
    x, y, z = v

    # Yaw about world Y, then pitch about the camera's X.
    cyaw, syaw = math.cos(VIEW_YAW), math.sin(VIEW_YAW)
    xe = x * cyaw - z * syaw
    ze = x * syaw + z * cyaw

    cp, sp = math.cos(VIEW_PITCH), math.sin(VIEW_PITCH)
    ye = y * cp - ze * sp
    depth = y * sp + ze * cp

    # Screen y is inverted so +Y points up on the canvas.
    return (cx + xe * scale, cy - ye * scale, depth)
