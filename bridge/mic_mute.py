# SPDX-FileCopyrightText: 2026 DrSciCortex
#
# SPDX-License-Identifier: GPL-3.0-only

"""Mute and unmute the Windows default microphone: what the right CyberFinger's pink button does by default
(VR mode, pink_button.py).

The mute is the capture endpoint's own, so it holds for every app at once (Resonite, VRChat, Discord: they record
from the default device, which SteamVR can switch to the headset's microphone). Both default capture devices are
set, the console one and the communications one, when they differ. Core Audio through ctypes, so no package is
needed.
"""

import ctypes
from ctypes import wintypes as wt

_CLSCTX_ALL = 23
_E_CAPTURE = 1
_ROLES = (0, 2)                  # eConsole, eCommunications


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


def _method(obj, index, *argtypes):
    """COM method `index` of the interface pointer `obj` (HRESULT-returning: failures raise OSError)."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])


def _release(obj):
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


def _with_endpoints(fn):
    """Call fn(volume) for each default capture device's IAudioEndpointVolume (each device once); returns the
    results in role order. [] without a microphone."""
    ole32 = ctypes.oledll.ole32
    try:
        ole32.CoInitializeEx(None, 0)            # COINIT_MULTITHREADED, this thread
    except OSError:
        pass                                     # already initialised in another mode: usable as it is
    enum = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(_guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")), None, _CLSCTX_ALL,
                           ctypes.byref(_guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")), ctypes.byref(enum))
    results, seen = [], set()
    try:
        for role in _ROLES:
            dev, vol, dev_id = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_wchar_p()
            try:
                _method(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                    enum, _E_CAPTURE, role, ctypes.byref(dev))              # GetDefaultAudioEndpoint
            except OSError:
                continue                                                    # no device for this role
            try:
                _method(dev, 5, ctypes.POINTER(ctypes.c_wchar_p))(dev, ctypes.byref(dev_id))   # GetId
                key = dev_id.value
                ole32.CoTaskMemFree(dev_id)
                if key in seen:
                    continue
                seen.add(key)
                _method(dev, 3, ctypes.POINTER(_GUID), wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                    dev, ctypes.byref(_guid("{5CDF2C82-841E-4546-9722-0CF74078229A}")), _CLSCTX_ALL, None,
                    ctypes.byref(vol))                                      # Activate(IAudioEndpointVolume)
                results.append(fn(vol))
            finally:
                _release(vol)
                _release(dev)
    finally:
        _release(enum)
    return results


def _get_mute(vol):
    muted = wt.BOOL()
    _method(vol, 15, ctypes.POINTER(wt.BOOL))(vol, ctypes.byref(muted))     # GetMute
    return bool(muted.value)


def is_muted():
    """The default microphone's mute, or None without one."""
    states = _with_endpoints(_get_mute)
    return states[0] if states else None


def set_muted(muted):
    """Mute (True) or unmute every default microphone. False without one."""
    return bool(_with_endpoints(
        lambda vol: _method(vol, 14, wt.BOOL, ctypes.c_void_p)(vol, 1 if muted else 0, None)))   # SetMute


def toggle():
    """Flip the default microphone's mute; returns the new state, or None without a microphone."""
    state = is_muted()
    if state is None:
        return None
    set_muted(not state)
    return not state
