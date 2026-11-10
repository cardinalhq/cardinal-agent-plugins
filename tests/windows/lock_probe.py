"""Print what msvcrt.locking reports here: a free byte, a held one, free
again, an unlock of nothing, and a closed descriptor.

Informational, always exits 0. The Windows lock shims (cardinal_core/_winfcntl.py,
common/behavior/compilation/winfcntl.py) assume a held byte is EACCES on
Windows and errno 0 under Wine; the native and Wine jobs print the facts.
"""
import ctypes
import msvcrt
import os
import tempfile


def attempt(label, fd, mode):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, mode, 1)
        print(f"{label}: ok")
    except OSError as error:
        print(f"{label}: {type(error).__name__} errno={error.errno} winerror={getattr(error, 'winerror', None)} {error}")


print("wine:", hasattr(ctypes.windll.ntdll, "wine_get_version"))
with tempfile.TemporaryDirectory() as directory:
    path = os.path.join(directory, "lock")
    first = os.open(path, os.O_RDWR | os.O_CREAT)
    second = os.open(path, os.O_RDWR)
    attempt("free, first handle locks", first, msvcrt.LK_NBLCK)
    attempt("held, second handle locks", second, msvcrt.LK_NBLCK)
    attempt("first handle unlocks", first, msvcrt.LK_UNLCK)
    attempt("free again, second handle locks", second, msvcrt.LK_NBLCK)
    attempt("second handle unlocks", second, msvcrt.LK_UNLCK)
    attempt("unlock of a byte that is not locked", first, msvcrt.LK_UNLCK)
    os.close(first)
    os.close(second)
    try:
        msvcrt.locking(first, msvcrt.LK_NBLCK, 1)
        print("closed descriptor: ok")
    except OSError as error:
        print(f"closed descriptor: {type(error).__name__} errno={error.errno} winerror={getattr(error, 'winerror', None)} {error}")
