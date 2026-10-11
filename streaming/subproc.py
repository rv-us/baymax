"""Start the gst-launch decoders so they can't outlive the program that started them."""

import ctypes
import subprocess
import sys

PR_SET_PDEATHSIG = 1
SIGTERM = 15


def _die_with_parent():
    """Runs in the child before exec (Linux): ask the kernel to send SIGTERM when the
    parent exits. Without it, a receiver killed with SIGTERM or SIGKILL leaves its
    decoder holding the UDP port, and the next receiver silently gets nothing."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, SIGTERM)


def popen(command: list, **kwargs) -> subprocess.Popen:
    if sys.platform.startswith("linux"):
        kwargs["preexec_fn"] = _die_with_parent
    return subprocess.Popen(command, **kwargs)
