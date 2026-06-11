"""Tests for heic2jpg — collect_files, convert_with_pillow, convert_one, run_pool, main."""

import ctypes
import ctypes.wintypes
import io
import os
import signal
import struct
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pillow_heif
import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies
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
# convert_with_pillow
# ===========================================================================


class TestConvertWithPillow:
    def _mock_image(self, info=None, mode="RGB"):
        """Return a (mock_img, transposed) pair wired up for Image.open context."""
        mock_img = MagicMock()
        mock_img.__enter__ = MagicMock(return_value=mock_img)
        mock_img.__exit__ = MagicMock(return_value=False)
        mock_img.info = info if info is not None else {}
        mock_img.mode = mode
        transposed = MagicMock()
        transposed.mode = "RGB"
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
        """When im.info['exif'] is present it must be forwarded to save()."""
        fake_exif = b"Exif\x00\x00" + b"II" + struct.pack("<H", 42) + struct.pack("<I", 8) + struct.pack("<H", 0)
        mock_img, transposed = self._mock_image(info={"exif": fake_exif})
        with (
            patch("heic2jpg.heic2jpg.Image.open", return_value=mock_img),
            patch("heic2jpg.heic2jpg.ImageOps.exif_transpose", return_value=transposed),
        ):
            heic2jpg.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=True)
        assert transposed.save.call_args[1]["exif"] == fake_exif

    def test_metadata_true_getexif_fallback(self, tmp_path):
        """When im.info has no 'exif', fall back to im.getexif().tobytes()."""
        fake_exif = b"Exif\x00\x00II"
        mock_img, transposed = self._mock_image()
        mock_img.getexif.return_value.tobytes.return_value = fake_exif
        with (
            patch("heic2jpg.heic2jpg.Image.open", return_value=mock_img),
            patch("heic2jpg.heic2jpg.ImageOps.exif_transpose", return_value=transposed),
        ):
            heic2jpg.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=True)
        assert transposed.save.call_args[1]["exif"] == fake_exif

    def test_metadata_getexif_exception_skipped(self, tmp_path):
        """If getexif().tobytes() raises, exif is silently skipped."""
        mock_img, transposed = self._mock_image()
        mock_img.getexif.return_value.tobytes.side_effect = RuntimeError("no exif")
        with (
            patch("heic2jpg.heic2jpg.Image.open", return_value=mock_img),
            patch("heic2jpg.heic2jpg.ImageOps.exif_transpose", return_value=transposed),
        ):
            heic2jpg.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=True)
        assert "exif" not in transposed.save.call_args[1]

    def test_raises_on_nonexistent_source(self, tmp_path):
        with pytest.raises(Exception):
            heic2jpg.convert_with_pillow(tmp_path / "ghost.heic", tmp_path / "out.jpg", quality=85, metadata=False)


# ===========================================================================
# convert_one
# ===========================================================================


class TestConvertOne:
    def _call(self, src, **kwargs):
        kw = dict(quality=50, keep=True, force=False, metadata=False, times=False)
        kw.update(kwargs)
        return heic2jpg.convert_one(src, **kw)

    def test_ok_status_and_creates_jpg(self, single_heic):
        _, status, err = self._call(single_heic)
        assert status == "ok" and err is None
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_returns_input_path(self, single_heic):
        path, _, _ = self._call(single_heic)
        assert path == single_heic

    def test_skip_when_jpg_exists_no_force(self, single_heic):
        jpg = single_heic.with_suffix(".jpg")
        jpg.write_bytes(b"existing")
        _, status, _ = self._call(single_heic, force=False)
        assert status == "skip"
        assert jpg.read_bytes() == b"existing"

    def test_force_overwrites_existing_jpg(self, single_heic):
        single_heic.with_suffix(".jpg").write_bytes(b"stale")
        _, status, _ = self._call(single_heic, force=True)
        assert status == "ok"
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_keep_false_deletes_original(self, single_heic):
        self._call(single_heic, keep=False)
        assert not single_heic.exists()

    def test_keep_true_preserves_original(self, single_heic):
        self._call(single_heic, keep=True)
        assert single_heic.exists()

    def test_delete_failure_returns_ok_with_warning(self, single_heic, mocker):
        mocker.patch("heic2jpg.heic2jpg.Path.unlink", side_effect=PermissionError("locked"))
        _, status, err = self._call(single_heic, keep=False)
        assert status == "ok" and err is not None and "warning" in err

    def test_fail_status_on_corrupt_source(self, tmp_path):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"not a valid heic file at all!")
        _, status, err = self._call(bad)
        assert status == "fail" and err is not None

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
        _, status, err = self._call(single_heic, times=True)
        assert status == "ok" and err is not None and "warning" in err

    def test_times_stat_oserror_skips_timestamps(self, single_heic, mocker):
        """If stat() raises on the source, src_stat stays None and timestamps
        are silently skipped — conversion still succeeds (lines 239-240)."""
        mocker.patch("heic2jpg.heic2jpg.os.utime")
        real_stat = Path.stat
        def failing_stat(self_, *a, **kw):
            if self_ == single_heic:
                raise OSError("permission denied")
            return real_stat(self_, *a, **kw)
        mocker.patch("heic2jpg.heic2jpg.Path.stat", failing_stat)
        _, status, err = self._call(single_heic, times=True)
        assert status == "ok" and err is None

    def test_times_birthtime_float_fallback(self, single_heic, mocker):
        """Hits the st_birthtime float→ns branch."""
        mock_stat = MagicMock()
        mock_stat.st_birthtime_ns = None
        mock_stat.st_birthtime = 1_700_000_000.5
        mock_stat.st_atime_ns = 1_700_000_000_000_000_000
        mock_stat.st_mtime_ns = 1_700_000_000_000_000_000

        real_stat = os.stat

        def selective_stat(path, *a, **kw):
            if Path(path) == single_heic:
                return mock_stat
            return real_stat(path, *a, **kw)

        mock_utime = mocker.patch("heic2jpg.heic2jpg.os.utime")
        with patch("os.stat", side_effect=selective_stat):
            _, status, _ = self._call(single_heic, times=True)

        assert status == "ok"
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

        real_stat = os.stat

        def selective_stat(path, *a, **kw):
            if Path(path) == single_heic:
                return mock_stat
            return real_stat(path, *a, **kw)

        mock_utime = mocker.patch("heic2jpg.heic2jpg.os.utime")
        with patch("os.stat", side_effect=selective_stat):
            _, status, _ = self._call(single_heic, times=True)

        assert status == "ok"
        mock_utime.assert_called_once()
        call_args = mock_utime.call_args
        ns_pair = call_args.kwargs.get("ns") or call_args[1].get("ns") or call_args[0][1]
        assert ns_pair[1] == 2_000_000_000_000_000_000  # fell back to mtime_ns

    def test_tmp_unlink_oserror_suppressed(self, tmp_path, mocker):
        """OSError during tmp.unlink() in the except block is suppressed."""
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 16)
        mocker.patch("heic2jpg.heic2jpg.Path.unlink", side_effect=OSError("locked"))
        _, status, _ = self._call(bad)
        assert status == "fail"

    def test_tmp_unlink_oserror_suppressed(self, single_heic, mocker):
        """OSError during tmp.unlink() in the except block is suppressed
        (lines 299-302). Patch os.replace to fail after convert_with_pillow
        has written the tmp file, so tmp.exists() is True when cleanup runs."""
        mocker.patch("heic2jpg.heic2jpg.os.replace", side_effect=OSError("replace failed"))
        _, status, _ = self._call(single_heic)
        assert status == "fail"

    def test_idempotent_with_force(self, single_heic):
        self._call(single_heic, force=True)
        self._call(single_heic, force=True)
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_metadata_true_does_not_crash(self, single_heic):
        _, status, _ = self._call(single_heic, metadata=True)
        assert status == "ok"


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
        with patch.object(heic2jpg, "PILLOW_OK", False):
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

    def test_skip_without_force(self, single_heic):
        jpg = single_heic.with_suffix(".jpg")
        jpg.write_bytes(b"original")
        heic2jpg.main(["-k", str(single_heic)])
        assert jpg.read_bytes() == b"original"

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

    def test_serial_path_single_file(self, tmp_path, mocker):
        """One file → jobs==1 → serial branch, run_pool never called."""
        make_heic(tmp_path / "only.heic")
        mock_pool = mocker.patch("heic2jpg.heic2jpg.run_pool")
        heic2jpg.main(["-k", str(tmp_path)])
        mock_pool.assert_not_called()

    def test_verbose_prints_fail_summary(self, tmp_path, capsys):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 32)
        heic2jpg.main(["-v", str(bad)])
        assert "failed=1" in capsys.readouterr().err


# ===========================================================================
# run_pool
# ===========================================================================


class TestRunPool:
    def _pool(self, files, **kwargs):
        kw = dict(jobs=2, q=50, keep=True, force=False, metadata=False, times=False, verbose=False)
        kw.update(kwargs)
        return heic2jpg.run_pool(files, **kw)

    def test_all_ok(self, heic_dir):
        files = heic2jpg.collect_files(heic_dir)
        ok, skipped, failed = self._pool(files)
        assert ok == 3 and skipped == 0 and failed == 0

    def test_skip_counted(self, heic_dir):
        files = heic2jpg.collect_files(heic_dir)
        files[0].with_suffix(".jpg").write_bytes(b"x")
        ok, skipped, _ = self._pool(files)
        assert skipped == 1 and ok == 2

    def test_failed_counted(self, tmp_path):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 32)
        _, _, failed = self._pool([bad])
        assert failed == 1

    def test_verbose_ok(self, single_heic, capsys):
        self._pool([single_heic], verbose=True)
        assert "ok:" in capsys.readouterr().err

    def test_verbose_skip(self, single_heic, capsys):
        single_heic.with_suffix(".jpg").write_bytes(b"x")
        self._pool([single_heic], verbose=True, force=False)
        assert "skip:" in capsys.readouterr().err

    def test_failure_always_printed(self, tmp_path, capsys):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 32)
        self._pool([bad], verbose=False)
        assert "FAIL" in capsys.readouterr().err

    def test_empty_list(self):
        ok, skipped, failed = self._pool([])
        assert ok == skipped == failed == 0

    def test_stress_20_files(self, tmp_path):
        files = [make_heic(tmp_path / f"img{i:02d}.heic") for i in range(20)]
        ok, _, failed = self._pool(files, jobs=4)
        assert ok == 20 and failed == 0

    def test_keyboard_interrupt_cancels_pool(self, heic_dir, capsys):
        """KeyboardInterrupt inside as_completed triggers cancel and prints 'Interrupted.'"""
        files = heic2jpg.collect_files(heic_dir)

        def raise_keyboard_interrupt(fs):
            raise KeyboardInterrupt

        with patch("heic2jpg.heic2jpg.as_completed", side_effect=raise_keyboard_interrupt):
            ok, skipped, failed = self._pool(files)

        assert ok + skipped + failed == 0
        assert "Interrupted" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform == "win32", reason="os.kill(SIGINT) not supported on Windows")
    def test_sigint_handler_sets_interrupted(self, heic_dir, capsys):
        """Fire SIGINT mid-pool and verify 'Interrupted.' is printed."""
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
    def test_try_pillow_heif_returns_true(self, mocker):
        mocker.patch("heic2jpg.heic2jpg.pillow_heif.register_heif_opener")
        assert heic2jpg._try_pillow_heif() is True

    def test_try_pillow_heif_returns_false_on_oserror(self, mocker):
        mocker.patch("heic2jpg.heic2jpg.pillow_heif.register_heif_opener", side_effect=OSError("no codec"))
        assert heic2jpg._try_pillow_heif() is False

    def test_pillow_ok_is_bool(self):
        assert isinstance(heic2jpg.PILLOW_OK, bool)

    def test_version_is_semver(self):
        import re
        assert re.match(r"^\d+\.\d+\.\d+", heic2jpg.__version__), (
            f"__version__ {heic2jpg.__version__!r} does not start with MAJOR.MINOR.PATCH"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="non-Windows code path only")
    def test_set_creation_time_windows_noop_on_non_windows(self, tmp_path):
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        assert heic2jpg._set_creation_time_windows(f, 0) is None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_set_creation_time_windows_sets_ctime(self, tmp_path):
        """SetFileTime runs without error and the file remains intact."""
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        heic2jpg._set_creation_time_windows(f, int(time.time() * 1e9))
        assert f.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_set_creation_time_windows_invalid_handle_raises(self, tmp_path, mocker):
        """CreateFileW returning INVALID_HANDLE_VALUE must raise OSError."""
        mock_kernel = mocker.MagicMock()
        mock_kernel.CreateFileW.return_value = ctypes.wintypes.HANDLE(-1).value
        mock_kernel.GetLastError.return_value = 5  # ERROR_ACCESS_DENIED
        mocker.patch("ctypes.WinDLL", return_value=mock_kernel)
        f = tmp_path / "x.txt"
        f.write_bytes(b"y")
        with pytest.raises(OSError):
            heic2jpg._set_creation_time_windows(f, 0)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_set_creation_time_windows_setfiletime_failure_raises(self, tmp_path, mocker):
        """SetFileTime returning 0 must raise OSError."""
        mock_kernel = mocker.MagicMock()
        mock_kernel.CreateFileW.return_value = 999  # plausible valid handle
        mock_kernel.SetFileTime.return_value = 0  # failure
        mock_kernel.GetLastError.return_value = 5
        mocker.patch("ctypes.WinDLL", return_value=mock_kernel)
        f = tmp_path / "x.txt"
        f.write_bytes(b"y")
        with pytest.raises(OSError):
            heic2jpg._set_creation_time_windows(f, 0)

# ===========================================================================
# Arbitrary byte payloads  →  convert_one never crashes the process
# ===========================================================================

class TestFuzzCorruptPayloads:
    """
    Property: convert_one must NEVER raise an unhandled exception, regardless
    of what bytes are in the source file.  It may return status="fail", but
    it must return a valid 3-tuple with status in {"ok", "skip", "fail"}.
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

        result = heic2jpg.convert_one(src, quality=50, keep=True, force=True, metadata=False, times=False)

        assert isinstance(result, tuple) and len(result) == 3
        path, status, err = result
        assert path == src
        assert status in {"ok", "skip", "fail"}
        if status == "fail":
            assert isinstance(err, str) and err
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

        result = heic2jpg.convert_one(src, quality=75, keep=True, force=True, metadata=True, times=False)
        _, status, _ = result
        assert status in {"ok", "skip", "fail"}

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

        result = heic2jpg.convert_one(src, quality=quality, keep=True, force=True, metadata=False, times=False)
        _, status, _ = result
        assert status in {"ok", "skip", "fail"}


# ===========================================================================
# Valid HEIC + mutated single bytes  →  convert_with_pillow
# ===========================================================================


class TestFuzzBitFlips:
    """
    Start from a real HEIC file and flip one byte at a random position.
    The converter must still return without raising (corrupted files should
    produce a fail status from convert_one, not an unhandled exception from
    convert_with_pillow propagating all the way up).
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

        result = heic2jpg.convert_one(src, quality=50, keep=True, force=True, metadata=False, times=False)
        _, status, _ = result
        assert status in {"ok", "skip", "fail"}


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
        import tempfile
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
        blacklist_categories=("Cs",),          # no surrogates
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
            try:
                (tmp_path / (safe + ".heic")).write_bytes(b"")
            except (OSError, ValueError):
                pass

        result = heic2jpg.collect_files(tmp_path, recursive=False)
        assert result == sorted(result)

    @given(depth=strategies.integers(min_value=0, max_value=5), files_per_level=strategies.integers(min_value=0, max_value=3))
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
# Extreme ctime_ns values  →  _set_creation_time_windows
# ===========================================================================


class TestFuzzCreationTime:
    """
    On non-Windows the function is a no-op; we verify it never raises for any
    integer input.  On Windows we'd need a real file handle, so we mock
    kernel32 to exercise the arithmetic and struct-packing logic.
    """

    @given(ctime_ns=strategies.integers())
    @settings(max_examples=500, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.skipif(sys.platform == "win32", reason="non-Windows no-op path")
    def test_noop_on_non_windows_never_raises(self, tmp_path, ctime_ns):
        """Any integer timestamp — positive, negative, or zero — is a no-op on non-Windows."""
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        assert heic2jpg._set_creation_time_windows(f, ctime_ns) is None

    @given(ctime_ns=strategies.integers(min_value=0, max_value=2**63 - 1))
    @settings(max_examples=200)
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows mocked path")
    def test_windows_filetime_arithmetic_never_raises(self, tmp_path, ctime_ns):
        """
        Mock kernel32 so we can exercise the epoch-conversion arithmetic
        and ctypes struct packing for arbitrary nanosecond timestamps.
        """
        mock_kernel = MagicMock()
        mock_kernel.CreateFileW.return_value = 999   # fake valid handle
        mock_kernel.SetFileTime.return_value = 1     # success
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        with patch("ctypes.WinDLL", return_value=mock_kernel):
            heic2jpg._set_creation_time_windows(f, ctime_ns)
        mock_kernel.CloseHandle.assert_called_once_with(999)


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
    def test_all_flag_combinations_parse_without_error(
        self, quality, metadata, times, keep, recursive, force, verbose
    ):
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
    def test_valid_heic_always_produces_valid_jpeg(
        self, tmp_path, width, height, r, g, b, quality, metadata
    ):
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
