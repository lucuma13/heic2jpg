#!/usr/bin/env python3
# =============================================================================
# heic2jpg.py — Fast HEIC to JPEG conversions
# =============================================================================
# Copyright (c) 2026 Luis Gómez Gutiérrez. License: MIT.

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

VERSION = "2.1.0"


# =========================================================================
# Windows creation-time helper
# =========================================================================

def _set_creation_time_windows(path: Path, ctime_ns: int) -> None:
    """Set a file's creation time on Windows via kernel32.SetFileTime.

    Uses ctypes so pywin32 is not required. Silently does nothing on
    non-Windows platforms — callers don't need to guard with sys.platform.

    Parameters
    ----------
    path     : file to update (must already exist)
    ctime_ns : desired creation time in nanoseconds since the Unix epoch

    Raises
    ------
    OSError  : if OpenFile or SetFileTime fails (caller should catch and warn)
    """
    if sys.platform != "win32":
        return

    import ctypes
    import ctypes.wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Convert nanoseconds-since-Unix-epoch → 100-nanosecond intervals
    # since 1601-01-01 (Windows FILETIME). The offset between the two
    # epochs is 116444736000000000 × 100ns ticks.
    EPOCH_DIFF_100NS = 116_444_736_000_000_000
    filetime_ticks = (ctime_ns // 100) + EPOCH_DIFF_100NS

    # Pack into a FILETIME struct (two DWORDs: low, high).
    ft = ctypes.wintypes.FILETIME(
        filetime_ticks & 0xFFFFFFFF,       # dwLowDateTime
        (filetime_ticks >> 32) & 0xFFFFFFFF,  # dwHighDateTime
    )

    # Open with GENERIC_WRITE + FILE_FLAG_BACKUP_SEMANTICS.
    # OPEN_EXISTING (3) | FILE_FLAG_BACKUP_SEMANTICS (0x02000000) lets us
    # open a file handle suitable for SetFileTime without truncating it.
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
        raise OSError(ctypes.get_last_error(),
                      f"CreateFileW failed on {path}")
    try:
        # SetFileTime(handle, lpCreationTime, lpLastAccessTime, lpLastWriteTime)
        # Passing NULL for the last two leaves atime/mtime untouched —
        # os.utime() already set those.
        ok = kernel32.SetFileTime(handle, ctypes.byref(ft), None, None)
        if not ok:
            raise OSError(ctypes.get_last_error(),
                          f"SetFileTime failed on {path}")
    finally:
        kernel32.CloseHandle(handle)


# =========================================================================
# Codec backend detection
# =========================================================================

def _try_pillow_heif() -> bool:
    """
    Try to register pillow_heif opener with Pillow.
    """
    try:
        # register_heif_opener teaches Pillow to recognise .heic files as
        # readable image formats — after this, Image.open() works on them.
        pillow_heif.register_heif_opener()
        return True
    except OSError:
        return False

# Module-level constants — set once at import, used per file.
PILLOW_OK = _try_pillow_heif()


# =========================================================================
# Conversion
# =========================================================================

def convert_with_pillow(
    src: Path, out: Path, quality: int, metadata: bool
) -> None:
    """
    Convert HEIC to JPEG using Pillow + pillow-heif.

    metadata=True → grab the raw EXIF bytes from the source image and
                    embed them verbatim in the JPEG. Without this flag
                    only the rotation baked in by exif_transpose is kept
                    and the EXIF block is dropped.
    """
    with Image.open(src) as im:
        # Bake in EXIF rotation and convert to RGB (remove transparency).
        # exif_transpose returns a *new* image, so we need to grab the
        # EXIF data before it may be transformed away, if we want to keep it.
        exif_bytes: bytes | None = None
        if metadata:
            # getexif() is available on most Pillow image objects; fall back
            # to tobytes() when the helper isn't present (e.g. older Pillow).
            raw = im.info.get("exif")
            if raw is not None:
                exif_bytes = raw
            else:
                try:
                    exif_bytes = im.getexif().tobytes()
                except Exception:
                    exif_bytes = None

        im = ImageOps.exif_transpose(im)

        if im.mode != "RGB":
            im = im.convert("RGB")

        save_kwargs: dict = {"format": "JPEG", "quality": quality}

        # Storage-saving: extra Huffman pass + standard 4:2:0 chroma subsampling.
        save_kwargs["optimize"] = True
        save_kwargs["subsampling"] = 2

        if exif_bytes:
            save_kwargs["exif"] = exif_bytes

        im.save(out, **save_kwargs)


# =========================================================================
# Single-file orchestration
# =========================================================================

def convert_one(
    src: Path,
    quality: int,
    keep: bool,
    force: bool,
    metadata: bool,
    times: bool,
) -> tuple[Path, str, str | None]:
    """Convert exactly one file. Pure function — safe to call from any thread.

    This wraps the chosen backend with the cross-cutting concerns:
    skip-if-exists, atomic write, optional original deletion,
    and structured error reporting.

    Returns
    -------
    A 3-tuple ``(path, status, error_msg)`` where:
      - ``path`` echoes the input path (useful when results come back
        out-of-order from a parallel pool).
      - ``status`` is one of ``"ok"``, ``"skip"`` (output already existed
        and -f wasn't set), or ``"fail"``.
      - ``error_msg`` is None on success/skip, or a human-readable
        error string on failure. Also used for "ok with warning" cases
        (e.g. converted but couldn't delete the original).
    """
    out = src.with_suffix(".jpg")

    # Skip if the destination already exists
    if out.exists() and not force:
        return src, "skip", None

    # Snapshot timestamps before we touch anything so we can restore them
    # onto the output file regardless of how long the conversion takes.
    src_stat: os.stat_result | None = None
    if times:
        try:
            src_stat = src.stat()
        except OSError:
            src_stat = None

    # Atomic write: write to a temp file, then rename onto the final name.
    # The PID suffix on the tmp name lets multiple worker processes coexist
    # without clobbering each other's temps.
    tmp = out.with_suffix(out.suffix + f".tmp.{os.getpid()}")
    warnings: list[str] = []
    try:
        convert_with_pillow(src, tmp, quality, metadata)

        # Conversion succeeded: atomically move tmp into place.
        os.replace(tmp, out)

        # Restore original timestamps onto the output.
        # os.utime takes (atime_ns, mtime_ns) in nanoseconds.
        #
        # Creation time (st_birthtime) is read-only on most Linux filesystems
        # — no portable syscall exists to set it. As a workaround we write
        # birthtime into mtime when available (macOS/APFS populates
        # st_birthtime; on Linux it's absent and we fall back to mtime).
        # The real mtime of a HEIC is almost always identical to birthtime
        # anyway (the file is written once, never modified), so this
        # faithfully represents "when the photo was taken" in the output's
        # mtime field — the one timestamp every tool shows.
        if times and src_stat is not None:
            try:
                birthtime_ns = getattr(src_stat, "st_birthtime_ns", None)
                if birthtime_ns is None and hasattr(src_stat, "st_birthtime"):
                    birthtime_ns = int(src_stat.st_birthtime * 1e9)
                # On macOS/Windows st_birthtime is available so we use it
                # for mtime too (see comment above). On Linux we fall back
                # to the source mtime.
                mtime_ns = birthtime_ns if birthtime_ns is not None \
                    else src_stat.st_mtime_ns
                os.utime(out, ns=(src_stat.st_atime_ns, mtime_ns))

                # Windows exposes a writable creation-time field via
                # SetFileTime. On macOS/Linux this is a no-op.
                ctime_ns = birthtime_ns if birthtime_ns is not None \
                    else src_stat.st_mtime_ns
                _set_creation_time_windows(out, ctime_ns)
            except OSError as e:
                warnings.append(f"warning: could not restore timestamps: {e}")

        # Optionally delete the original
        if not keep:
            try:
                src.unlink()
            except OSError as e:
                # Return "ok" with a warning rather than "fail" — the
                # user got their JPEG, they just have a stale HEIC.
                warnings.append(f"warning: could not delete original: {e}")

        warning_str = "; ".join(warnings) if warnings else None
        return src, "ok", warning_str

    except Exception as e:
        # Clean up the partial temp file before reporting the error.
        # Without this, repeated failed runs would litter the directory
        # with .tmp.PID files.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass  # nothing more we can do
        return src, "fail", f"{type(e).__name__}: {e}"


# =========================================================================
# File discovery
# =========================================================================

def collect_files(root: Path, recursive: bool = False) -> list[Path]:
    """Find all HEIC files under ``root``.

    If ``root`` is a single file, returns just that (if it has a .heic
    extension, else empty). If it's a directory:
      - recursive=False (default): walks one level deep (original behaviour).
      - recursive=True  (-R):      walks the entire subtree via rglob.

    The result is sorted for deterministic output — useful for tests and
    so that the user can mentally predict progress.

    Note on symlinks: ``iterdir`` and ``rglob`` do NOT follow directory
    symlinks by default, which is the safe behaviour. Following them risks
    infinite loops if there's a cycle.
    """
    if root.is_file():
        return [root] if root.suffix.lower() == ".heic" else []
    if not root.is_dir():
        return []
    if recursive:
        return sorted(
            f for f in root.rglob("*")
            if f.is_file() and f.suffix.lower() == ".heic"
        )
    return sorted(
        f for f in root.iterdir()
        if f.is_file() and f.suffix.lower() == ".heic"
    )


# =========================================================================
# Parallel runner
# =========================================================================

def run_pool(files, jobs, q, keep, force, metadata, times, verbose):
    """Run all conversions through a thread pool.

    Parameters
    ----------
    files : list of Path to convert
    jobs : int worker count
    q, keep, force, metadata, times, verbose : forwarded to convert_one

    Returns
    -------
    Tuple of ``(ok_count, skipped_count, failed_count)``.

    Signal handling
    ---------------
    Installs a SIGINT handler so the first Ctrl-C cancels pending work
    and lets in-flight conversions finish cleanly (preserving their
    atomic-write guarantee). A second Ctrl-C bypasses the handler and
    kills the process immediately.
    """
    ok = skipped = failed = 0
    interrupted = False

    def _handle_sigint(signum, frame):
        # Flag the loop to stop scheduling new work. Restoring the default
        # handler means a second Ctrl-C will hard-kill the process.
        nonlocal interrupted
        interrupted = True
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    # `hasattr` guard: SIGINT is universal, but being defensive for any
    # weird embedded Python environment costs nothing.
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _handle_sigint)

    # `with ThreadPoolExecutor(...)` ensures the pool is shut down cleanly
    # even if an exception escapes the loop. Without the context manager,
    # Python would leak worker threads.
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        # Submit everything up front. The pool internally queues anything
        # over `max_workers` and spawns them as workers free up.
        futures: dict[Future, Path] = {
            ex.submit(convert_one, f, q, keep, force, metadata, times): f
            for f in files
        }
        try:
            # `as_completed` yields futures as they finish, NOT in
            # submission order. This means progress feels smooth even if
            # one file is much slower than the rest.
            for fut in as_completed(futures):
                if interrupted:
                    # Cancel anything still queued (won't affect futures
                    # that are already running — those finish naturally).
                    for pending in futures:
                        pending.cancel()
                    break

                src, status, err = fut.result()
                if status == "ok":
                    ok += 1
                    if verbose:
                        print(f"ok: {src}" + (f" ({err})" if err else ""),
                              file=sys.stderr)
                elif status == "skip":
                    skipped += 1
                    if verbose:
                        print(f"skip: {src}", file=sys.stderr)
                else:  # status == "fail"
                    failed += 1
                    # Always print failures, even without -v. Silent
                    # failures are how data loss happens.
                    print(f"FAIL {src}: {err}", file=sys.stderr)
        except KeyboardInterrupt:
            # Belt-and-braces: handles the case where SIGINT arrives
            # between `as_completed` calls and our handler hasn't run.
            interrupted = True
            for pending in futures:
                pending.cancel()

    if interrupted:
        print("Interrupted.", file=sys.stderr)
    return ok, skipped, failed


# =========================================================================
# CLI
# =========================================================================

def parse_args(argv=None):
    """Build and run the argument parser.

    Splitting this out from main() makes it testable (you can call
    ``parse_args(["-q", "50", "/tmp"])`` from a unit test).
    """
    p = argparse.ArgumentParser(
        prog="heic2jpg",
        description="Fast HEIC to JPG converter",
    )
    p.add_argument("path", nargs="?", default=".",
                   help="File or directory to convert (default: current dir)")
    p.add_argument("-q", "--quality", type=int, default=30, metavar="[1-100]",
                   help="Target quality (default: 30)")
    p.add_argument("-m", "--metadata", action="store_true",
                   help="Preserve EXIF metadata")
    p.add_argument("-t", "--times", action="store_true",
                   help="Preserve source file timestamps")
    p.add_argument("-k", "--keep", action="store_true",
                   help="Keep originals (default: delete after conversion)")
    p.add_argument("-R", "--recursive", action="store_true",
                   help="Recurse into subdirectories")
    p.add_argument("-f", "--force", action="store_true",
                   help="Overwrite existing .jpg outputs")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=VERSION)

    return p.parse_args(argv)


def main(argv=None) -> int:
    """Entry point. Returns the process exit code (see module docstring)."""
    args = parse_args(argv)

    # --- Validate args -----------------------------------------------------
    if not (1 <= args.quality <= 100):
        print("Error: quality must be 1-100", file=sys.stderr)
        return 1

    # Bail early if no codec is available — better than failing one
    # file at a time later.
    if not PILLOW_OK:
        print("Error: no codec available. "
              "Install pillow-heif and register pillow_heif opener with Pillow.",
              file=sys.stderr)
        return 1

    # Resolve path
    src = Path(args.path).expanduser().resolve()
    if not src.exists():
        print(f"Error: '{src}' does not exist", file=sys.stderr)
        return 1
    if src.is_file() and src.suffix.lower() != ".heic":
        print(f"Error: '{src}' is not a HEIC file", file=sys.stderr)
        return 1
    if not src.is_file() and not src.is_dir():
        print(f"Error: '{src}' is not a file or directory", file=sys.stderr)
        return 1

    files = collect_files(src, recursive=args.recursive)
    if not files:
        print(f"No HEIC files found in: {src}", file=sys.stderr)
        return 2

    # No point spawning more workers than there are files.
    # Consider capping at 8 workers, beyond that the I/O bottleneck dominates
    # and you get diminishing returns.
    cpu_count = os.cpu_count() or 4
    jobs = min(cpu_count, len(files))

    if args.verbose:
        print(f"Converting {len(files)} files, q={args.quality}, "
              f"{jobs} threads, backend=pillow-heif, "
              f"recursive={args.recursive}, metadata={args.metadata}, "
              f"times={args.times}", file=sys.stderr)

    # --- Run ---------------------------------------------------------------
    t0 = time.perf_counter()

    if jobs == 1:
        # Skip the executor entirely for jobs=1 — no point paying queueing
        # overhead for a single worker. This branch is also useful for
        # debugging: stack traces don't get re-raised across worker
        # boundaries.
        ok = skipped = failed = 0
        for f in files:
            _, status, err = convert_one(
                f, args.quality, args.keep, args.force,
                args.metadata, args.times,
            )
            if status == "ok":
                ok += 1
            elif status == "skip":
                skipped += 1
            else:
                failed += 1
                print(f"FAIL {f}: {err}", file=sys.stderr)
    else:
        ok, skipped, failed = run_pool(
            files, jobs,
            args.quality, args.keep, args.force,
            args.metadata, args.times, args.verbose,
        )
    dt = time.perf_counter() - t0

    # Print a summary if we did anything noteworthy
    if args.verbose or failed:
        print(f"Done in {dt:.2f}s — ok={ok} skipped={skipped} failed={failed}",
              file=sys.stderr)

    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main())