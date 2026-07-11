"""
Importable test helpers shared across the test suite.

Plain functions only — fixtures live in conftest.py.
"""

import io
from pathlib import Path

import pillow_heif
from PIL import Image


def make_heic(path: Path, size=(64, 64), color=(100, 150, 200)) -> Path:
    img = Image.new("RGB", size, color=color)
    pillow_heif.from_pillow(img).save(path, format="HEIF")
    return path


def make_rgba_heic(path: Path, size=(32, 32), color=(100, 150, 200, 128)) -> Path:
    img = Image.new("RGBA", size, color=color)
    pillow_heif.from_pillow(img).save(path, format="HEIF")
    return path


def is_valid_jpeg(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(2) == b"\xff\xd8"


def make_heic_bytes(size=(8, 8), color=(128, 128, 128)) -> bytes:
    """Return raw bytes of a minimal valid HEIC file."""
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    pillow_heif.from_pillow(img).save(buf, format="HEIF")
    return buf.getvalue()


VALID_HEIC_BYTES = make_heic_bytes()  # cached once — reused across tests
