VERSION = "2.1.0"

# The CyberFinger Bridge version: the one place to bump it. Shown in the bridge GUIs (Windows and Linux) and read by
# installer/setup.iss when the installer is built, which takes the text between the quotes on the first line, so keep
# the VERSION line first.

import os
import subprocess
import sys


def git_hash():
    """The git commit this bridge came from, e.g. "2ee2a58" ("-dirty" when built or run with uncommitted changes).
    A built .exe has no git or .git folder, so build.bat records the hash in build_info.py before PyInstaller runs;
    from source it is read from git directly. "unknown" if neither is available."""
    if getattr(sys, "frozen", False):
        try:
            from build_info import GIT_HASH
            return GIT_HASH
        except ImportError:
            return "unknown"
    try:
        result = subprocess.run(["git", "describe", "--always", "--dirty"],
                                cwd=os.path.dirname(os.path.abspath(__file__)),
                                capture_output=True, text=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"
