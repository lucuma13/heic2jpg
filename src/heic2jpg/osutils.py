"""Filesystem timestamp helpers shared by the converter."""

import ctypes
import ctypes.wintypes
import os
import sys
from pathlib import Path

# -------------------------------------------------------------------------
# Windows creation-time helper
# -------------------------------------------------------------------------


def _set_creation_time_windows(path: Path, ctime_ns: int) -> None:
    """
    Set a file's creation time on Windows via kernel32.SetFileTime.

    Uses ctypes so pywin32 is not required. Silently does nothing on non-Windows
    platforms — callers don't need to guard with sys.platform.

    Parameters:
    path     : file to update (must already exist) ctime_ns : desired creation
    time in nanoseconds since the Unix epoch

    Raises:
    OSError  : if OpenFile or SetFileTime fails (caller should catch and warn)
    """
    if sys.platform != "win32":
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Declare signatures explicitly. Without a restype, ctypes truncates
    # CreateFileW's HANDLE return to a signed 32-bit int on 64-bit Windows, so a
    # failed open (-1) would never compare equal to INVALID_HANDLE_VALUE.
    kernel32.CreateFileW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.LPVOID,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
    kernel32.SetFileTime.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(ctypes.wintypes.FILETIME),
        ctypes.POINTER(ctypes.wintypes.FILETIME),
        ctypes.POINTER(ctypes.wintypes.FILETIME),
    ]
    kernel32.SetFileTime.restype = ctypes.wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

    # Convert nanoseconds-since-Unix-epoch → 100-nanosecond intervals since
    # 1601-01-01 (Windows FILETIME). The offset between the two epochs is
    # 116444736000000000 x 100ns ticks.
    EPOCH_DIFF_100NS = 116_444_736_000_000_000
    filetime_ticks = (ctime_ns // 100) + EPOCH_DIFF_100NS

    # Pack into a FILETIME struct (two DWORDs: low, high).
    ft = ctypes.wintypes.FILETIME(
        filetime_ticks & 0xFFFFFFFF,  # dwLowDateTime
        (filetime_ticks >> 32) & 0xFFFFFFFF,  # dwHighDateTime
    )

    # Open with GENERIC_WRITE + FILE_FLAG_BACKUP_SEMANTICS. OPEN_EXISTING (3) |
    # FILE_FLAG_BACKUP_SEMANTICS (0x02000000) lets us open a file handle
    # suitable for SetFileTime without truncating it.
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

    handle = kernel32.CreateFileW(
        str(path),
        GENERIC_WRITE,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1).value
    if handle == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed on {path}")
    try:
        # SetFileTime(handle, lpCreationTime, lpLastAccessTime, lpLastWriteTime)
        # Passing NULL for the last two leaves atime/mtime untouched —
        # os.utime() already set those.
        ok = kernel32.SetFileTime(handle, ctypes.byref(ft), None, None)
        if not ok:
            raise OSError(ctypes.get_last_error(), f"SetFileTime failed on {path}")
    finally:
        kernel32.CloseHandle(handle)


# -------------------------------------------------------------------------
# Timestamp restoration
# -------------------------------------------------------------------------


def _restore_timestamps(out: Path, src_stat: os.stat_result) -> None:
    """
    Copy the source file's timestamps onto the converted output.

    os.utime takes (atime_ns, mtime_ns) in nanoseconds.

    Creation time (st_birthtime) is read-only on most Linux filesystems — no
    portable syscall exists to set it. As a workaround we write birthtime into
    mtime when available (macOS/APFS populates st_birthtime; on Linux it's
    absent and we fall back to mtime). The real mtime of a HEIC is almost always
    identical to birthtime anyway (the file is written once, never modified), so
    this faithfully represents "when the photo was taken" in the output's mtime
    field — the one timestamp every tool shows.

    Raises OSError on failure (caller catches and warns).
    """
    birthtime_ns = getattr(src_stat, "st_birthtime_ns", None)
    if birthtime_ns is None and hasattr(src_stat, "st_birthtime"):
        birthtime_ns = int(src_stat.st_birthtime * 1e9)
    mtime_ns = birthtime_ns if birthtime_ns is not None else src_stat.st_mtime_ns
    os.utime(out, ns=(src_stat.st_atime_ns, mtime_ns))

    # Windows exposes a writable creation-time field via SetFileTime.
    # On macOS/Linux this is a no-op.
    _set_creation_time_windows(out, mtime_ns)
