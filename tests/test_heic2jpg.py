"""Tests for heic2jpg."""

import contextlib
import ctypes
import ctypes.wintypes
import io
import os
import re
import signal
import struct
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pillow_heif
import pytest
from hypothesis import HealthCheck, given, settings, strategies
from PIL import Image

from heic2jpg import heic2jpg

pillow_heif.register_heif_opener()

# ===========================================================================
# Helpers
# ===========================================================================


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


def _make_heic_bytes(size=(8, 8), color=(128, 128, 128)) -> bytes:
    """Return raw bytes of a minimal valid HEIC file."""
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    pillow_heif.from_pillow(img).save(buf, format="HEIF")
    return buf.getvalue()


VALID_HEIC_BYTES = _make_heic_bytes()  # cached once — reused across tests


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def single_heic(tmp_path) -> Path:
    return make_heic(tmp_path / "single.heic")


@pytest.fixture
def heic_dir(tmp_path) -> Path:
    make_heic(tmp_path / "a.heic", color=(255, 0, 0))
    make_heic(tmp_path / "b.heic", color=(0, 255, 0))
    make_heic(tmp_path / "c.heic", color=(0, 0, 255))
    return tmp_path


@pytest.fixture
def nested_heic_dir(tmp_path) -> Path:
    make_heic(tmp_path / "top.heic")
    (tmp_path / "sub").mkdir()
    make_heic(tmp_path / "sub" / "deep.heic")
    (tmp_path / "sub" / "subsub").mkdir()
    make_heic(tmp_path / "sub" / "subsub" / "deeper.heic")
    (tmp_path / "other").mkdir()
    make_heic(tmp_path / "other" / "another.heic")
    return tmp_path


# ===========================================================================
# collect_files
# ===========================================================================


class TestCollectFiles:
    def test_single_heic_file(self, single_heic):
        assert heic2jpg.collect_files(single_heic) == [single_heic]

    def test_single_non_heic_file(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG\r\n")
        assert heic2jpg.collect_files(f) == []

    def test_empty_directory(self, tmp_path):
        assert heic2jpg.collect_files(tmp_path) == []

    def test_directory_returns_three_files(self, heic_dir):
        assert len(heic2jpg.collect_files(heic_dir)) == 3

    def test_result_is_sorted(self, heic_dir):
        result = heic2jpg.collect_files(heic_dir)
        assert result == sorted(result)

    def test_nonexistent_path_returns_empty(self, tmp_path):
        assert heic2jpg.collect_files(tmp_path / "ghost") == []

    def test_ignores_non_heic_files(self, tmp_path):
        make_heic(tmp_path / "photo.heic")
        (tmp_path / "photo.jpg").write_bytes(b"")
        (tmp_path / "notes.txt").write_bytes(b"")
        assert len(heic2jpg.collect_files(tmp_path)) == 1

    def test_case_insensitive_extension(self, tmp_path):
        (tmp_path / "upper.HEIC").write_bytes(b"")
        (tmp_path / "lower.heic").write_bytes(b"")
        (tmp_path / "mixed.HeIc").write_bytes(b"")
        assert len(heic2jpg.collect_files(tmp_path)) == 3

    def test_non_recursive_does_not_descend(self, nested_heic_dir):
        result = heic2jpg.collect_files(nested_heic_dir, recursive=False)
        assert [p.name for p in result] == ["top.heic"]

    def test_recursive_finds_all(self, nested_heic_dir):
        assert len(heic2jpg.collect_files(nested_heic_dir, recursive=True)) == 4

    def test_recursive_result_is_sorted(self, nested_heic_dir):
        result = heic2jpg.collect_files(nested_heic_dir, recursive=True)
        assert result == sorted(result)


# ===========================================================================
# plan_outputs
# ===========================================================================


class TestPlanOutputs:
    """Batch-collision cases use fake /x paths (nothing exists there);
    disk-diversion cases create real files in tmp_path."""

    def test_empty(self):
        assert heic2jpg.plan_outputs([]) == []

    def test_no_collision_maps_to_jpg(self):
        files = [Path("/x/a.heic"), Path("/x/b.HEIC")]
        assert heic2jpg.plan_outputs(files) == [Path("/x/a.jpg"), Path("/x/b.jpg")]

    def test_case_variant_collision_gets_numeric_suffix(self):
        files = [Path("/x/IMG.HEIC"), Path("/x/IMG.heic")]
        assert heic2jpg.plan_outputs(files) == [Path("/x/IMG.jpg"), Path("/x/IMG-1.jpg")]

    def test_same_name_in_different_dirs_does_not_collide(self):
        files = [Path("/x/IMG.heic"), Path("/y/IMG.heic")]
        assert heic2jpg.plan_outputs(files) == [Path("/x/IMG.jpg"), Path("/y/IMG.jpg")]

    def test_suffix_cascades_when_suffixed_name_also_taken(self):
        files = [Path("/x/IMG-1.heic"), Path("/x/IMG.HEIC"), Path("/x/IMG.heic")]
        assert heic2jpg.plan_outputs(files) == [
            Path("/x/IMG-1.jpg"),
            Path("/x/IMG.jpg"),
            Path("/x/IMG-2.jpg"),
        ]

    def test_outputs_are_always_unique(self):
        files = [Path(f"/x/IMG.{ext}") for ext in ("heic", "HEIC", "HeIc", "hEiC")]
        outs = heic2jpg.plan_outputs(files)
        assert len(set(outs)) == len(files)

    def test_diverts_around_existing_file(self, tmp_path):
        (tmp_path / "IMG.jpg").write_bytes(b"bystander")
        assert heic2jpg.plan_outputs([tmp_path / "IMG.heic"]) == [tmp_path / "IMG-1.jpg"]

    def test_diverts_past_existing_suffixed_file(self, tmp_path):
        (tmp_path / "IMG.jpg").write_bytes(b"bystander")
        (tmp_path / "IMG-1.jpg").write_bytes(b"also taken")
        assert heic2jpg.plan_outputs([tmp_path / "IMG.heic"]) == [tmp_path / "IMG-2.jpg"]

    def test_force_reclaims_natural_name_only(self, tmp_path):
        """-f overwrites IMG.heic's own IMG.jpg, but an existing IMG-1.jpg
        may be an unrelated photo and is still diverted around."""
        (tmp_path / "IMG.jpg").write_bytes(b"mine")
        (tmp_path / "IMG-1.jpg").write_bytes(b"bystander")
        files = [tmp_path / "IMG.heic", tmp_path / "IMG.HEIC"]
        assert heic2jpg.plan_outputs(files, force=True) == [
            tmp_path / "IMG.jpg",
            tmp_path / "IMG-2.jpg",
        ]


# ===========================================================================
# convert_with_pillow
# ===========================================================================


class TestConvertWithPillow:
    def _mock_image(self, info=None, mode="RGB"):
        """Return a (mock_img, transposed) pair wired up for Image.open context.

        EXIF is read off the exif_transpose result (its Orientation tag is
        already stripped there), so ``info`` lands on the transposed mock.
        """
        mock_img = MagicMock()
        mock_img.__enter__ = MagicMock(return_value=mock_img)
        mock_img.__exit__ = MagicMock(return_value=False)
        transposed = MagicMock()
        transposed.info = info if info is not None else {}
        transposed.mode = mode
        transposed.save = MagicMock()
        return mock_img, transposed

    def test_produces_valid_jpeg(self, single_heic, tmp_path):
        out = tmp_path / "out.jpg"
        heic2jpg.convert_with_pillow(single_heic, out, quality=85, metadata=False)
        assert is_valid_jpeg(out)

    def test_output_is_rgb(self, single_heic, tmp_path):
        out = tmp_path / "out.jpg"
        heic2jpg.convert_with_pillow(single_heic, out, quality=85, metadata=False)
        with Image.open(out) as im:
            assert im.mode == "RGB"

    def test_rgba_source_converted_to_rgb(self, tmp_path):
        """RGBA input hits the im.mode != 'RGB' branch."""
        src = make_rgba_heic(tmp_path / "rgba.heic")
        out = tmp_path / "out.jpg"
        heic2jpg.convert_with_pillow(src, out, quality=85, metadata=False)
        assert is_valid_jpeg(out)
        with Image.open(out) as im:
            assert im.mode == "RGB"

    def test_quality_affects_file_size(self, single_heic, tmp_path):
        lo, hi = tmp_path / "lo.jpg", tmp_path / "hi.jpg"
        heic2jpg.convert_with_pillow(single_heic, lo, quality=1, metadata=False)
        heic2jpg.convert_with_pillow(single_heic, hi, quality=95, metadata=False)
        assert lo.stat().st_size < hi.stat().st_size

    def test_output_dimensions_match_source(self, single_heic, tmp_path):
        out = tmp_path / "out.jpg"
        heic2jpg.convert_with_pillow(single_heic, out, quality=85, metadata=False)
        with Image.open(single_heic) as src_im, Image.open(out) as out_im:
            assert src_im.size == out_im.size

    def test_metadata_true_embeds_exif(self, tmp_path):
        """The transposed image's info['exif'] must be forwarded to save()."""
        fake_exif = b"Exif\x00\x00" + b"II" + struct.pack("<H", 42) + struct.pack("<I", 8) + struct.pack("<H", 0)
        mock_img, transposed = self._mock_image(info={"exif": fake_exif})
        with (
            patch("heic2jpg.heic2jpg.Image.open", return_value=mock_img),
            patch("heic2jpg.heic2jpg.ImageOps.exif_transpose", return_value=transposed),
        ):
            heic2jpg.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=True)
        assert transposed.save.call_args[1]["exif"] == fake_exif

    def test_metadata_true_getexif_fallback(self, tmp_path):
        """When info has no 'exif', fall back to the transposed getexif().tobytes()."""
        fake_exif = b"Exif\x00\x00II"
        mock_img, transposed = self._mock_image()
        transposed.getexif.return_value.tobytes.return_value = fake_exif
        with (
            patch("heic2jpg.heic2jpg.Image.open", return_value=mock_img),
            patch("heic2jpg.heic2jpg.ImageOps.exif_transpose", return_value=transposed),
        ):
            heic2jpg.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=True)
        assert transposed.save.call_args[1]["exif"] == fake_exif

    def test_metadata_getexif_exception_skipped(self, tmp_path):
        """If getexif().tobytes() raises, exif is silently skipped."""
        mock_img, transposed = self._mock_image()
        transposed.getexif.return_value.tobytes.side_effect = RuntimeError("no exif")
        with (
            patch("heic2jpg.heic2jpg.Image.open", return_value=mock_img),
            patch("heic2jpg.heic2jpg.ImageOps.exif_transpose", return_value=transposed),
        ):
            heic2jpg.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=True)
        assert "exif" not in transposed.save.call_args[1]

    def test_orientation_is_baked_in_and_stripped(self, tmp_path):
        """A source tagged Orientation=6 (90° CW) is physically rotated, and the
        Orientation tag is absent from the output so pixels aren't rotated twice.

        The HEIC encoder normalizes Orientation to 1, so the oriented source is
        built in memory and fed through the *real* exif_transpose."""
        src = Image.new("RGB", (40, 20), (100, 150, 200))
        exif = src.getexif()
        exif[0x0112] = 6  # Orientation tag
        src.info["exif"] = exif.tobytes()

        out = tmp_path / "out.jpg"
        with patch("heic2jpg.heic2jpg.Image.open", return_value=src):
            heic2jpg.convert_with_pillow(tmp_path / "x.heic", out, quality=90, metadata=True)

        with Image.open(out) as im:
            assert im.size == (20, 40)  # width/height swapped by the rotation
            assert im.getexif().get(0x0112) is None

    def test_raises_on_nonexistent_source(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            heic2jpg.convert_with_pillow(tmp_path / "ghost.heic", tmp_path / "out.jpg", quality=85, metadata=False)


# ===========================================================================
# convert_one
# ===========================================================================


class TestConvertOne:
    def _call(self, src, out=None, **opt_kwargs):
        kw: dict = {"quality": 50, "keep": True, "force": False, "metadata": False, "times": False}
        kw.update(opt_kwargs)
        return heic2jpg.convert_one(src, heic2jpg.Options(**kw), out=out)

    def test_ok_status_and_creates_jpg(self, single_heic):
        res = self._call(single_heic)
        assert res.status is heic2jpg.Status.OK
        assert res.error is None
        assert res.warnings == []
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_returns_input_path(self, single_heic):
        assert self._call(single_heic).src == single_heic

    def test_skip_when_jpg_exists_no_force(self, single_heic):
        jpg = single_heic.with_suffix(".jpg")
        jpg.write_bytes(b"existing")
        res = self._call(single_heic, force=False)
        assert res.status is heic2jpg.Status.SKIP
        assert jpg.read_bytes() == b"existing"

    def test_force_overwrites_existing_jpg(self, single_heic):
        single_heic.with_suffix(".jpg").write_bytes(b"stale")
        res = self._call(single_heic, force=True)
        assert res.status is heic2jpg.Status.OK
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_force_never_overwrites_diverted_name(self, single_heic):
        """force consents to overwriting the natural name only; a diverted
        output that appeared after planning is skipped, not clobbered."""
        out = single_heic.with_name("single-1.jpg")
        out.write_bytes(b"bystander")
        res = self._call(single_heic, out=out, force=True)
        assert res.status is heic2jpg.Status.SKIP
        assert out.read_bytes() == b"bystander"

    def test_keep_false_deletes_original(self, single_heic):
        self._call(single_heic, keep=False)
        assert not single_heic.exists()

    def test_keep_true_preserves_original(self, single_heic):
        self._call(single_heic, keep=True)
        assert single_heic.exists()

    def test_delete_failure_returns_ok_with_warning(self, single_heic, mocker):
        mocker.patch("heic2jpg.heic2jpg.Path.unlink", side_effect=PermissionError("locked"))
        res = self._call(single_heic, keep=False)
        assert res.status is heic2jpg.Status.OK
        assert any("could not delete" in w for w in res.warnings)

    def test_fail_status_on_corrupt_source(self, tmp_path):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"not a valid heic file at all!")
        res = self._call(bad)
        assert res.status is heic2jpg.Status.FAIL
        assert res.error is not None

    def test_tmp_file_cleaned_up_on_failure(self, tmp_path):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 16)
        self._call(bad)
        assert list(tmp_path.glob("*.tmp.*")) == []

    def test_no_jpg_produced_on_failure(self, tmp_path):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 16)
        self._call(bad)
        assert not (tmp_path / "bad.jpg").exists()

    def test_no_tmp_files_left_after_success(self, single_heic):
        self._call(single_heic)
        assert list(single_heic.parent.glob("*.tmp.*")) == []

    def test_times_calls_utime(self, single_heic, mocker):
        mock_utime = mocker.patch("heic2jpg.heic2jpg.os.utime")
        self._call(single_heic, times=True)
        mock_utime.assert_called_once()

    def test_times_false_does_not_call_utime(self, single_heic, mocker):
        mock_utime = mocker.patch("heic2jpg.heic2jpg.os.utime")
        self._call(single_heic, times=False)
        mock_utime.assert_not_called()

    def test_times_utime_oserror_returns_ok_with_warning(self, single_heic, mocker):
        mocker.patch("heic2jpg.heic2jpg.os.utime", side_effect=OSError("denied"))
        res = self._call(single_heic, times=True)
        assert res.status is heic2jpg.Status.OK
        assert any("could not restore timestamps" in w for w in res.warnings)

    def test_times_stat_oserror_skips_timestamps(self, single_heic, mocker):
        """If stat() raises on the source, src_stat stays None and timestamps
        are silently skipped — conversion still succeeds."""
        mocker.patch("heic2jpg.heic2jpg.os.utime")
        real_stat = Path.stat

        def failing_stat(self_, *a, **kw):
            if self_ == single_heic:
                raise OSError("permission denied")
            return real_stat(self_, *a, **kw)

        mocker.patch("heic2jpg.heic2jpg.Path.stat", failing_stat)
        res = self._call(single_heic, times=True)
        assert res.status is heic2jpg.Status.OK
        assert res.warnings == []

    def test_times_birthtime_float_fallback(self, single_heic, mocker):
        """Hits the st_birthtime float→ns branch."""
        mock_stat = MagicMock()
        mock_stat.st_birthtime_ns = None
        mock_stat.st_birthtime = 1_700_000_000.5
        mock_stat.st_atime_ns = 1_700_000_000_000_000_000
        mock_stat.st_mtime_ns = 1_700_000_000_000_000_000

        real_stat = Path.stat

        def selective_stat(self_, *a, **kw):
            if self_ == single_heic:
                return mock_stat
            return real_stat(self_, *a, **kw)

        mock_utime = mocker.patch("heic2jpg.heic2jpg.os.utime")
        mocker.patch("heic2jpg.heic2jpg.Path.stat", selective_stat)
        res = self._call(single_heic, times=True)

        assert res.status is heic2jpg.Status.OK
        mock_utime.assert_called_once()
        expected_mtime_ns = int(1_700_000_000.5 * 1e9)
        call_args = mock_utime.call_args
        ns_pair = call_args.kwargs.get("ns") or call_args[1].get("ns") or call_args[0][1]
        assert ns_pair[1] == expected_mtime_ns

    def test_times_no_birthtime_falls_back_to_mtime(self, single_heic, mocker):
        """When st_birthtime is absent, mtime_ns is used (Linux path)."""
        mock_stat = MagicMock(spec=os.stat_result)
        # On macOS spec includes st_birthtime, so we must explicitly remove it
        # to simulate a Linux stat_result where the attribute doesn't exist.
        del mock_stat.st_birthtime
        del mock_stat.st_birthtime_ns
        mock_stat.st_atime_ns = 1_000_000_000_000_000_000
        mock_stat.st_mtime_ns = 2_000_000_000_000_000_000

        real_stat = Path.stat

        def selective_stat(self_, *a, **kw):
            if self_ == single_heic:
                return mock_stat
            return real_stat(self_, *a, **kw)

        mock_utime = mocker.patch("heic2jpg.heic2jpg.os.utime")
        mocker.patch("heic2jpg.heic2jpg.Path.stat", selective_stat)
        res = self._call(single_heic, times=True)

        assert res.status is heic2jpg.Status.OK
        mock_utime.assert_called_once()
        call_args = mock_utime.call_args
        ns_pair = call_args.kwargs.get("ns") or call_args[1].get("ns") or call_args[0][1]
        assert ns_pair[1] == 2_000_000_000_000_000_000  # fell back to mtime_ns

    def test_tmp_unlink_oserror_suppressed(self, single_heic, mocker):
        """OSError during tmp.unlink() in the except block is suppressed.
        Patch Path.replace to fail after convert_with_pillow has written the
        tmp file, so the cleanup path runs."""
        mocker.patch("heic2jpg.heic2jpg.Path.replace", side_effect=OSError("replace failed"))
        res = self._call(single_heic)
        assert res.status is heic2jpg.Status.FAIL

    def test_idempotent_with_force(self, single_heic):
        self._call(single_heic, force=True)
        self._call(single_heic, force=True)
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_metadata_true_does_not_crash(self, single_heic):
        assert self._call(single_heic, metadata=True).status is heic2jpg.Status.OK

    def test_explicit_out_path_used(self, single_heic, tmp_path):
        out = tmp_path / "custom-1.jpg"
        res = self._call(single_heic, out=out)
        assert res.status is heic2jpg.Status.OK
        assert is_valid_jpeg(out)
        assert not single_heic.with_suffix(".jpg").exists()

    def test_keyboard_interrupt_cleans_tmp_and_propagates(self, single_heic, mocker):
        """Ctrl-C mid-conversion must not leave a .tmp file behind."""

        def fake_convert(src, tmp, quality, metadata):
            tmp.write_bytes(b"partial")
            raise KeyboardInterrupt

        mocker.patch("heic2jpg.heic2jpg.convert_with_pillow", side_effect=fake_convert)
        with pytest.raises(KeyboardInterrupt):
            self._call(single_heic)
        assert list(single_heic.parent.glob("*.tmp.*")) == []


# ===========================================================================
# parse_args
# ===========================================================================


class TestParseArgs:
    def test_defaults(self):
        args = heic2jpg.parse_args(["."])
        assert args.quality == 30
        assert args.metadata is False
        assert args.times is False
        assert args.keep is False
        assert args.recursive is False
        assert args.force is False
        assert args.verbose is False

    def test_all_flags(self, tmp_path):
        args = heic2jpg.parse_args(["-q", "60", "-m", "-t", "-k", "-R", "-f", "-v", str(tmp_path)])
        assert args.quality == 60
        assert args.path == str(tmp_path)
        assert all(getattr(args, f) for f in ("metadata", "times", "keep", "recursive", "force", "verbose"))

    def test_version_exits(self):
        with pytest.raises(SystemExit):
            heic2jpg.parse_args(["--version"])

    def test_default_path_is_dot(self):
        assert heic2jpg.parse_args([]).path == "."


# ===========================================================================
# main
# ===========================================================================


class TestMain:
    def test_quality_zero_returns_1(self, capsys):
        assert heic2jpg.main(["-q", "0", "."]) == 1

    def test_quality_101_returns_1(self, capsys):
        assert heic2jpg.main(["-q", "101", "."]) == 1

    def test_nonexistent_path_returns_1(self, tmp_path, capsys):
        assert heic2jpg.main([str(tmp_path / "no_such_dir")]) == 1
        assert "does not exist" in capsys.readouterr().err

    def test_non_heic_file_returns_1(self, tmp_path, capsys):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG")
        assert heic2jpg.main([str(f)]) == 1
        assert "not a HEIC" in capsys.readouterr().err

    def test_neither_file_nor_dir_returns_1(self, tmp_path, capsys):
        """Hits the 'not a file or directory' branch via mocking."""
        with (
            patch("heic2jpg.heic2jpg.Path.exists", return_value=True),
            patch("heic2jpg.heic2jpg.Path.is_file", return_value=False),
            patch("heic2jpg.heic2jpg.Path.is_dir", return_value=False),
        ):
            rc = heic2jpg.main([str(tmp_path / "weird")])
        assert rc == 1
        assert "not a file or directory" in capsys.readouterr().err

    def test_no_heic_files_returns_2(self, tmp_path, capsys):
        assert heic2jpg.main([str(tmp_path)]) == 2

    def test_no_codec_returns_1(self, tmp_path, capsys):
        with patch.object(heic2jpg, "_try_pillow_heif", return_value=False):
            assert heic2jpg.main([str(tmp_path)]) == 1
        assert "codec" in capsys.readouterr().err.lower()

    def test_converts_single_file_returns_0(self, single_heic):
        assert heic2jpg.main([str(single_heic)]) == 0
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_converts_directory_returns_0(self, heic_dir):
        assert heic2jpg.main([str(heic_dir)]) == 0
        assert len(list(heic_dir.glob("*.jpg"))) == 3

    def test_recursive(self, nested_heic_dir):
        assert heic2jpg.main(["-R", str(nested_heic_dir)]) == 0
        assert len(list(nested_heic_dir.rglob("*.jpg"))) == 4

    def test_verbose_output(self, single_heic, capsys):
        heic2jpg.main(["-v", "-k", str(single_heic)])
        assert "Done" in capsys.readouterr().err

    def test_existing_jpg_diverts_instead_of_skipping(self, single_heic, capsys):
        """The taken name is preserved, the HEIC still converts (to a
        numbered variant), and the diversion is announced without -v."""
        jpg = single_heic.with_suffix(".jpg")
        jpg.write_bytes(b"original")
        assert heic2jpg.main(["-k", str(single_heic)]) == 0
        assert jpg.read_bytes() == b"original"
        assert is_valid_jpeg(single_heic.with_name("single-1.jpg"))
        err = capsys.readouterr().err
        assert "warning" in err
        assert "single-1.jpg" in err

    def test_force_overwrites(self, single_heic):
        single_heic.with_suffix(".jpg").write_bytes(b"stale")
        heic2jpg.main(["-f", "-k", str(single_heic)])
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_failed_conversion_returns_3(self, tmp_path, capsys):
        bad = tmp_path / "corrupt.heic"
        bad.write_bytes(b"\x00" * 32)
        assert heic2jpg.main([str(bad)]) == 3
        assert "FAIL" in capsys.readouterr().err

    def test_quality_boundaries(self, single_heic):
        assert heic2jpg.main(["-q", "1", "-k", str(single_heic)]) == 0
        assert heic2jpg.main(["-q", "100", "-k", str(single_heic), "-f"]) == 0

    def test_single_file_goes_through_pool(self, tmp_path, mocker):
        """There is one execution path: even a single file runs via run_pool."""
        make_heic(tmp_path / "only.heic")
        mock_pool = mocker.patch("heic2jpg.heic2jpg.run_pool", return_value=(1, 0, 0, False))
        assert heic2jpg.main(["-k", str(tmp_path)]) == 0
        mock_pool.assert_called_once()

    def test_verbose_prints_fail_summary(self, tmp_path, capsys):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 32)
        heic2jpg.main(["-v", str(bad)])
        assert "failed=1" in capsys.readouterr().err

    def test_keyboard_interrupt_returns_130(self, single_heic, mocker, capsys):
        """Ctrl-C mid-run: graceful message, exit code 130."""
        mocker.patch("heic2jpg.heic2jpg.convert_one", side_effect=KeyboardInterrupt)
        rc = heic2jpg.main(["-k", str(single_heic)])
        assert rc == 130
        assert "Interrupted" in capsys.readouterr().err

    def test_pool_interrupt_returns_130(self, heic_dir, mocker):
        mocker.patch("heic2jpg.heic2jpg.run_pool", return_value=(0, 0, 0, True))
        assert heic2jpg.main(["-k", str(heic_dir)]) == 130


# ===========================================================================
# run_pool
# ===========================================================================


class TestRunPool:
    def _pool(self, files, jobs=2, verbose=False, **opt_kwargs):
        kw: dict = {"quality": 50, "keep": True, "force": False, "metadata": False, "times": False}
        kw.update(opt_kwargs)
        return heic2jpg.run_pool(files, heic2jpg.plan_outputs(files), jobs, heic2jpg.Options(**kw), verbose)

    def test_all_ok(self, heic_dir):
        files = heic2jpg.collect_files(heic_dir)
        ok, skipped, failed, interrupted = self._pool(files)
        assert ok == 3
        assert skipped == 0
        assert failed == 0
        assert interrupted is False

    def test_existing_output_diverts_and_converts(self, heic_dir):
        files = heic2jpg.collect_files(heic_dir)
        blocked = files[0].with_suffix(".jpg")
        blocked.write_bytes(b"x")
        ok, skipped, _, _ = self._pool(files)
        assert ok == 3
        assert skipped == 0
        assert blocked.read_bytes() == b"x"
        assert is_valid_jpeg(files[0].with_name(f"{files[0].stem}-1.jpg"))

    def test_skip_counted_when_output_appears_after_planning(self, single_heic):
        """convert_one's exists() backstop: outs planned before the file
        existed still yield SKIP rather than an overwrite."""
        out = single_heic.with_suffix(".jpg")
        out.write_bytes(b"x")
        opts = heic2jpg.Options(keep=True)
        ok, skipped, failed, _ = heic2jpg.run_pool([single_heic], [out], 1, opts, verbose=False)
        assert (ok, skipped, failed) == (0, 1, 0)

    def test_failed_counted(self, tmp_path):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 32)
        _, _, failed, _ = self._pool([bad])
        assert failed == 1

    def test_verbose_ok(self, single_heic, capsys):
        self._pool([single_heic], verbose=True)
        assert "ok:" in capsys.readouterr().err

    def test_warning_printed_without_verbose(self, single_heic, mocker, capsys):
        """Non-fatal warnings (e.g. undeletable original) surface without -v."""
        mocker.patch("heic2jpg.heic2jpg.Path.unlink", side_effect=PermissionError("locked"))
        self._pool([single_heic], verbose=False, keep=False)
        err = capsys.readouterr().err
        assert "warning:" in err
        assert "could not delete" in err

    def test_verbose_skip(self, single_heic, capsys):
        """Explicit outs bypass plan_outputs' diversion to hit the skip branch."""
        out = single_heic.with_suffix(".jpg")
        out.write_bytes(b"x")
        heic2jpg.run_pool([single_heic], [out], 1, heic2jpg.Options(keep=True), verbose=True)
        assert "skip:" in capsys.readouterr().err

    def test_failure_always_printed(self, tmp_path, capsys):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 32)
        self._pool([bad], verbose=False)
        assert "FAIL" in capsys.readouterr().err

    def test_empty_list(self):
        ok, skipped, failed, interrupted = self._pool([])
        assert ok == skipped == failed == 0
        assert interrupted is False

    def test_stress_20_files(self, tmp_path):
        files = [make_heic(tmp_path / f"img{i:02d}.heic") for i in range(20)]
        ok, _, failed, _ = self._pool(files, jobs=4)
        assert ok == 20
        assert failed == 0

    def test_keyboard_interrupt_cancels_pool(self, heic_dir, capsys):
        """KeyboardInterrupt inside as_completed cancels the queue, prints
        'Interrupted.', and the counts still reflect conversions that finished
        during the shutdown drain (the summary must match the disk)."""
        files = heic2jpg.collect_files(heic_dir)

        def raise_keyboard_interrupt(fs):
            raise KeyboardInterrupt

        with patch("heic2jpg.heic2jpg.as_completed", side_effect=raise_keyboard_interrupt):
            ok, skipped, failed, interrupted = self._pool(files)

        assert interrupted is True
        assert ok == len(list(heic_dir.glob("*.jpg")))
        assert skipped == failed == 0
        assert "Interrupted" in capsys.readouterr().err

    def test_sigint_handler_untouched_by_pool(self, heic_dir):
        """run_pool relies on default SIGINT behaviour and must not install its own handler."""
        prev = signal.getsignal(signal.SIGINT)
        self._pool(heic2jpg.collect_files(heic_dir))
        assert signal.getsignal(signal.SIGINT) is prev

    @pytest.mark.skipif(sys.platform == "win32", reason="os.kill(SIGINT) not supported on Windows")
    def test_real_sigint_interrupts_pool(self, heic_dir, capsys):
        """Fire SIGINT mid-pool: the resulting KeyboardInterrupt is caught and 'Interrupted.' printed."""
        files = heic2jpg.collect_files(heic_dir)
        original_as_completed = heic2jpg.as_completed

        def sigint_then_complete(fs):
            os.kill(os.getpid(), signal.SIGINT)
            yield from original_as_completed(fs)

        with patch("heic2jpg.heic2jpg.as_completed", side_effect=sigint_then_complete):
            self._pool(files)

        assert "Interrupted" in capsys.readouterr().err


# ===========================================================================
# Helpers and module-level constants
# ===========================================================================


class TestHelpers:
    @pytest.fixture(autouse=True)
    def _fresh_codec_cache(self):
        """_try_pillow_heif is cached; clear it so each test exercises the real body."""
        heic2jpg._try_pillow_heif.cache_clear()
        yield
        heic2jpg._try_pillow_heif.cache_clear()

    def test_try_pillow_heif_returns_true(self, mocker):
        mocker.patch("heic2jpg.heic2jpg.pillow_heif.register_heif_opener")
        assert heic2jpg._try_pillow_heif() is True

    def test_try_pillow_heif_returns_false_on_oserror(self, mocker):
        mocker.patch("heic2jpg.heic2jpg.pillow_heif.register_heif_opener", side_effect=OSError("no codec"))
        assert heic2jpg._try_pillow_heif() is False

    def test_try_pillow_heif_registers_only_once(self, mocker):
        mock_register = mocker.patch("heic2jpg.heic2jpg.pillow_heif.register_heif_opener")
        heic2jpg._try_pillow_heif()
        heic2jpg._try_pillow_heif()
        mock_register.assert_called_once()

    def test_version_is_semver(self):
        assert re.match(r"^\d+\.\d+\.\d+", heic2jpg.__version__), (
            f"__version__ {heic2jpg.__version__!r} does not start with MAJOR.MINOR.PATCH"
        )


# ===========================================================================
# Arbitrary byte payloads  →  convert_one never crashes the process
# ===========================================================================


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
class TestFuzzCorruptPayloads:
    """
    Property: convert_one must NEVER raise an unhandled exception, regardless
    of what bytes are in the source file.  It may return Status.FAIL, but it
    must return a well-formed Result.

    Corrupt bytes can produce a header declaring a huge pixel count, which
    trips Pillow's DecompressionBombWarning — expected noise here, so it's
    filtered.
    """

    @given(payload=strategies.binary(min_size=0, max_size=4096))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_arbitrary_bytes_never_raises(self, tmp_path, payload):
        src = tmp_path / "fuzz.heic"
        src.write_bytes(payload)
        # Ensure no leftover jpg from a previous example
        jpg = src.with_suffix(".jpg")
        if jpg.exists():
            jpg.unlink()

        result = heic2jpg.convert_one(src, heic2jpg.Options(quality=50, keep=True, force=True))

        assert isinstance(result, heic2jpg.Result)
        assert result.src == src
        assert isinstance(result.status, heic2jpg.Status)
        if result.status is heic2jpg.Status.FAIL:
            assert isinstance(result.error, str)
            assert result.error
        # No stale tmp files
        assert list(tmp_path.glob("*.tmp.*")) == []

    @given(payload=strategies.binary(min_size=0, max_size=4096))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_arbitrary_bytes_with_metadata_never_raises(self, tmp_path, payload):
        """metadata=True exercises extra EXIF paths; still must not crash."""
        src = tmp_path / "fuzz_meta.heic"
        src.write_bytes(payload)
        jpg = src.with_suffix(".jpg")
        if jpg.exists():
            jpg.unlink()

        result = heic2jpg.convert_one(src, heic2jpg.Options(quality=75, keep=True, force=True, metadata=True))
        assert isinstance(result.status, heic2jpg.Status)

    @given(
        payload=strategies.binary(min_size=0, max_size=512),
        quality=strategies.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_arbitrary_bytes_all_quality_levels(self, tmp_path, payload, quality):
        """Quality parameter must not change the no-crash guarantee."""
        src = tmp_path / "fuzz_q.heic"
        src.write_bytes(payload)
        jpg = src.with_suffix(".jpg")
        if jpg.exists():
            jpg.unlink()

        result = heic2jpg.convert_one(src, heic2jpg.Options(quality=quality, keep=True, force=True))
        assert isinstance(result.status, heic2jpg.Status)


# ===========================================================================
# Valid HEIC + mutated single bytes  →  convert_with_pillow
# ===========================================================================


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
class TestFuzzBitFlips:
    """
    Start from a real HEIC file and flip one byte at a random position.
    The converter must still return without raising (corrupted files should
    produce a fail status from convert_one, not an unhandled exception from
    convert_with_pillow propagating all the way up).

    A flipped dimension byte can inflate the declared pixel count, tripping
    Pillow's DecompressionBombWarning — expected noise here, so it's filtered.
    """

    @given(
        offset=strategies.integers(min_value=0, max_value=len(VALID_HEIC_BYTES) - 1),
        replacement=strategies.integers(min_value=0, max_value=255),
    )
    @settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_single_byte_mutation_handled(self, tmp_path, offset, replacement):
        mutated = bytearray(VALID_HEIC_BYTES)
        mutated[offset] = replacement
        src = tmp_path / "mutated.heic"
        src.write_bytes(bytes(mutated))
        jpg = src.with_suffix(".jpg")
        if jpg.exists():
            jpg.unlink()

        result = heic2jpg.convert_one(src, heic2jpg.Options(quality=50, keep=True, force=True))
        assert isinstance(result.status, heic2jpg.Status)


# ===========================================================================
# Quality boundary / out-of-range values  →  parse_args + main
# ===========================================================================


class TestFuzzQualityValues:
    """
    Property: quality values outside [1, 100] must cause main() to return 1
    without raising.  Values inside the range must not trigger that error.
    """

    @given(quality=strategies.integers(min_value=101, max_value=10_000))
    def test_quality_above_100_returns_1(self, quality):
        rc = heic2jpg.main(["-q", str(quality), "."])
        assert rc == 1

    @given(quality=strategies.integers(min_value=-10_000, max_value=0))
    def test_quality_zero_or_below_returns_1(self, quality):
        rc = heic2jpg.main(["-q", str(quality), "."])
        assert rc == 1

    @given(quality=strategies.integers(min_value=1, max_value=100))
    def test_valid_quality_does_not_return_1_for_quality_reason(self, quality):
        """
        A valid quality value should never produce rc=1 due to quality
        validation.  (It may still return 2 if no HEIC files exist — that's
        fine; we just confirm it's not the quality-error path.)
        Uses tempfile rather than tmp_path because hypothesis reuses the
        fixture across examples — each example needs its own clean directory.
        """
        with tempfile.TemporaryDirectory() as d:
            rc = heic2jpg.main(["-q", str(quality), d])
        assert rc in {0, 2, 3}

    @given(quality=strategies.integers(min_value=1, max_value=100))
    def test_parse_args_accepts_valid_quality(self, quality):
        args = heic2jpg.parse_args(["-q", str(quality), "."])
        assert args.quality == quality


# ===========================================================================
# Exotic filenames  →  collect_files
# ===========================================================================

# Characters that are valid in POSIX filenames (anything except NUL and /).
_POSIX_FILENAME_CHARS = strategies.text(
    alphabet=strategies.characters(
        blacklist_categories=("Cs",),  # no surrogates
        blacklist_characters="\x00/",
    ),
    min_size=1,
    max_size=80,
)


class TestFuzzCollectFiles:
    """
    Property: collect_files must never raise regardless of what filenames
    exist in the directory, and must only return paths whose suffix
    (case-insensitively) is ".heic".
    """

    @given(names=strategies.lists(_POSIX_FILENAME_CHARS, min_size=0, max_size=20))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_only_returns_heic_extensions(self, tmp_path, names):
        created = []
        for name in names:
            # Strip path separators that may have slipped through on some OSes
            safe = name.replace("/", "_").replace("\x00", "_")
            if not safe:
                continue
            try:
                p = tmp_path / safe
                p.write_bytes(b"")
                created.append(p)
            except (OSError, ValueError):
                # Some generated names are invalid on this OS — skip them
                pass

        result = heic2jpg.collect_files(tmp_path, recursive=False)
        for p in result:
            assert p.suffix.lower() == ".heic", f"Unexpected file: {p}"

    @given(names=strategies.lists(_POSIX_FILENAME_CHARS, min_size=0, max_size=10))
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_result_is_always_sorted(self, tmp_path, names):
        for name in names:
            safe = name.replace("/", "_").replace("\x00", "_")
            if not safe:
                continue
            with contextlib.suppress(OSError, ValueError):
                (tmp_path / (safe + ".heic")).write_bytes(b"")

        result = heic2jpg.collect_files(tmp_path, recursive=False)
        assert result == sorted(result)

    @given(
        depth=strategies.integers(min_value=0, max_value=5),
        files_per_level=strategies.integers(min_value=0, max_value=3),
    )
    @settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_recursive_vs_non_recursive_count(self, tmp_path, depth, files_per_level):
        """Recursive result must be >= non-recursive result."""
        # Build a tree of the given depth
        current = tmp_path
        for level in range(depth + 1):
            for i in range(files_per_level):
                (current / f"img_{level}_{i}.heic").write_bytes(b"")
            if level < depth:
                current = current / f"sub_{level}"
                current.mkdir(exist_ok=True)

        flat = heic2jpg.collect_files(tmp_path, recursive=False)
        deep = heic2jpg.collect_files(tmp_path, recursive=True)
        assert len(deep) >= len(flat)


# ===========================================================================
# _set_creation_time_windows  →  Windows creation-time helper
# ===========================================================================


class TestSetCreationTimeWindows:
    """
    Example-based and property-based coverage for the Windows creation-time
    helper.  On non-Windows the function is a no-op; on Windows we mock
    kernel32 to exercise the arithmetic, struct-packing, and error paths.
    """

    @pytest.mark.skipif(sys.platform == "win32", reason="non-Windows code path only")
    def test_noop_on_non_windows(self, tmp_path):
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        assert heic2jpg._set_creation_time_windows(f, 0) is None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_sets_ctime(self, tmp_path):
        """SetFileTime runs without error and the file remains intact."""
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        heic2jpg._set_creation_time_windows(f, int(time.time() * 1e9))
        assert f.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_invalid_handle_raises(self, tmp_path, mocker):
        """CreateFileW returning INVALID_HANDLE_VALUE must raise OSError."""
        mock_kernel = mocker.MagicMock()
        mock_kernel.CreateFileW.return_value = ctypes.wintypes.HANDLE(-1).value
        mock_kernel.GetLastError.return_value = 5  # ERROR_ACCESS_DENIED
        mocker.patch("ctypes.WinDLL", return_value=mock_kernel)
        f = tmp_path / "x.txt"
        f.write_bytes(b"y")
        with pytest.raises(OSError, match="CreateFileW failed"):
            heic2jpg._set_creation_time_windows(f, 0)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_setfiletime_failure_raises(self, tmp_path, mocker):
        """SetFileTime returning 0 must raise OSError."""
        mock_kernel = mocker.MagicMock()
        mock_kernel.CreateFileW.return_value = 999  # plausible valid handle
        mock_kernel.SetFileTime.return_value = 0  # failure
        mock_kernel.GetLastError.return_value = 5
        mocker.patch("ctypes.WinDLL", return_value=mock_kernel)
        f = tmp_path / "x.txt"
        f.write_bytes(b"y")
        with pytest.raises(OSError, match="SetFileTime failed"):
            heic2jpg._set_creation_time_windows(f, 0)

    @given(ctime_ns=strategies.integers(min_value=0, max_value=2**63 - 1))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows mocked path")
    def test_windows_filetime_arithmetic_never_raises(self, tmp_path, ctime_ns):
        """
        Mock kernel32 so we can exercise the epoch-conversion arithmetic
        and ctypes struct packing for arbitrary nanosecond timestamps.
        """
        mock_kernel = MagicMock()
        mock_kernel.CreateFileW.return_value = 999  # fake valid handle
        mock_kernel.SetFileTime.return_value = 1  # success
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        with patch("ctypes.WinDLL", return_value=mock_kernel):
            heic2jpg._set_creation_time_windows(f, ctime_ns)
        mock_kernel.CloseHandle.assert_called_once_with(999)

    @given(ctime_ns=strategies.integers())
    @settings(max_examples=500, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.skipif(sys.platform == "win32", reason="non-Windows no-op path")
    def test_noop_on_non_windows_never_raises(self, tmp_path, ctime_ns):
        """Any integer timestamp — positive, negative, or zero — is a no-op on non-Windows."""
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        assert heic2jpg._set_creation_time_windows(f, ctime_ns) is None


# ===========================================================================
# Arbitrary CLI argument lists  →  parse_args robustness
# ===========================================================================


class TestFuzzParseArgs:
    """
    parse_args wraps argparse, which may call sys.exit() on truly invalid
    input.  The property here is: for any set of *valid* boolean flags plus
    an in-range quality, parse_args must succeed and round-trip the quality.
    """

    @given(
        quality=strategies.integers(min_value=1, max_value=100),
        metadata=strategies.booleans(),
        times=strategies.booleans(),
        keep=strategies.booleans(),
        recursive=strategies.booleans(),
        force=strategies.booleans(),
        verbose=strategies.booleans(),
    )
    @settings(max_examples=200)
    def test_all_flag_combinations_parse_without_error(self, quality, metadata, times, keep, recursive, force, verbose):  # noqa: PLR0913
        argv = ["-q", str(quality)]
        if metadata:
            argv.append("-m")
        if times:
            argv.append("-t")
        if keep:
            argv.append("-k")
        if recursive:
            argv.append("-R")
        if force:
            argv.append("-f")
        if verbose:
            argv.append("-v")
        argv.append(".")

        args = heic2jpg.parse_args(argv)
        assert args.quality == quality
        assert args.metadata == metadata
        assert args.times == times
        assert args.keep == keep
        assert args.recursive == recursive
        assert args.force == force
        assert args.verbose == verbose

    @given(quality=strategies.integers(min_value=1, max_value=100))
    @settings(max_examples=100)
    def test_quality_round_trips(self, quality):
        args = heic2jpg.parse_args(["-q", str(quality), "."])
        assert args.quality == quality


# ===========================================================================
# Valid HEIC with random image parameters  →  convert_with_pillow
# ===========================================================================


class TestFuzzValidImages:
    """
    Property: a genuinely valid HEIC file (any reasonable size/color) must
    always produce a valid JPEG when convert_with_pillow is called.
    """

    @given(
        width=strategies.integers(min_value=1, max_value=256),
        height=strategies.integers(min_value=1, max_value=256),
        r=strategies.integers(min_value=0, max_value=255),
        g=strategies.integers(min_value=0, max_value=255),
        b=strategies.integers(min_value=0, max_value=255),
        quality=strategies.integers(min_value=1, max_value=100),
        metadata=strategies.booleans(),
    )
    @settings(
        max_examples=80,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    def test_valid_heic_always_produces_valid_jpeg(self, tmp_path, width, height, r, g, b, quality, metadata):  # noqa: PLR0913
        heic_bytes = _make_heic_bytes(size=(width, height), color=(r, g, b))
        src = tmp_path / "valid.heic"
        src.write_bytes(heic_bytes)
        out = tmp_path / "out.jpg"
        if out.exists():
            out.unlink()

        heic2jpg.convert_with_pillow(src, out, quality=quality, metadata=metadata)

        assert out.exists()
        with out.open("rb") as f:
            assert f.read(2) == b"\xff\xd8", "Output is not a valid JPEG"

    @given(
        width=strategies.integers(min_value=1, max_value=64),
        height=strategies.integers(min_value=1, max_value=64),
        alpha=strategies.integers(min_value=0, max_value=255),
    )
    @settings(
        max_examples=40,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    def test_rgba_heic_always_converts_to_rgb_jpeg(self, tmp_path, width, height, alpha):
        src = make_rgba_heic(tmp_path / "rgba.heic", size=(width, height), color=(200, 100, 50, alpha))
        out = tmp_path / "out.jpg"
        if out.exists():
            out.unlink()

        heic2jpg.convert_with_pillow(src, out, quality=50, metadata=False)

        with Image.open(out) as im:
            assert im.mode == "RGB"
