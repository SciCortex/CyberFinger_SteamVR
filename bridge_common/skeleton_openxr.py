# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Runtime hand skeleton via native OpenXR — for Monado / WiVRn stacks.

The OpenVR backend in skeleton.py cannot work on an OpenVR-on-OpenXR layer
(xrizer, OpenComposite) in front of Monado/WiVRn, and not for a fixable reason:

  * those layers implement only the Background and Scene application types and
    reject Overlay outright; and, decisively,
  * OpenXR delivers action input only to a session in XR_SESSION_STATE_FOCUSED,
    and a session only reaches FOCUSED by submitting frames. A bridge that
    renders nothing therefore never gets focus, so its skeletal actions stay
    inactive — verified to hold even with a scene app running. SteamVR's
    vrserver serves every client at once; Monado focuses exactly one.

This backend sidesteps both problems by talking to the OpenXR runtime directly:

  * XR_MND_headless creates a session with no graphics binding, which Monado
    takes all the way to FOCUSED without a single frame submitted; and
  * XR_EXT_hand_tracking's xrLocateHandJointsEXT is NOT action-based, so it
    needs no action manifest, no binding files, and does not contend with the
    running game for focus.

The 26 joints XR_EXT_hand_tracking reports (PALM, WRIST, then five finger
chains, tips at 5/10/15/20/25) are exactly the layout SKELETON_CHAINS already
draws, so no remapping is needed.

Duck-typed identically to OpenVRSkeletonSource: .start(), .stop(), .status,
.hands, .pose_info, .raw_pose, .driver_feed and .feed_enabled, so the GUIs and
panels cannot tell the two apart.
"""

import ctypes
import threading
import time

from .graphics import matrix_to_quat, quat_to_matrix, relative_pose, unrotate_vec

# pyopenxr. Broad except: the import can also fail on a missing OpenXR loader
# (libopenxr_loader.so), not just an absent package.
try:
    import xr
    HAS_PYOPENXR = True
except Exception:
    HAS_PYOPENXR = False


def _required_extensions():
    return (
        xr.MND_HEADLESS_EXTENSION_NAME,             # session with no graphics binding
        xr.EXT_HAND_TRACKING_EXTENSION_NAME,        # xrLocateHandJointsEXT
        xr.KHR_CONVERT_TIMESPEC_TIME_EXTENSION_NAME,  # XrTime without a frame loop
    )


def openxr_hand_tracking_available():
    """True if the ACTIVE OpenXR runtime can serve a headless hand-tracking session.

    Enumerating instance extensions needs no instance and no session, so this is
    cheap and side-effect free — safe to call at startup to pick a backend. It
    is also self-selecting across platforms: XR_MND_headless is a Monado
    extension, so SteamVR (Windows or Linux) simply won't advertise it and the
    OpenVR backend keeps the job.
    """
    if not HAS_PYOPENXR:
        return False
    try:
        have = {e.extension_name.decode()
                for e in xr.enumerate_instance_extension_properties()}
    except Exception:
        return False
    return all(name in have for name in _required_extensions())


class OpenXRSkeletonSource:
    """Polls the OpenXR runtime for hand joints in a background thread."""

    RETRY_S = 5.0

    def __init__(self, log=None):
        self._log = log or (lambda msg: None)
        self.hands = [None, None]        # 0 = left, 1 = right
        self.pose_info = [None, None]
        self.raw_pose = [None, None]
        self.driver_feed = [None, None]
        self.feed_enabled = False
        self.status = "starting..."
        self.app_type = "OpenXR headless"
        self._running = False
        self._thread = None
        self._ready = False
        self._logged_waiting = False
        self._session_state = None
        self._session_running = False
        # Previous (time, position) per hand and for the head, for the finite
        # differences that stand in for velocity — the dome inset's whisker.
        self._prev = {}
        self._ever_active = False
        self._hand_active = [None, None]
        self._reset_handles()

    def _reset_handles(self):
        self._instance = None
        self._session = None
        self._space = None
        self._view_space = None
        self._trackers = {}
        self._locations = {}
        self._buffers = {}
        self._locate_fn = None

    # ── lifecycle ──

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._teardown()

    def _teardown(self):
        self._ready = False
        self._session_running = False
        self._session_state = None
        self.hands = [None, None]
        self.pose_info = [None, None]
        self.raw_pose = [None, None]
        self.driver_feed = [None, None]
        self._prev = {}
        self._hand_active = [None, None]
        for destroy, handle in ((getattr(xr, "destroy_session", None), self._session),
                                (getattr(xr, "destroy_instance", None), self._instance)):
            if destroy and handle is not None:
                try:
                    destroy(handle)
                except Exception:
                    pass
        self._reset_handles()

    def _connect(self):
        self._instance = xr.create_instance(xr.InstanceCreateInfo(
            enabled_extension_names=list(_required_extensions())))
        system = xr.get_system(self._instance, xr.SystemGetInfo(
            form_factor=xr.FormFactor.HEAD_MOUNTED_DISPLAY))
        # next=None is the headless session: no graphics binding at all. Only
        # legal because XR_MND_headless is enabled above.
        self._session = xr.create_session(self._instance, xr.SessionCreateInfo(
            system_id=system, next=None))
        self._space = xr.create_reference_space(self._session,
            xr.ReferenceSpaceCreateInfo(
                reference_space_type=xr.ReferenceSpaceType.LOCAL))
        self._view_space = xr.create_reference_space(self._session,
            xr.ReferenceSpaceCreateInfo(
                reference_space_type=xr.ReferenceSpaceType.VIEW))

        n = xr.HAND_JOINT_COUNT_EXT
        for hand, which in ((0, xr.HandEXT.LEFT), (1, xr.HandEXT.RIGHT)):
            self._trackers[hand] = xr.create_hand_tracker_ext(
                self._session, xr.HandTrackerCreateInfoEXT(
                    hand=which, hand_joint_set=xr.HandJointSetEXT.DEFAULT))
            # The buffer must outlive the call and be referenced from here:
            # HandJointLocationsEXT stores only a pointer to it.
            self._buffers[hand] = (xr.HandJointLocationEXT * n)()
            self._locations[hand] = xr.HandJointLocationsEXT(
                joint_locations=self._buffers[hand])

        # pyopenxr's locate_hand_joints_ext wrapper allocates nothing — it
        # passes jointLocations == NULL and the runtime rejects the call. Same
        # class of wrapper bug as pyopenvr's getSkeletalBoneData, so drive the
        # function pointer directly with our own buffers, as there.
        self._locate_fn = ctypes.cast(
            xr.get_instance_proc_addr(self._instance, "xrLocateHandJointsEXT"),
            xr.PFN_xrLocateHandJointsEXT)

        self._ready = True
        self._logged_waiting = False
        self._ever_active = False
        self.status = "connecting..."
        self._log("Skeleton: OpenXR headless session created "
                  "(XR_EXT_hand_tracking)")

    # ── time ──

    def _now(self):
        """Current time as XrTime, without a frame loop to predict against.

        A rendering app would use xrWaitFrame's predicted display time; a
        headless one has no frames, so convert the monotonic clock instead.
        """
        t = time.clock_gettime(time.CLOCK_MONOTONIC)
        sec = int(t)
        return xr.convert_timespec_time_to_time_khr(
            self._instance,
            xr.timespec(tv_sec=sec, tv_nsec=int((t - sec) * 1e9)))

    # ── main loop ──

    def _loop(self):
        while self._running:
            if not self._ready:
                try:
                    self._connect()
                except Exception as e:
                    self._teardown()
                    self.status = "VR runtime not running"
                    if not self._logged_waiting:
                        self._logged_waiting = True
                        self._log(f"Skeleton: OpenXR unavailable "
                                  f"({type(e).__name__}), will retry")
                    deadline = time.time() + self.RETRY_S
                    while self._running and time.time() < deadline:
                        time.sleep(0.2)
                    continue
            try:
                self._poll()
            except Exception as e:
                self._teardown()
                self.status = "VR runtime lost, retrying"
                self._log(f"Skeleton: OpenXR session lost ({type(e).__name__})")
                continue
            time.sleep(1.0 / 30.0)

    def _drain_events(self):
        while True:
            try:
                ev = xr.poll_event(self._instance)
            except xr.EventUnavailable:
                return
            if ev.type == xr.StructureType.EVENT_DATA_SESSION_STATE_CHANGED:
                changed = ctypes.cast(
                    ctypes.byref(ev),
                    ctypes.POINTER(xr.EventDataSessionStateChanged)).contents
                state = xr.SessionState(changed.state)
                self._session_state = state
                if state == xr.SessionState.READY and not self._session_running:
                    xr.begin_session(self._session, xr.SessionBeginInfo(
                        primary_view_configuration_type=(
                            xr.ViewConfigurationType.PRIMARY_STEREO)))
                    self._session_running = True
                    self._log("Skeleton: OpenXR session begun")
                elif state == xr.SessionState.STOPPING:
                    self._session_running = False
                    xr.end_session(self._session)
                elif state in (xr.SessionState.EXITING,
                               xr.SessionState.LOSS_PENDING):
                    raise RuntimeError(f"session {state.name}")
            elif ev.type == xr.StructureType.EVENT_DATA_INSTANCE_LOSS_PENDING:
                raise RuntimeError("instance loss pending")

    def _poll(self):
        self._drain_events()
        if not self._session_running:
            self.status = "waiting for session"
            self.hands = [None, None]
            return

        now = self._now()
        wall = time.time()

        # Head pose, for the head-relative dome inset: locate VIEW in LOCAL.
        head = self._locate(self._view_space, now, "head", wall)

        for hand in (0, 1):
            joints, pose, raw = self._read_hand(hand, now, wall, head)
            self.hands[hand] = joints
            self.pose_info[hand] = pose
            self.raw_pose[hand] = raw
            # The driver stream stays the OpenVR backend's job — see the note
            # in create_skeleton_source().
            self.driver_feed[hand] = None

        if any(self._hand_active):
            self.status = "connected"
        elif self._ever_active:
            self.status = "hands not in view"
        else:
            self.status = "no hands tracked yet"

    def _locate(self, space, now, key, wall):
        """Locate a space in LOCAL → (position, rotation rows, velocity)."""
        loc = xr.locate_space(space=space, base_space=self._space, time=now)
        need = (xr.SpaceLocationFlags.POSITION_VALID_BIT
                | xr.SpaceLocationFlags.ORIENTATION_VALID_BIT)
        if (loc.location_flags & need) != need:
            self._prev.pop(key, None)
            return None
        p = loc.pose.position
        o = loc.pose.orientation
        pos = (p.x, p.y, p.z)
        rot = quat_to_matrix((o.w, o.x, o.y, o.z))
        return pos, rot, self._velocity(key, pos, wall)

    def _velocity(self, key, pos, wall):
        """Finite-difference velocity. XR_EXT_hand_tracking can report true
        joint velocities via a chained struct, but a difference over the 30 Hz
        tick is plenty for the dome's 0.15 s lookahead whisker."""
        prev = self._prev.get(key)
        self._prev[key] = (wall, pos)
        if prev is None:
            return (0.0, 0.0, 0.0)
        dt = wall - prev[0]
        if dt <= 1e-4:
            return (0.0, 0.0, 0.0)
        return tuple((pos[i] - prev[1][i]) / dt for i in range(3))

    def _read_hand(self, hand, now, wall, head):
        """→ (joints in wrist-local space, pose_info entry, raw world pose)."""
        locations = self._locations[hand]
        self._locate_fn(self._trackers[hand],
                        xr.HandJointsLocateInfoEXT(base_space=self._space,
                                                   time=now),
                        ctypes.byref(locations))
        hn = "L" if hand == 0 else "R"
        active = bool(locations.is_active)
        if active != self._hand_active[hand]:
            # As in the OpenVR backend, don't narrate the initial
            # unknown→False edge: hands simply not in view yet is the norm.
            if active or self._hand_active[hand] is not None:
                self._log(f"Skeleton: {hn} hand "
                          + ("tracking" if active else "lost"))
            self._hand_active[hand] = active
        if not active:
            self._prev.pop(hn, None)
            return None, None, None
        self._ever_active = True

        buf = self._buffers[hand]
        wrist = buf[xr.HandJointEXT.WRIST]
        wp, wo = wrist.pose.position, wrist.pose.orientation
        wrist_pos = (wp.x, wp.y, wp.z)
        wrist_quat = (wo.w, wo.x, wo.y, wo.z)
        wrist_rot = quat_to_matrix(wrist_quat)

        # The panels expect joints in a hand-local frame (OpenVR model space):
        # _orient_to_px takes them wrist-relative and applies the hand's world
        # rotation itself. OpenXR reports world-space joints, so rotate them
        # into the wrist frame here — otherwise the world rotation is applied
        # twice and the render tumbles.
        joints = tuple(
            unrotate_vec(wrist_rot,
                         (buf[j].pose.position.x - wrist_pos[0],
                          buf[j].pose.position.y - wrist_pos[1],
                          buf[j].pose.position.z - wrist_pos[2]))
            for j in range(locations.joint_count))

        dev = (wrist_pos, wrist_rot, self._velocity(hn, wrist_pos, wall))
        pose = relative_pose(head, dev) if head is not None else None
        return joints, pose, (wrist_pos, wrist_rot)
