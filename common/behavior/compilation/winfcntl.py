"""`fcntl.flock` for Windows, on `msvcrt.locking`.

Imported as `fcntl` where the real module does not exist, so the lock sites
keep the calls they make on POSIX. The lock is one byte at offset 0 of the
lock file. A Windows region lock is always exclusive (LOCK_SH behaves as
LOCK_EX), conflicts between two opens of the file even in one process, and
is dropped by the system when the holder exits.

The byte being held is EACCES from msvcrt (EDEADLOCK when it gives up). A
non-blocking request then raises BlockingIOError, the class flock raises and
the callers catch; a blocking one waits like flock. Any other error is
raised at once, unchanged.
"""
import errno
import os
import time

LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
HELD = (errno.EACCES, errno.EDEADLK)


def flock(file, operation, sleep=time.sleep):
    import msvcrt  # Windows only; imported here so this module imports anywhere

    fd = file if isinstance(file, int) else file.fileno()
    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK if operation & LOCK_UN else msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if operation & LOCK_UN or error.errno not in HELD:
                raise
            if operation & LOCK_NB:  # what flock raises, and what the callers catch
                raise BlockingIOError(errno.EWOULDBLOCK, 'the lock is held') from error
            sleep(0.05)
