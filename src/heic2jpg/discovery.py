"""Finding HEIC files and planning their output paths."""

import sys
from pathlib import Path


def collect_files(root: Path, recursive: bool = False) -> list[Path]:
    """
    Find all HEIC files under ``root``.

    If ``root`` is a single file, returns just that (if it has a .heic
    extension, else empty). If it's a directory:
      - recursive=False (default): walks one level deep (original behaviour).
      - recursive=True  (-R):      walks the entire subtree via rglob.

    The result is sorted for deterministic output - useful for tests and so that
    the user can mentally predict progress.

    Note on symlinks: ``iterdir`` and ``rglob`` do NOT follow directory symlinks
    by default, which is the safe behaviour. Following them risks infinite loops
    if there's a cycle.
    """
    if root.is_file():
        return [root] if root.suffix.lower() == ".heic" else []
    if not root.is_dir():
        return []
    entries = root.rglob("*") if recursive else root.iterdir()
    return sorted(f for f in entries if f.is_file() and f.suffix.lower() == ".heic")


def plan_outputs(files: list[Path], force: bool = False) -> list[Path]:
    """
    Map each source file to a free, unique .jpg output path.

    Every source gets converted - a taken output name never causes a
    skip or an overwrite. A name is taken when another source in this
    batch already claimed it (``IMG.heic`` and ``IMG.HEIC`` both →
    ``IMG.jpg`` on case-sensitive filesystems) or when a file already
    exists there on disk. Taken names divert to a numeric suffix:
    ``IMG-1.jpg``, ``IMG-2.jpg``, …

    ``force`` lets a source reclaim its own natural name (``IMG.heic``
    → ``IMG.jpg``) even if it exists on disk. It never applies to the
    suffixed names: an existing ``IMG-1.jpg`` may be an unrelated photo
    we can't identify as ours, so it is always diverted around.

    The exists() checks are a snapshot; convert_one re-checks before
    writing as a backstop against files appearing mid-run.
    """
    claimed: set[Path] = set()
    outs: list[Path] = []
    for src in files:
        natural = src.with_suffix(".jpg")
        out = natural
        n = 0
        while out in claimed or (out.exists() and not (force and out == natural)):
            n += 1
            out = src.with_name(f"{src.stem}-{n}.jpg")
        claimed.add(out)
        outs.append(out)
    return outs


def announce_diversions(files: list[Path], outs: list[Path]) -> None:
    """Warn about every output diverted from its natural name."""
    for src, out in zip(files, outs, strict=True):
        natural = src.with_suffix(".jpg")
        if out.name != natural.name:
            print(f"warning: '{natural.name}' is taken - converting {src} to '{out.name}'", file=sys.stderr)
