"""Tests for heic2jpg.cli."""

import tempfile
from unittest.mock import patch

import pytest
from hypothesis import given, settings, strategies

from heic2jpg import cli

from .helpers import is_valid_jpeg, make_heic

# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_defaults(self):
        args = cli.parse_args(["."])
        assert args.quality == 30
        assert args.metadata is False
        assert args.times is False
        assert args.keep is False
        assert args.recursive is False
        assert args.force is False
        assert args.verbose is False

    def test_all_flags(self, tmp_path):
        args = cli.parse_args(["-q", "60", "-m", "-t", "-k", "-R", "-f", "-v", str(tmp_path)])
        assert args.quality == 60
        assert args.path == str(tmp_path)
        assert all(getattr(args, f) for f in ("metadata", "times", "keep", "recursive", "force", "verbose"))

    def test_version_exits(self):
        with pytest.raises(SystemExit):
            cli.parse_args(["--version"])

    def test_default_path_is_dot(self):
        assert cli.parse_args([]).path == "."


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    def test_quality_zero_returns_1(self, capsys):
        assert cli.main(["-q", "0", "."]) == 1

    def test_quality_101_returns_1(self, capsys):
        assert cli.main(["-q", "101", "."]) == 1

    def test_nonexistent_path_returns_1(self, tmp_path, capsys):
        assert cli.main([str(tmp_path / "no_such_dir")]) == 1
        assert "does not exist" in capsys.readouterr().err

    def test_non_heic_file_returns_1(self, tmp_path, capsys):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG")
        assert cli.main([str(f)]) == 1
        assert "not a HEIC" in capsys.readouterr().err

    def test_neither_file_nor_dir_returns_1(self, tmp_path, capsys):
        """Hits the 'not a file or directory' branch via mocking."""
        with (
            patch("heic2jpg.cli.Path.exists", return_value=True),
            patch("heic2jpg.cli.Path.is_file", return_value=False),
            patch("heic2jpg.cli.Path.is_dir", return_value=False),
        ):
            rc = cli.main([str(tmp_path / "weird")])
        assert rc == 1
        assert "not a file or directory" in capsys.readouterr().err

    def test_no_heic_files_returns_2(self, tmp_path, capsys):
        assert cli.main([str(tmp_path)]) == 2

    def test_no_codec_returns_1(self, tmp_path, capsys):
        with patch.object(cli, "_try_pillow_heif", return_value=False):
            assert cli.main([str(tmp_path)]) == 1
        assert "codec" in capsys.readouterr().err.lower()

    def test_converts_single_file_returns_0(self, single_heic):
        assert cli.main([str(single_heic)]) == 0
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_converts_directory_returns_0(self, heic_dir):
        assert cli.main([str(heic_dir)]) == 0
        assert len(list(heic_dir.glob("*.jpg"))) == 3

    def test_recursive(self, nested_heic_dir):
        assert cli.main(["-R", str(nested_heic_dir)]) == 0
        assert len(list(nested_heic_dir.rglob("*.jpg"))) == 4

    def test_verbose_output(self, single_heic, capsys):
        cli.main(["-v", "-k", str(single_heic)])
        assert "Done" in capsys.readouterr().err

    def test_existing_jpg_diverts_instead_of_skipping(self, single_heic, capsys):
        """
        The taken name is preserved, the HEIC still converts (to a numbered
        variant), and the diversion is announced without -v.
        """
        jpg = single_heic.with_suffix(".jpg")
        jpg.write_bytes(b"original")
        assert cli.main(["-k", str(single_heic)]) == 0
        assert jpg.read_bytes() == b"original"
        assert is_valid_jpeg(single_heic.with_name("single-1.jpg"))
        err = capsys.readouterr().err
        assert "warning" in err
        assert "single-1.jpg" in err

    def test_force_overwrites(self, single_heic):
        single_heic.with_suffix(".jpg").write_bytes(b"stale")
        cli.main(["-f", "-k", str(single_heic)])
        assert is_valid_jpeg(single_heic.with_suffix(".jpg"))

    def test_failed_conversion_returns_3(self, tmp_path, capsys):
        bad = tmp_path / "corrupt.heic"
        bad.write_bytes(b"\x00" * 32)
        assert cli.main([str(bad)]) == 3
        assert "FAIL" in capsys.readouterr().err

    def test_quality_boundaries(self, single_heic):
        assert cli.main(["-q", "1", "-k", str(single_heic)]) == 0
        assert cli.main(["-q", "100", "-k", str(single_heic), "-f"]) == 0

    def test_single_file_goes_through_pool(self, tmp_path, mocker):
        """There is one execution path: even a single file runs via run_pool."""
        make_heic(tmp_path / "only.heic")
        mock_pool = mocker.patch("heic2jpg.cli.run_pool", return_value=(1, 0, 0, False))
        assert cli.main(["-k", str(tmp_path)]) == 0
        mock_pool.assert_called_once()

    def test_verbose_prints_fail_summary(self, tmp_path, capsys):
        bad = tmp_path / "bad.heic"
        bad.write_bytes(b"\x00" * 32)
        cli.main(["-v", str(bad)])
        assert "failed=1" in capsys.readouterr().err

    def test_keyboard_interrupt_returns_130(self, single_heic, mocker, capsys):
        """Ctrl-C mid-run: graceful message, exit code 130."""
        mocker.patch("heic2jpg.runner.convert_one", side_effect=KeyboardInterrupt)
        rc = cli.main(["-k", str(single_heic)])
        assert rc == 130
        assert "Interrupted" in capsys.readouterr().err

    def test_pool_interrupt_returns_130(self, heic_dir, mocker):
        mocker.patch("heic2jpg.cli.run_pool", return_value=(0, 0, 0, True))
        assert cli.main(["-k", str(heic_dir)]) == 130


# ---------------------------------------------------------------------------
# Quality boundary / out-of-range values  →  parse_args + main
# ---------------------------------------------------------------------------


class TestFuzzQualityValues:
    """
    Property: quality values outside [1, 100] must cause main() to return 1
    without raising.  Values inside the range must not trigger that error.
    """

    @given(quality=strategies.integers(min_value=101, max_value=10_000))
    def test_quality_above_100_returns_1(self, quality):
        rc = cli.main(["-q", str(quality), "."])
        assert rc == 1

    @given(quality=strategies.integers(min_value=-10_000, max_value=0))
    def test_quality_zero_or_below_returns_1(self, quality):
        rc = cli.main(["-q", str(quality), "."])
        assert rc == 1

    @given(quality=strategies.integers(min_value=1, max_value=100))
    def test_valid_quality_does_not_return_1_for_quality_reason(self, quality):
        """
        A valid quality value should never produce rc=1 due to quality
        validation.  (It may still return 2 if no HEIC files exist — that's
        fine; we just confirm it's not the quality-error path.) Uses tempfile
        rather than tmp_path because hypothesis reuses the fixture across
        examples — each example needs its own clean directory.
        """
        with tempfile.TemporaryDirectory() as d:
            rc = cli.main(["-q", str(quality), d])
        assert rc in {0, 2, 3}

    @given(quality=strategies.integers(min_value=1, max_value=100))
    def test_parse_args_accepts_valid_quality(self, quality):
        args = cli.parse_args(["-q", str(quality), "."])
        assert args.quality == quality


# ---------------------------------------------------------------------------
# Arbitrary CLI argument lists  →  parse_args robustness
# ---------------------------------------------------------------------------


class TestFuzzParseArgs:
    """
    parse_args wraps argparse, which may call sys.exit() on truly invalid input.
    The property here is: for any set of *valid* boolean flags plus an in-range
    quality, parse_args must succeed and round-trip the quality.
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

        args = cli.parse_args(argv)
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
        args = cli.parse_args(["-q", str(quality), "."])
        assert args.quality == quality
