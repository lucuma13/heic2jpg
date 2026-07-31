#!/usr/bin/env python3
"""
`heic2jpg` is a fast HEIC to JPEG converter designed to resolve the compatibility gap
between iPhone image files and common desktop workflows. Conversion runs in parallel
across worker threads using [pillow-heif](https://pypi.org/project/pillow-heif/) for
fast, in-process decoding in macOS, Linux or Windows.

Usage examples:

Convert a single file:

```bash
heic2jpg path/to/photo.HEIC
```

Convert all files in directory:
```sh
heic2jpg path/to/photo/album
heic2jpg                        # current directory
```
"""
# Copyright (c) 2026 Luis Gómez Gutiérrez. License: MIT.

import argparse
import os
import sys
import time
from pathlib import Path

from heic2jpg import __version__
from heic2jpg.convert import Options, _try_pillow_heif
from heic2jpg.discovery import announce_diversions, collect_files, plan_outputs
from heic2jpg.runner import run_pool
from heic2jpg.update_checker import run_with_update_check


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build and run the argument parser.

    Splitting this out from main() makes it testable (you can call
    ``parse_args(["-q", "50", "/tmp"])`` from a unit test).
    """
    p = argparse.ArgumentParser(
        prog="heic2jpg",
        description="Fast HEIC to JPG converter",
    )
    defaults = Options()
    p.add_argument("path", nargs="?", default=".", help="File or directory to convert (default: current dir)")
    p.add_argument(
        "-q",
        "--quality",
        type=int,
        default=defaults.quality,
        metavar="[1-100]",
        help=f"Target quality (default: {defaults.quality})",
    )
    p.add_argument("-m", "--metadata", action="store_true", help="Preserve EXIF metadata")
    p.add_argument("-t", "--times", action="store_true", help="Preserve source file timestamps")
    p.add_argument("-k", "--keep", action="store_true", help="Keep originals (default: delete after conversion)")
    p.add_argument("-R", "--recursive", action="store_true", help="Recurse into subdirectories")
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite an existing .jpg output instead of writing a numbered variant",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point: the CLI body runs inside the PyPI update check."""
    return run_with_update_check("heic2jpg", __version__, lambda: _main(argv))


def _main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    """Entry point.

    Returns the process exit code:
      0   - success (everything converted or skipped)
      1   - bad arguments or unusable environment (codec missing, bad path)
      2   - no HEIC files found
      3   - at least one conversion failed
      130 - interrupted by Ctrl-C
    """
    args = parse_args(argv)

    # --- Validate args -----------------------------------------------------
    if not (1 <= args.quality <= 100):  # noqa: PLR2004 - quality is 1-100%
        print("Error: quality must be 1-100", file=sys.stderr)
        return 1

    # Register the codec lazily, and bail early if it's unavailable -
    # better than failing one file at a time later.
    if not _try_pillow_heif():
        print(
            "Error: could not initialise the HEIC codec. "
            "The native libheif library may be missing or incompatible - "
            "try reinstalling pillow-heif or installing libheif for your OS.",
            file=sys.stderr,
        )
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

    # Resolve output-name conflicts (existing files, IMG.heic vs IMG.HEIC)
    # up front, and say which files got diverted to a numbered name.
    outs = plan_outputs(files, force=args.force)
    announce_diversions(files, outs)

    opts = Options(
        quality=args.quality,
        keep=args.keep,
        force=args.force,
        metadata=args.metadata,
        times=args.times,
    )

    # No point spawning more workers than there are files.
    cpu_count = os.cpu_count() or 4
    jobs = min(cpu_count, len(files))

    if args.verbose:
        print(
            f"Converting {len(files)} files, q={args.quality}, "
            f"{jobs} threads, backend=pillow-heif, "
            f"recursive={args.recursive}, metadata={args.metadata}, "
            f"times={args.times}",
            file=sys.stderr,
        )

    # --- Run ---------------------------------------------------------------
    t0 = time.perf_counter()
    ok, skipped, failed, interrupted = run_pool(files, outs, jobs, opts, args.verbose)
    dt = time.perf_counter() - t0

    # Print a summary if we did anything noteworthy
    if args.verbose or failed:
        print(f"Done in {dt:.2f}s - ok={ok} skipped={skipped} failed={failed}", file=sys.stderr)

    if interrupted:
        return 130

    return 3 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
