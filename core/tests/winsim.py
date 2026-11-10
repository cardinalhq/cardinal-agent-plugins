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


def apply() -> None:
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
