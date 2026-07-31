"""Per-file HEIC → JPEG conversion."""

import contextlib
import functools
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

from heic2jpg.osutils import _restore_timestamps

# -------------------------------------------------------------------------
# Codec backend detection
# -------------------------------------------------------------------------


@functools.cache
def _try_pillow_heif() -> bool:
    """
    Try to register the pillow_heif opener with Pillow (once per process).

    Called lazily from main() rather than at import time, so importing this
    module has no side effects: Pillow's process-global format registry is only
    mutated when a conversion is actually about to happen. The result is cached
    - registration happens at most once per process.

    pillow-heif is a hard dependency so ImportError won't occur here, but
    register_heif_opener() can raise OSError if the native libheif shared
    library is missing or incompatible on the current system.
    """
    try:
        # register_heif_opener teaches Pillow to recognise .heic files as
        # readable image formats - after this, Image.open() works on them.
        pillow_heif.register_heif_opener()
        return True
    except OSError:
        return False


# -------------------------------------------------------------------------
# Run options and per-file results
# -------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """
    Conversion settings shared by every file in a run.

    These are also the CLI defaults - parse_args reads them, so they live in one
    place. Frozen so a single instance can be shared safely across worker
    threads.
    """

    quality: int = 30
    keep: bool = False
    force: bool = False
    metadata: bool = False
    times: bool = False


class Status(Enum):
    """Outcome of converting a single file."""

    OK = "ok"
    SKIP = "skip"  # output already existed and -f wasn't set
    FAIL = "fail"


@dataclass
class Result:
    """
    Outcome of converting one file.

    ``src`` echoes the input path (useful when results come back out-of-order
    from a parallel pool). ``error`` is set only when status is FAIL.
    ``warnings`` collects non-fatal issues on an otherwise successful conversion
    (e.g. the JPEG was written but the original couldn't be deleted).
    """

    src: Path
    status: Status
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


# -------------------------------------------------------------------------
# Conversion
# -------------------------------------------------------------------------


def convert_with_pillow(src: Path, out: Path, quality: int, metadata: bool) -> None:
    """
    Convert HEIC to JPEG using Pillow + pillow-heif.

    metadata=True → embed the source's EXIF bytes in the JPEG, except the
                    Orientation tag: rotation is always normalized into the
                    pixels by exif_transpose, so Orientation is stripped to
                    avoid rotating the already-upright pixels a second time.
                    Without this flag the EXIF block is dropped entirely.

    The source's ICC colour profile is always carried over, independent of
    ``metadata``.
    """
    with Image.open(src) as im:
        # Bake in EXIF rotation and convert to RGB (remove transparency).
        rgb = ImageOps.exif_transpose(im)

        # Take EXIF from the transposed image: exif_transpose strips the
        # Orientation tag from its result, so whatever we embed can never rotate
        # the already-rotated pixels a second time. (When nothing needed
        # transposing, the copy carries the source EXIF unchanged.)
        exif_bytes: bytes | None = None
        if metadata:
            exif_bytes = rgb.info.get("exif")
            if exif_bytes is None:
                with contextlib.suppress(Exception):  # getexif absent on some Pillow builds
                    exif_bytes = rgb.getexif().tobytes() or None

        # Grab the ICC profile before any mode conversion, which drops .info.
        icc_bytes: bytes | None = rgb.info.get("icc_profile")

        if rgb.mode != "RGB":
            rgb = rgb.convert("RGB")

        # optimize: extra Huffman pass. subsampling=2: standard 4:2:0 chroma subsampling.
        save_kwargs: dict = {
            "format": "JPEG",
            "quality": quality,
            "optimize": True,
            "subsampling": 2,
        }
        if exif_bytes:
            save_kwargs["exif"] = exif_bytes
        if icc_bytes:
            save_kwargs["icc_profile"] = icc_bytes

        # Keep passing a path here, not a BytesIO or other file-like object.
        # Pillow's JPEG encoder only releases the GIL when it can write straight
        # to a real file, encoding in a single C call; given a file-like object
        # it falls back to a chunked loop that returns to Python between chunks
        # and holds the GIL throughout.
        rgb.save(out, **save_kwargs)


# -------------------------------------------------------------------------
# Single-file orchestration
# -------------------------------------------------------------------------


def convert_one(src: Path, opts: Options, out: Path | None = None) -> Result:
    """
    Convert exactly one file. Pure function - safe to call from any thread.

    This wraps the chosen backend with the cross-cutting concerns:
    skip-if-exists, atomic write, optional original deletion, and structured
    error reporting.

    ``out`` is the destination path; it defaults to ``src`` with a .jpg suffix.
    main() passes pre-computed unique outputs (see plan_outputs) so that sources
    differing only in extension case can't clobber each other's output or share
    a temp file.
    """
    natural = src.with_suffix(".jpg")
    if out is None:
        out = natural

    # Skip if the destination already exists. plan_outputs normally diverts
    # around existing files, so this only fires when one appeared after planning
    # - or on direct API calls. ``force`` consents to overwriting a source's own
    # natural name, never a diverted one.
    if out.exists() and not (opts.force and out == natural):
        return Result(src, Status.SKIP)

    # Snapshot timestamps before we touch anything so we can restore them onto
    # the output file regardless of how long the conversion takes.
    src_stat: os.stat_result | None = None
    if opts.times:
        try:
            src_stat = src.stat()
        except OSError:
            src_stat = None

    # Atomic write: write to a temp file, then rename onto the final name. `out`
    # is unique per source (main() resolves collisions up front via
    # plan_outputs), so worker threads never share a tmp path. The PID suffix
    # guards against two separate heic2jpg processes running concurrently in the
    # same directory.
    tmp = out.with_suffix(out.suffix + f".tmp.{os.getpid()}")
    warnings: list[str] = []
    try:
        convert_with_pillow(src, tmp, opts.quality, opts.metadata)

        # Conversion succeeded: atomically move tmp into place.
        tmp.replace(out)

        # Restore original timestamps onto the output.
        if opts.times and src_stat is not None:
            try:
                _restore_timestamps(out, src_stat)
            except OSError as e:
                warnings.append(f"could not restore timestamps: {e}")

        # Optionally delete the original
        if not opts.keep:
            try:
                src.unlink()
            except OSError as e:
                # A warning rather than FAIL - the user got their JPEG,
                # they just have a stale HEIC.
                warnings.append(f"could not delete original: {e}")

        return Result(src, Status.OK, warnings=warnings)

    except Exception as e:  # noqa: BLE001 - any conversion failure becomes Status.FAIL
        return Result(src, Status.FAIL, error=f"{type(e).__name__}: {e}")
    finally:
        # Clean up the partial temp file on every failure path - including
        # KeyboardInterrupt, which must propagate but not litter the directory
        # with .tmp.PID files. After success the rename already consumed the
        # temp file, so the unlink is a suppressed no-op.
        with contextlib.suppress(OSError):
            tmp.unlink()
