"""Package-wide fixtures shared across the test suite."""

import pillow_heif
import pytest

from .helpers import make_heic

pillow_heif.register_heif_opener()


@pytest.fixture
def single_heic(tmp_path):
    return make_heic(tmp_path / "single.heic")


@pytest.fixture
def heic_dir(tmp_path):
    make_heic(tmp_path / "a.heic", color=(255, 0, 0))
    make_heic(tmp_path / "b.heic", color=(0, 255, 0))
    make_heic(tmp_path / "c.heic", color=(0, 0, 255))
    return tmp_path


@pytest.fixture
def nested_heic_dir(tmp_path):
    make_heic(tmp_path / "top.heic")
    (tmp_path / "sub").mkdir()
    make_heic(tmp_path / "sub" / "deep.heic")
    (tmp_path / "sub" / "subsub").mkdir()
    make_heic(tmp_path / "sub" / "subsub" / "deeper.heic")
    (tmp_path / "other").mkdir()
    make_heic(tmp_path / "other" / "another.heic")
    return tmp_path
