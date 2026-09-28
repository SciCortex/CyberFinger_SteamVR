# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Canvas visualisation shared by both bridges.

HandPanel draws one controller's live state (sticks, buttons, trigger, IMU
orientation triads); SkeletonPanel draws the runtime-tracked hand skeleton plus
a head-relative position dome. Both are plain tkinter Canvas widgets with no
platform dependencies.
"""

import math
import time
import tkinter as tk
from tkinter import ttk

from .graphics import (COLOR_ACCENT, COLOR_ACCENT2, COLOR_BG, COLOR_BG2,
                       COLOR_BG3, COLOR_BLUE, COLOR_FG, COLOR_FG_DIM,
                       COLOR_GREEN, COLOR_ORANGE, COLOR_RED, FONT, VIEW_PITCH,
                       blend, project, quat_to_euler_deg, quat_to_matrix,
                       rotate_vec)
from .protocol import BUTTON_ROWS
from .skeleton import SKELETON_CHAINS, SKELETON_TIPS


class HandPanel:
    """Canvas-based hand state visualization."""

    def __init__(self, parent, label, side):
        self.label = label
        self.frame = ttk.Frame(parent)
        self.frame.pack(side=side, fill=tk.BOTH, expand=True,
                        padx=(0, 4) if side == tk.LEFT else (4, 0))

        self.canvas = tk.Canvas(self.frame, bg=COLOR_BG2, highlightthickness=0, width=1,
                                height=350)
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def update_state(self, state):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 10 or h < 10:
            return

        is_left = self.label == "LEFT"

        # Title
        if state.connected:
            c.create_text(w // 2, 14, text=f"{self.label}", fill=COLOR_ACCENT,
                          font=(FONT, 11, "bold"))
        else:
            c.create_text(w // 2, 14, text=f"{self.label} (disconnected)",
                          fill=COLOR_FG_DIM, font=(FONT, 10))
            return

        # Battery
        bat = state.battery
        bat_color = COLOR_GREEN if bat > 50 else COLOR_ORANGE if bat > 20 else COLOR_RED
        c.create_text(w - 10, 14, text=f"{bat}%", fill=bat_color,
                      font=(FONT, 9), anchor=tk.E)

        # Packet counter
        c.create_text(10, 14, text=f"#{state.packet_count}", fill=COLOR_FG_DIM,
                      font=(FONT, 8), anchor=tk.W)

        # ── Joystick visualization ──
        joy_cx = w // 4 if is_left else 3 * w // 4
        joy_cy = 80
        joy_r = 35

        c.create_oval(joy_cx - joy_r, joy_cy - joy_r,
                      joy_cx + joy_r, joy_cy + joy_r,
                      outline=COLOR_BG3, width=2, fill=COLOR_BG)
        c.create_line(joy_cx - joy_r, joy_cy, joy_cx + joy_r, joy_cy,
                      fill=COLOR_BG3, width=1)
        c.create_line(joy_cx, joy_cy - joy_r, joy_cx, joy_cy + joy_r,
                      fill=COLOR_BG3, width=1)

        jx = state.joy_x_float * (joy_r - 6)
        jy = state.joy_y_float * (joy_r - 6)
        dot_r = 6
        c.create_oval(joy_cx + jx - dot_r, joy_cy + jy - dot_r,
                      joy_cx + jx + dot_r, joy_cy + jy + dot_r,
                      fill=COLOR_ACCENT, outline=COLOR_ACCENT2, width=1)

        # ── Button indicators ──
        btn_x = 3 * w // 4 if is_left else w // 4
        btn_y_start = 30
        btn_spacing = 17

        for i, (name, bit) in enumerate(BUTTON_ROWS):
            by = btn_y_start + i * btn_spacing
            pressed = bool(state.buttons & bit)
            fill = COLOR_ACCENT if pressed else COLOR_BG
            outline = COLOR_ACCENT if pressed else COLOR_BG3
            c.create_oval(btn_x - 7, by - 7, btn_x + 7, by + 7,
                          fill=fill, outline=outline, width=2)
            c.create_text(btn_x + 14, by, text=name,
                          fill=COLOR_FG if pressed else COLOR_FG_DIM,
                          font=(FONT, 8), anchor=tk.W)

        # ── Trigger bar ──
        trig_x = w // 2
        trig_y = 168
        trig_w = w - 40
        trig_h = 10
        trig_val = state.trigger_float

        c.create_rectangle(trig_x - trig_w // 2, trig_y,
                           trig_x + trig_w // 2, trig_y + trig_h,
                           fill=COLOR_BG, outline=COLOR_BG3)
        if trig_val > 0.01:
            fill_w = int(trig_val * trig_w)
            c.create_rectangle(trig_x - trig_w // 2, trig_y,
                               trig_x - trig_w // 2 + fill_w, trig_y + trig_h,
                               fill=COLOR_ACCENT, outline="")
        c.create_text(trig_x, trig_y - 6, text=f"Trigger: {int(trig_val * 100)}%",
                      fill=COLOR_FG_DIM, font=(FONT, 8))

        # ── IMU orientation ──
        self._draw_imu(c, w, h, state)

    def _draw_imu(self, c, w, h, state):
        """Draw a 3D triad per populated IMU slot, or a placeholder if none."""
        c.create_line(20, 192, w - 20, 192, fill=COLOR_BG3, width=1)

        imus = state.active_imus()
        if not imus:
            c.create_text(w // 2, 258, text="no IMU installed",
                          fill=COLOR_FG_DIM, font=(FONT, 9))
            return

        # Share the panel width between however many slots are live, shrinking
        # the triads rather than letting them collide.
        col_w = w / len(imus)
        scale = max(18.0, min(46.0, col_w * 0.30))
        cy = 262

        for i, (label, quat) in enumerate(imus):
            cx = col_w * (i + 0.5)
            c.create_text(cx, 206, text=label, fill=COLOR_FG_DIM,
                          font=(FONT, 8, "bold"))
            self._draw_triad(c, cx, cy, scale, quat)

            roll, pitch, yaw = quat_to_euler_deg(quat)
            if len(imus) == 1:
                readout = f"R{roll:+6.1f}  P{pitch:+6.1f}  Y{yaw:+6.1f}"
            else:
                readout = f"{roll:+.0f} {pitch:+.0f} {yaw:+.0f}"
            c.create_text(cx, 322, text=readout,
                          fill=COLOR_FG_DIM, font=(FONT, 8))

    @staticmethod
    def _draw_triad(c, cx, cy, scale, quat):
        """Render one orientation as an XYZ axis triad against a horizon ring."""
        m = quat_to_matrix(quat)

        # Reference ground ring so rotation reads against a fixed horizon.
        ring = []
        for i in range(32):
            a = 2.0 * math.pi * i / 32
            px, py, _ = project((math.cos(a), 0.0, math.sin(a)), cx, cy, scale)
            ring.extend((px, py))
        c.create_polygon(ring, outline=COLOR_BG3, fill="", width=1)

        axes = [
            ((1.0, 0.0, 0.0), COLOR_RED,   "X"),
            ((0.0, 1.0, 0.0), COLOR_GREEN, "Y"),
            ((0.0, 0.0, 1.0), COLOR_BLUE,  "Z"),
        ]

        # Paint far-to-near so nearer arms overlap correctly.
        drawn = []
        for vec, color, name in axes:
            px, py, depth = project(rotate_vec(m, vec), cx, cy, scale)
            drawn.append((depth, px, py, color, name))
        drawn.sort(key=lambda t: t[0])

        ox, oy, _ = project((0.0, 0.0, 0.0), cx, cy, scale)
        for depth, px, py, color, name in drawn:
            # Nearer arms draw thicker — a cheap depth cue without shading.
            width = 3 if depth >= 0 else 2
            c.create_line(ox, oy, px, py, fill=color, width=width)
            c.create_oval(px - 3, py - 3, px + 3, py + 3, fill=color, outline="")
            c.create_text(px + 9, py - 7, text=name, fill=color,
                          font=(FONT, 8, "bold"))

    def set_disconnected(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w > 10:
            c.create_text(w // 2, h // 2, text=f"{self.label}\n(disconnected)",
                          fill=COLOR_FG_DIM, font=(FONT, 10), justify=tk.CENTER)


class SkeletonPanel:
    """Canvas rendering of the runtime-tracked hand skeleton for one hand."""

    def __init__(self, parent, label, side):
        self.label = label
        self.frame = ttk.Frame(parent)
        self.frame.pack(side=side, fill=tk.BOTH, expand=True,
                        padx=(0, 4) if side == tk.LEFT else (4, 0))
        self.canvas = tk.Canvas(self.frame, bg=COLOR_BG2, highlightthickness=0, width=1,
                                height=150)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        # Projection axes, chosen from the first tracked frame and then kept
        # fixed — re-deriving per frame makes the view twitch between axes
        # whenever the hand tilts past 45°.
        self._axes = None
        # Dome trail: recent (time, head-local unit direction) samples for this
        # panel's own hand, redrawn with fading color each frame.
        self._trail = []

    def _pick_axes(self, joints):
        """Choose the two model-space axes to project onto: the widest-spread
        axis is drawn vertically (finger direction), the runner-up across.
        Sign puts fingertips at the top of the canvas."""
        pts = joints[1:26]  # skip root and aux bones
        ext = []
        for a in range(3):
            vals = [p[a] for p in pts]
            ext.append((max(vals) - min(vals), a))
        ext.sort(reverse=True)
        v_axis, h_axis = ext[0][1], ext[1][1]
        tips = [joints[t][v_axis] for t in SKELETON_TIPS]
        v_sign = -1.0 if (sum(tips) / len(tips)) >= joints[1][v_axis] else 1.0
        self._axes = (h_axis, v_axis, v_sign)

    def draw(self, joints, status, pose=None, other_pose=None):
        """pose/other_pose: (rot_3x3, head_local_pos, dist, vel) from pose_info."""
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 10 or h < 10:
            return

        if not joints or len(joints) < 26:
            self._axes = None
            c.create_text(w // 2, h // 2,
                          text=f"{self.label} skeleton\n({status})",
                          fill=COLOR_FG_DIM, font=(FONT, 9),
                          justify=tk.CENTER)
        else:
            c.create_text(w // 2, 12, text=f"{self.label} SKELETON",
                          fill=COLOR_BLUE, font=(FONT, 8, "bold"))
            if pose is not None:
                self._draw_bones(c, w, h,
                                 self._orient_to_px(joints, pose[0], w, h,
                                                    dist=pose[2]))
            else:
                self._draw_bones(c, w, h, self._autofit_to_px(joints, w, h))

        if pose is not None:
            self._draw_dome(c, w, h, pose, other_pose)

    # ── skeleton projections ──

    @staticmethod
    def _orient_to_px(joints, rot, w, h, dist=None):
        """World-oriented view: wrist-relative joints rotated by the hand
        device's world rotation, then through a fixed camera. Base scale is
        physical (no zooming as the hand turns); on top of that, distance from
        the head scales the whole render like the dome dot — closer hand draws
        bigger."""
        scale = min(w - 24, h - 36) / 0.28  # px per metre, hand span ~0.25 m
        if dist is not None:
            scale *= max(0.6, min(1.5, 0.55 / max(dist, 0.2)))
        wx, wy, wz = joints[1]
        # Straight-on camera, unlike the IMU triads' three-quarter view: its
        # 35° yaw reads as "the hand is rotated wrong", not as perspective.
        # A touch of pitch keeps some depth without skewing the heading.
        cy_, sy_ = 1.0, 0.0
        cp_, sp_ = math.cos(VIEW_PITCH), math.sin(VIEW_PITCH)

        def to_px(j):
            lx, ly, lz = joints[j]
            lx, ly, lz = lx - wx, ly - wy, lz - wz
            x = rot[0][0] * lx + rot[0][1] * ly + rot[0][2] * lz
            y = rot[1][0] * lx + rot[1][1] * ly + rot[1][2] * lz
            z = rot[2][0] * lx + rot[2][1] * ly + rot[2][2] * lz
            # 180° about vertical: without it the render is left/right mirrored
            # relative to the user's own view of their hand.
            x, z = -x, -z
            x1 = x * cy_ + z * sy_
            z1 = -x * sy_ + z * cy_
            y1 = y * cp_ - z1 * sp_
            return w / 2 + x1 * scale, h / 2 + 6 - y1 * scale

        return to_px

    def _autofit_to_px(self, joints, w, h):
        """Fallback when no device pose is available: original auto-fit."""
        if self._axes is None:
            self._pick_axes(joints)
        h_axis, v_axis, v_sign = self._axes

        pts = joints[1:26]
        hs = [p[h_axis] for p in pts]
        vs = [p[v_axis] * v_sign for p in pts]
        cx_m = (max(hs) + min(hs)) / 2.0
        cy_m = (max(vs) + min(vs)) / 2.0
        span_h = max(max(hs) - min(hs), 0.05)
        span_v = max(max(vs) - min(vs), 0.05)
        scale = min((w - 24) / span_h, (h - 32) / span_v)

        def to_px(j):
            p = joints[j]
            return (w / 2 + (p[h_axis] - cx_m) * scale,
                    h / 2 + 4 + (p[v_axis] * v_sign - cy_m) * scale)

        return to_px

    @staticmethod
    def _draw_bones(c, w, h, to_px):
        for chain in SKELETON_CHAINS:
            px = [to_px(j) for j in chain]
            for (x0, y0), (x1, y1) in zip(px, px[1:]):
                c.create_line(x0, y0, x1, y1, fill=COLOR_FG_DIM, width=2)

        for chain in SKELETON_CHAINS:
            for j in chain[1:]:
                x, y = to_px(j)
                if j in SKELETON_TIPS:
                    c.create_oval(x - 3, y - 3, x + 3, y + 3,
                                  fill=COLOR_ACCENT, outline="")
                else:
                    c.create_oval(x - 2, y - 2, x + 2, y + 2,
                                  fill=COLOR_BLUE, outline="")

        wx, wy = to_px(1)
        c.create_rectangle(wx - 3, wy - 3, wx + 3, wy + 3,
                           fill=COLOR_GREEN, outline="")

    # ── position inset: 180° dome as seen from the head ──

    def _draw_dome(self, c, w, h, pose, other_pose):
        """Azimuthal-equidistant projection of the front hemisphere: centre is
        straight ahead of the gaze, rings at 30°/60°/90° off-forward, the rim
        is beside/behind the head. Dot size encodes distance."""
        r = max(24, min(44, h // 3))
        margin = 8
        cx = (margin + r) if self.label == "LEFT" else (w - margin - r)
        cy = h - margin - r

        for k in (1 / 3, 2 / 3, 1.0):
            rr = r * k
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr,
                          outline=COLOR_BG3, width=1)
        for deg in range(0, 360, 45):
            a = math.radians(deg)
            c.create_line(cx + (r / 3) * math.cos(a), cy + (r / 3) * math.sin(a),
                          cx + r * math.cos(a), cy + r * math.sin(a),
                          fill=COLOR_BG3, width=1)
        c.create_line(cx - 3, cy, cx + 3, cy, fill=COLOR_FG_DIM)
        c.create_line(cx, cy - 3, cx, cy + 3, fill=COLOR_FG_DIM)

        # Fading trail of the own hand's last second of motion.
        now = time.time()
        self._trail.append((now, pose[1]))
        while self._trail and self._trail[0][0] < now - 1.0:
            self._trail.pop(0)
        pts = [self._dome_project(cx, cy, r, p)[:2] for _, p in self._trail]
        for i in range(1, len(pts)):
            age = (now - self._trail[i][0])  # 0 = fresh, 1 = oldest
            c.create_line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1],
                          fill=blend(COLOR_ACCENT, COLOR_BG2, age), width=1)

        if other_pose is not None:
            self._dome_dot(c, cx, cy, r, other_pose, COLOR_FG_DIM)
        self._dome_dot(c, cx, cy, r, pose, COLOR_ACCENT)

        c.create_text(cx, cy - r - 7, text=f"{pose[2]:.2f}m",
                      fill=COLOR_FG_DIM, font=(FONT, 7))

    @staticmethod
    def _dome_project(cx, cy, r, local):
        """Head-local point → dome pixel position (+ whether behind 90°)."""
        x, y, z = local
        n = math.sqrt(x * x + y * y + z * z)
        if n < 1e-6:
            return cx, cy, False
        ux, uy, uz = x / n, y / n, z / n
        # Head frame: +x right, +y up, -z forward. Angle off forward-gaze:
        theta = math.acos(max(-1.0, min(1.0, -uz)))
        rr = min(theta / (math.pi / 2), 1.0) * r
        phi = math.atan2(uy, ux)
        return (cx + rr * math.cos(phi), cy - rr * math.sin(phi),
                theta > math.pi / 2)

    def _dome_dot(self, c, cx, cy, r, pose, color):
        _, local, dist, vel = pose
        px, py, behind = self._dome_project(cx, cy, r, local)

        # Velocity whisker: where the hand will be in 0.15 s, projected the
        # same way, so the whisker curves with the dome rather than lying.
        speed = math.sqrt(sum(v * v for v in vel))
        if speed > 0.05:
            ahead = tuple(local[i] + vel[i] * 0.15 for i in range(3))
            qx, qy, _ = self._dome_project(cx, cy, r, ahead)
            c.create_line(px, py, qx, qy, fill=color, width=1)

        size = max(2.5, 7.0 - dist * 6.0)  # closer → bigger
        if behind:
            c.create_oval(px - size, py - size, px + size, py + size,
                          outline=color, width=1)
        else:
            c.create_oval(px - size, py - size, px + size, py + size,
                          fill=color, outline="")
