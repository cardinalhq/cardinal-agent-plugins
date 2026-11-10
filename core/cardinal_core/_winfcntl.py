"""`fcntl.flock` for Windows, on `msvcrt.locking`.

Imported as `fcntl` where the real module does not exist, so the lock code
keeps the calls it makes on POSIX. The lock is one byte at offset 0 of the
lock file. A Windows region lock is always exclusive, so LOCK_SH behaves as
LOCK_EX; like flock, it conflicts between two opens of the file even in one
process, and the system drops it when the holder exits.
"""

from __future__ import annotations

import os
import time

LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8


def flock(fd: int, operation: int) -> None:
    import msvcrt  # Windows only; imported here so this module imports anywhere

    os.lseek(fd, 0, os.SEEK_SET)
    if operation & LOCK_UN:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    elif operation & LOCK_NB:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # OSError when held, as flock raises
    else:
        while True:  # LK_LOCK gives up after ten seconds; flock waits
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)
                os.lseek(fd, 0, os.SEEK_SET)
