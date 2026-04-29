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

VERSION = "2.0.0"


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

def convert_with_pillow(src: Path, out: Path, quality: int) -> None:
    """
    Convert HEIC to JPEG using Pillow + pillow-heif.
    """
    with Image.open(src) as im:
        # Bake in EXIF rotation and convert to RGB (remove transparency)
        im = ImageOps.exif_transpose(im)
        
        if im.mode != "RGB":
            im = im.convert("RGB")
        
        # Save with performance-oriented settings
        # optimize=False: faster encoding by skipping Huffman passes
        # subsampling=0: keeps colour data at full resolution (4:4:4)
        im.save(out, "JPEG", quality=quality, optimize=False)


# =========================================================================
# Single-file orchestration
# =========================================================================

def convert_one(src: Path, quality: int, keep: bool, force: bool
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

    # Atomic write: write to a temp file, then rename onto the final name.
    # The PID suffix on the tmp name lets multiple worker processes coexist
    # without clobbering each other's temps.
    tmp = out.with_suffix(out.suffix + f".tmp.{os.getpid()}")
    try:
        convert_with_pillow(src, tmp, quality)

        # Conversion succeeded: atomically move tmp into place.
        os.replace(tmp, out)

        # Optionally delete the original
        if not keep:
            try:
                src.unlink()
            except OSError as e:
                # Return "ok" with a warning rather than "fail" — the
                # user got their JPEG, they just have a stale HEIC.
                return src, "ok", f"warning: could not delete original: {e}"
        return src, "ok", None

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

def collect_files(root: Path) -> list[Path]:
    """Find all HEIC files under ``root``.

    If ``root`` is a single file, returns just that (if it has a .heic
    extension, else empty). If it's a directory, walks one level deep.

    The result is sorted for deterministic output — useful for tests and
    so that the user can mentally predict progress.

    Note on symlinks: ``iterdir`` does NOT follow directory symlinks by
    default, which is the safe behaviour. Following them risks infinite
    loops if there's a cycle.
    """
    if root.is_file():
        return [root] if root.suffix.lower() == ".heic" else []
    if not root.is_dir():
        return []
    return sorted(
        f for f in root.iterdir()
        if f.is_file() and f.suffix.lower() == ".heic"
    )


# =========================================================================
# Parallel runner
# =========================================================================

def run_pool(files, jobs, q, keep, force, verbose):
    """Run all conversions through a thread pool.

    Parameters
    ----------
    files : list of Path to convert
    jobs : int worker count
    q, keep, force, verbose : forwarded to convert_one

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
            ex.submit(convert_one, f, q, keep, force): f for f in files
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
    p.add_argument("-q", "--quality", type=int, default=30, metavar="[1-100]]",
                   help="Target quality (default: 30)")
    p.add_argument("-k", "--keep", action="store_true",
                   help="Keep originals (default: delete after conversion)")
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

    files = collect_files(src)
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
              f"{jobs} threads, backend=pillow-heif", file=sys.stderr)

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
                f, args.quality, args.keep, args.force
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
            args.quality, args.keep, args.force, args.verbose,
        )
    dt = time.perf_counter() - t0

    # Print a summary if we did anything noteworthy
    if args.verbose or failed:
        print(f"Done in {dt:.2f}s — ok={ok} skipped={skipped} failed={failed}",
              file=sys.stderr)

    return 3 if failed else 0


if __name__ == "__main__":
    # `sys.exit` propagates the int to the OS as the process exit code.
    sys.exit(main())