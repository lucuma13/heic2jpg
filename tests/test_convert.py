"""Tests for heic2jpg.convert."""

import os
import re
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings, strategies
from PIL import Image, ImageCms

import heic2jpg
from heic2jpg import convert

from .helpers import VALID_HEIC_BYTES, is_valid_jpeg, make_heic_bytes, make_rgba_heic

# ---------------------------------------------------------------------------
# convert_with_pillow
# ---------------------------------------------------------------------------


class TestConvertWithPillow:
    def _mock_image(self, info=None, mode="RGB"):
        """
        Return a (mock_img, transposed) pair wired up for Image.open context.

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
        convert.convert_with_pillow(single_heic, out, quality=85, metadata=False)
        assert is_valid_jpeg(out)

    def test_output_is_rgb(self, single_heic, tmp_path):
        out = tmp_path / "out.jpg"
        convert.convert_with_pillow(single_heic, out, quality=85, metadata=False)
        with Image.open(out) as im:
            assert im.mode == "RGB"

    def test_rgba_source_converted_to_rgb(self, tmp_path):
        """RGBA input hits the im.mode != 'RGB' branch."""
        src = make_rgba_heic(tmp_path / "rgba.heic")
        out = tmp_path / "out.jpg"
        convert.convert_with_pillow(src, out, quality=85, metadata=False)
        assert is_valid_jpeg(out)
        with Image.open(out) as im:
            assert im.mode == "RGB"

    def test_quality_affects_file_size(self, single_heic, tmp_path):
        lo, hi = tmp_path / "lo.jpg", tmp_path / "hi.jpg"
        convert.convert_with_pillow(single_heic, lo, quality=1, metadata=False)
        convert.convert_with_pillow(single_heic, hi, quality=95, metadata=False)
        assert lo.stat().st_size < hi.stat().st_size

    def test_output_dimensions_match_source(self, single_heic, tmp_path):
        out = tmp_path / "out.jpg"
        convert.convert_with_pillow(single_heic, out, quality=85, metadata=False)
        with Image.open(single_heic) as src_im, Image.open(out) as out_im:
            assert src_im.size == out_im.size

    def test_metadata_true_embeds_exif(self, tmp_path):
        """The transposed image's info['exif'] must be forwarded to save()."""
        fake_exif = b"Exif\x00\x00" + b"II" + struct.pack("<H", 42) + struct.pack("<I", 8) + struct.pack("<H", 0)
        mock_img, transposed = self._mock_image(info={"exif": fake_exif})
        with (
            patch("heic2jpg.convert.Image.open", return_value=mock_img),
            patch("heic2jpg.convert.ImageOps.exif_transpose", return_value=transposed),
        ):
            convert.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=True)
        assert transposed.save.call_args[1]["exif"] == fake_exif

    def test_metadata_true_getexif_fallback(self, tmp_path):
        """When info has no 'exif', fall back to the transposed getexif().tobytes()."""
        fake_exif = b"Exif\x00\x00II"
        mock_img, transposed = self._mock_image()
        transposed.getexif.return_value.tobytes.return_value = fake_exif
        with (
            patch("heic2jpg.convert.Image.open", return_value=mock_img),
            patch("heic2jpg.convert.ImageOps.exif_transpose", return_value=transposed),
        ):
            convert.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=True)
        assert transposed.save.call_args[1]["exif"] == fake_exif

    def test_metadata_getexif_exception_skipped(self, tmp_path):
        """If getexif().tobytes() raises, exif is silently skipped."""
        mock_img, transposed = self._mock_image()
        transposed.getexif.return_value.tobytes.side_effect = RuntimeError("no exif")
        with (
            patch("heic2jpg.convert.Image.open", return_value=mock_img),
            patch("heic2jpg.convert.ImageOps.exif_transpose", return_value=transposed),
        ):
            convert.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=True)
        assert "exif" not in transposed.save.call_args[1]

    def test_icc_profile_forwarded_even_without_metadata(self, tmp_path):
        """The source ICC profile is embedded regardless of the metadata flag."""
        fake_icc = b"\x00\x00\x02\x18appl" + b"fake profile bytes"
        mock_img, transposed = self._mock_image(info={"icc_profile": fake_icc})
        with (
            patch("heic2jpg.convert.Image.open", return_value=mock_img),
            patch("heic2jpg.convert.ImageOps.exif_transpose", return_value=transposed),
        ):
            convert.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=False)
        assert transposed.save.call_args[1]["icc_profile"] == fake_icc

    def test_no_icc_key_when_source_has_no_profile(self, tmp_path):
        """A source without a profile must not pass icc_profile to save()."""
        mock_img, transposed = self._mock_image(info={})
        with (
            patch("heic2jpg.convert.Image.open", return_value=mock_img),
            patch("heic2jpg.convert.ImageOps.exif_transpose", return_value=transposed),
        ):
            convert.convert_with_pillow(tmp_path / "x.heic", tmp_path / "out.jpg", quality=80, metadata=False)
        assert "icc_profile" not in transposed.save.call_args[1]

    def test_icc_profile_survives_real_roundtrip(self, tmp_path):
        """A profiled source yields a JPEG whose embedded profile round-trips."""
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        src = Image.new("RGB", (16, 16), (200, 30, 30))
        src.info["icc_profile"] = icc

        out = tmp_path / "out.jpg"
        with patch("heic2jpg.convert.Image.open", return_value=src):
            convert.convert_with_pillow(tmp_path / "x.heic", out, quality=90, metadata=False)

        with Image.open(out) as im:
            assert im.info.get("icc_profile") == icc

    def test_orientation_is_baked_in_and_stripped(self, tmp_path):
        """
        A source tagged Orientation=6 (90° CW) is physically rotated, and the
        Orientation tag is absent from the output so pixels aren't rotated
        twice.

        The HEIC encoder normalizes Orientation to 1, so the oriented source is
        built in memory and fed through the *real* exif_transpose.
        """
        src = Image.new("RGB", (40, 20), (100, 150, 200))
        exif = src.getexif()
        exif[0x0112] = 6  # Orientation tag
        src.info["exif"] = exif.tobytes()

        out = tmp_path / "out.jpg"
        with patch("heic2jpg.convert.Image.open", return_value=src):
            convert.convert_with_pillow(tmp_path / "x.heic", out, quality=90, metadata=True)

        with Image.open(out) as im:
            assert im.size == (20, 40)  # width/height swapped by the rotation
            assert im.getexif().get(0x0112) is None

    def test_raises_on_nonexistent_source(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            convert.convert_with_pillow(tmp_path / "ghost.heic", tmp_path / "out.jpg", quality=85, metadata=False)


# ---------------------------------------------------------------------------
# convert_one
# ---------------------------------------------------------------------------


class TestConvertOne:
    def _call(self, src, out=None, **opt_kwargs):
        kw: dict = {"quality": 50, "keep": True, "force": False, "metadata": False, "times": False}
        kw.update(opt_kwargs)
        return convert.convert_one(src, convert.Options(**kw), out=out)

    def test_ok_status_and_creates_jpg(self, single_heic):
        res = self._call(single_heic)
        assert res.status is convert.Status.OK
        assert res.error is None
        assert res.warnings == []
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_returns_input_path(self, single_heic):
        assert self._call(single_heic).src == single_heic

    def test_skip_when_jpg_exists_no_force(self, single_heic):
        jpg = single_heic.with_suffix(".jpg")
        jpg.write_bytes(b"existing")
        res = self._call(single_heic, force=False)
        assert res.status is convert.Status.SKIP
        assert jpg.read_bytes() == b"existing"

    def test_force_overwrites_existing_jpg(self, single_heic):
        single_heic.with_suffix(".jpg").write_bytes(b"stale")
        res = self._call(single_heic, force=True)
        assert res.status is convert.Status.OK
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_force_never_overwrites_diverted_name(self, single_heic):
        """
        force consents to overwriting the natural name only; a diverted output
        that appeared after planning is skipped, not clobbered.
        """
        out = single_heic.with_name("single-1.jpg")
        out.write_bytes(b"bystander")
        res = self._call(single_heic, out=out, force=True)
        assert res.status is convert.Status.SKIP
        assert out.read_bytes() == b"bystander"

    def test_keep_false_deletes_original(self, single_heic):
        self._call(single_heic, keep=False)
        assert not single_heic.exists()

    def test_keep_true_preserves_original(self, single_heic):
        self._call(single_heic, keep=True)
        assert single_heic.exists()

    def test_delete_failure_returns_ok_with_warning(self, single_heic, mocker):
        mocker.patch("heic2jpg.convert.Path.unlink", side_effect=PermissionError("locked"))
        res = self._call(single_heic, keep=False)
        assert res.status is convert.Status.OK
        assert any("could not delete" in w for w in res.warnings)

    def test_fail_status_on_corrupt_source(self, tmp_path):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"not a valid heic file at all!")
        res = self._call(bad)
        assert res.status is convert.Status.FAIL
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
        mock_utime = mocker.patch("heic2jpg.osutils.os.utime")
        self._call(single_heic, times=True)
        mock_utime.assert_called_once()

    def test_times_false_does_not_call_utime(self, single_heic, mocker):
        mock_utime = mocker.patch("heic2jpg.osutils.os.utime")
        self._call(single_heic, times=False)
        mock_utime.assert_not_called()

    def test_times_utime_oserror_returns_ok_with_warning(self, single_heic, mocker):
        mocker.patch("heic2jpg.osutils.os.utime", side_effect=OSError("denied"))
        res = self._call(single_heic, times=True)
        assert res.status is convert.Status.OK
        assert any("could not restore timestamps" in w for w in res.warnings)

    def test_times_stat_oserror_skips_timestamps(self, single_heic, mocker):
        """If stat() raises on the source, src_stat stays None and timestamps
        are silently skipped - conversion still succeeds."""
        mocker.patch("heic2jpg.osutils.os.utime")
        real_stat = Path.stat

        def failing_stat(self_, *a, **kw):
            if self_ == single_heic:
                raise OSError("permission denied")
            return real_stat(self_, *a, **kw)

        mocker.patch("heic2jpg.convert.Path.stat", failing_stat)
        res = self._call(single_heic, times=True)
        assert res.status is convert.Status.OK
        assert res.warnings == []

    def test_times_birthtime_float_fallback(self, single_heic, mocker):
        """Hits the st_birthtime float→ns branch (macOS-only)."""
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

        mocker.patch("heic2jpg.osutils.sys.platform", "darwin")
        mock_utime = mocker.patch("heic2jpg.osutils.os.utime")
        mocker.patch("heic2jpg.convert.Path.stat", selective_stat)
        res = self._call(single_heic, times=True)

        assert res.status is convert.Status.OK
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

        mock_utime = mocker.patch("heic2jpg.osutils.os.utime")
        mocker.patch("heic2jpg.convert.Path.stat", selective_stat)
        res = self._call(single_heic, times=True)

        assert res.status is convert.Status.OK
        mock_utime.assert_called_once()

    def test_times_windows_birthtime_ignored_uses_mtime(self, single_heic, mocker):
        """
        Windows also populates st_birthtime (since Python 3.12), but there it
        means "when this file was copied onto this machine" rather than "when
        the photo was taken" - unlike macOS/APFS. mtime must win even though
        birthtime is present.
        """
        mock_stat = MagicMock()
        mock_stat.st_birthtime_ns = 1_700_000_000_000_000_000  # would be wrong if used
        mock_stat.st_birthtime = 1_700_000_000.0
        mock_stat.st_atime_ns = 1_600_000_000_000_000_000
        mock_stat.st_mtime_ns = 1_600_000_000_000_000_000

        real_stat = Path.stat

        def selective_stat(self_, *a, **kw):
            if self_ == single_heic:
                return mock_stat
            return real_stat(self_, *a, **kw)

        mocker.patch("heic2jpg.osutils.sys.platform", "win32")
        # Faking win32 also un-guards the kernel32 helper, which would blow up on
        # ctypes.WinDLL off Windows. It is covered on its own in test_osutils.
        mocker.patch("heic2jpg.osutils._set_creation_time_windows")
        mock_utime = mocker.patch("heic2jpg.osutils.os.utime")
        mocker.patch("heic2jpg.convert.Path.stat", selective_stat)
        res = self._call(single_heic, times=True)

        assert res.status is convert.Status.OK
        mock_utime.assert_called_once()
        call_args = mock_utime.call_args
        ns_pair = call_args.kwargs.get("ns") or call_args[1].get("ns") or call_args[0][1]
        assert ns_pair[1] == 1_600_000_000_000_000_000

    def test_tmp_unlink_oserror_suppressed(self, single_heic, mocker):
        """OSError during tmp.unlink() in the except block is suppressed.
        Patch Path.replace to fail after convert_with_pillow has written the
        tmp file, so the cleanup path runs."""
        mocker.patch("heic2jpg.convert.Path.replace", side_effect=OSError("replace failed"))
        res = self._call(single_heic)
        assert res.status is convert.Status.FAIL

    def test_idempotent_with_force(self, single_heic):
        self._call(single_heic, force=True)
        self._call(single_heic, force=True)
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_metadata_true_does_not_crash(self, single_heic):
        assert self._call(single_heic, metadata=True).status is convert.Status.OK

    def test_explicit_out_path_used(self, single_heic, tmp_path):
        out = tmp_path / "custom-1.jpg"
        res = self._call(single_heic, out=out)
        assert res.status is convert.Status.OK
        assert is_valid_jpeg(out)
        assert not single_heic.with_suffix(".jpg").exists()

    def test_keyboard_interrupt_cleans_tmp_and_propagates(self, single_heic, mocker):
        """Ctrl-C mid-conversion must not leave a .tmp file behind."""

        def fake_convert(src, tmp, quality, metadata):
            tmp.write_bytes(b"partial")
            raise KeyboardInterrupt

        mocker.patch("heic2jpg.convert.convert_with_pillow", side_effect=fake_convert)
        with pytest.raises(KeyboardInterrupt):
            self._call(single_heic)
        assert list(single_heic.parent.glob("*.tmp.*")) == []


# ---------------------------------------------------------------------------
# Codec detection and package version
# ---------------------------------------------------------------------------


class TestHelpers:
    @pytest.fixture(autouse=True)
    def _fresh_codec_cache(self):
        """_try_pillow_heif is cached; clear it so each test exercises the real body."""
        convert._try_pillow_heif.cache_clear()
        yield
        convert._try_pillow_heif.cache_clear()

    def test_try_pillow_heif_returns_true(self, mocker):
        mocker.patch("heic2jpg.convert.pillow_heif.register_heif_opener")
        assert convert._try_pillow_heif() is True

    def test_try_pillow_heif_returns_false_on_oserror(self, mocker):
        mocker.patch("heic2jpg.convert.pillow_heif.register_heif_opener", side_effect=OSError("no codec"))
        assert convert._try_pillow_heif() is False

    def test_try_pillow_heif_registers_only_once(self, mocker):
        mock_register = mocker.patch("heic2jpg.convert.pillow_heif.register_heif_opener")
        convert._try_pillow_heif()
        convert._try_pillow_heif()
        mock_register.assert_called_once()

    def test_version_is_semver(self):
        assert re.match(r"^\d+\.\d+\.\d+", heic2jpg.__version__), (
            f"__version__ {heic2jpg.__version__!r} does not start with MAJOR.MINOR.PATCH"
        )


# ---------------------------------------------------------------------------
# Arbitrary byte payloads  →  convert_one never crashes the process
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
class TestFuzzCorruptPayloads:
    """
    Property: convert_one must NEVER raise an unhandled exception, regardless of
    what bytes are in the source file.  It may return Status.FAIL, but it must
    return a well-formed Result.

    Corrupt bytes can produce a header declaring a huge pixel count, which trips
    Pillow's DecompressionBombWarning - expected noise here, so it's filtered.
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

        result = convert.convert_one(src, convert.Options(quality=50, keep=True, force=True))

        assert isinstance(result, convert.Result)
        assert result.src == src
        assert isinstance(result.status, convert.Status)
        if result.status is convert.Status.FAIL:
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

        result = convert.convert_one(src, convert.Options(quality=75, keep=True, force=True, metadata=True))
        assert isinstance(result.status, convert.Status)

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

        result = convert.convert_one(src, convert.Options(quality=quality, keep=True, force=True))
        assert isinstance(result.status, convert.Status)


# ---------------------------------------------------------------------------
# Valid HEIC + mutated single bytes  →  convert_one
# ---------------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
class TestFuzzBitFlips:
    """
    Start from a real HEIC file and flip one byte at a random position. The
    converter must still return without raising (corrupted files should produce
    a fail status from convert_one, not an unhandled exception from
    convert_with_pillow propagating all the way up).

    A flipped dimension byte can inflate the declared pixel count, tripping
    Pillow's DecompressionBombWarning - expected noise here, so it's filtered.
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

        result = convert.convert_one(src, convert.Options(quality=50, keep=True, force=True))
        assert isinstance(result.status, convert.Status)


# ---------------------------------------------------------------------------
# Valid HEIC with random image parameters  →  convert_with_pillow
# ---------------------------------------------------------------------------


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
    def test_valid_heic_always_produces_valid_jpeg(self, tmp_path, width, height, r, g, b, quality, metadata):  # noqa: PLR0913, PLR0917
        heic_bytes = make_heic_bytes(size=(width, height), color=(r, g, b))
        src = tmp_path / "valid.heic"
        src.write_bytes(heic_bytes)
        out = tmp_path / "out.jpg"
        if out.exists():
            out.unlink()

        convert.convert_with_pillow(src, out, quality=quality, metadata=metadata)

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

        convert.convert_with_pillow(src, out, quality=50, metadata=False)

        with Image.open(out) as im:
            assert im.mode == "RGB"
