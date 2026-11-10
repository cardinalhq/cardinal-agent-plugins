"""Make a Python interpreter look like a Windows one, for tests on any OS.

`apply()` hides what a Windows CPython lacks: the POSIX-only modules cannot
be imported and the POSIX-only `os` / `signal` names are gone. `run(code)`
does that in a fresh interpreter and then runs `code`, so the modules under
test are imported the way Windows would import them.

This catches the ImportError / AttributeError class only. Paths, process
and file semantics stay the host's: the Wine CI job (windows-wine.yml) and a
real Windows machine cover those.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

MISSING_MODULES = ("fcntl", "grp", "pwd", "resource", "termios")
# os.fchmod exists on Windows from Python 3.13; hidden to model 3.9 to 3.12.
MISSING_OS = ("O_NOFOLLOW", "fchmod", "fchown", "getegid", "geteuid", "getgid", "getpgid", "getuid", "killpg",
              "setsid", "uname")
# signal.SIGKILL is missing on Windows too, but the host's subprocess module needs it.
MISSING_SIGNAL = ("ITIMER_REAL", "SIGALRM", "SIGHUP", "SIGUSR1", "alarm", "setitimer")


def _fake_msvcrt():
    """msvcrt.locking on the host's flock (which, like a Windows region
    lock, conflicts between two opens in one process), so lock code runs here."""
    import errno
    import fcntl
    import types

    module = types.ModuleType("msvcrt")
    module.LK_UNLCK, module.LK_LOCK, module.LK_NBLCK = 0, 1, 2

    def locking(fd: int, mode: int, nbytes: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN if mode == module.LK_UNLCK else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PermissionError(errno.EACCES, "Permission denied") from None  # as msvcrt reports a held region

    module.locking = locking
    return module


def apply() -> None:
    if os.name != "nt":
        sys.modules["msvcrt"] = _fake_msvcrt()
    for name in MISSING_MODULES:
        sys.modules[name] = None  # type: ignore[assignment]  # import raises ImportError
    for module, names in ((os, MISSING_OS), (signal, MISSING_SIGNAL)):
        for name in names:
            if hasattr(module, name):
                delattr(module, name)


def run(code: str, **kwargs) -> subprocess.CompletedProcess:
    """Run `code` in a new interpreter after apply(). Keyword arguments go
    to subprocess.run (cwd, env, input)."""
    prelude = f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent)!r}); import winsim; winsim.apply()\n"
    return subprocess.run([sys.executable, "-c", prelude + code], capture_output=True, text=True, timeout=60,
                          **kwargs)
