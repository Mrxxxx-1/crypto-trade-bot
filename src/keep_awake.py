"""Hold off Windows idle sleep while the bot runs (``KEEP_AWAKE=true``).

Uses ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``, which
asks Windows not to idle-sleep for as long as the calling thread keeps the flag
set. It changes no power settings: the hold disappears when ``release`` runs or
the process exits, however it exits. The display may still turn off, and a
closed lid or an explicit Sleep still sleeps.

On other platforms this is a no-op that says so; use ``caffeinate`` (macOS) or
``systemd-inhibit`` (Linux) to wrap the process instead.
"""

from __future__ import annotations

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def hold() -> bool:
    """Ask the OS not to idle-sleep. Call from the thread that runs the bot."""
    if sys.platform != "win32":
        print("KEEP_AWAKE: only supported on Windows; wrap the process with caffeinate/systemd-inhibit.")
        return False
    import ctypes

    ok = bool(ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    print("KEEP_AWAKE: idle sleep blocked while the bot runs." if ok else "KEEP_AWAKE: Windows refused the request.")
    return ok


def release() -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
