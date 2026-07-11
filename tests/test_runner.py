"""Tests for heic2jpg.runner."""

import os
import signal
import sys
from unittest.mock import patch

import pytest

from heic2jpg import discovery, runner
from heic2jpg.convert import Options

from .helpers import is_valid_jpeg, make_heic

# ---------------------------------------------------------------------------
# run_pool
# ---------------------------------------------------------------------------


class TestRunPool:
    def _pool(self, files, jobs=2, verbose=False, **opt_kwargs):
        kw: dict = {"quality": 50, "keep": True, "force": False, "metadata": False, "times": False}
        kw.update(opt_kwargs)
        return runner.run_pool(files, discovery.plan_outputs(files), jobs, Options(**kw), verbose)

    def test_all_ok(self, heic_dir):
        files = discovery.collect_files(heic_dir)
        ok, skipped, failed, interrupted = self._pool(files)
        assert ok == 3
        assert skipped == 0
        assert failed == 0
        assert interrupted is False

    def test_existing_output_diverts_and_converts(self, heic_dir):
        files = discovery.collect_files(heic_dir)
        blocked = files[0].with_suffix(".jpg")
        blocked.write_bytes(b"x")
        ok, skipped, _, _ = self._pool(files)
        assert ok == 3
        assert skipped == 0
        assert blocked.read_bytes() == b"x"
        assert is_valid_jpeg(files[0].with_name(f"{files[0].stem}-1.jpg"))

    def test_skip_counted_when_output_appears_after_planning(self, single_heic):
        """
        convert_one's exists() backstop: outs planned before the file existed
        still yield SKIP rather than an overwrite.
        """
        out = single_heic.with_suffix(".jpg")
        out.write_bytes(b"x")
        opts = Options(keep=True)
        ok, skipped, failed, _ = runner.run_pool([single_heic], [out], 1, opts, verbose=False)
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
        mocker.patch("heic2jpg.convert.Path.unlink", side_effect=PermissionError("locked"))
        self._pool([single_heic], verbose=False, keep=False)
        err = capsys.readouterr().err
        assert "warning:" in err
        assert "could not delete" in err

    def test_verbose_skip(self, single_heic, capsys):
        """Explicit outs bypass plan_outputs' diversion to hit the skip branch."""
        out = single_heic.with_suffix(".jpg")
        out.write_bytes(b"x")
        runner.run_pool([single_heic], [out], 1, Options(keep=True), verbose=True)
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
        """
        KeyboardInterrupt inside as_completed cancels the queue, prints
        'Interrupted.', and the counts still reflect conversions that finished
        during the shutdown drain (the summary must match the disk).
        """
        files = discovery.collect_files(heic_dir)

        def raise_keyboard_interrupt(fs):
            raise KeyboardInterrupt

        with patch("heic2jpg.runner.as_completed", side_effect=raise_keyboard_interrupt):
            ok, skipped, failed, interrupted = self._pool(files)

        assert interrupted is True
        assert ok == len(list(heic_dir.glob("*.jpg")))
        assert skipped == failed == 0
        assert "Interrupted" in capsys.readouterr().err

    def test_sigint_handler_untouched_by_pool(self, heic_dir):
        """run_pool relies on default SIGINT behaviour and must not install its own handler."""
        prev = signal.getsignal(signal.SIGINT)
        self._pool(discovery.collect_files(heic_dir))
        assert signal.getsignal(signal.SIGINT) is prev

    @pytest.mark.skipif(sys.platform == "win32", reason="os.kill(SIGINT) not supported on Windows")
    def test_real_sigint_interrupts_pool(self, heic_dir, capsys):
        """Fire SIGINT mid-pool: the resulting KeyboardInterrupt is caught and 'Interrupted.' printed."""
        files = discovery.collect_files(heic_dir)
        original_as_completed = runner.as_completed

        def sigint_then_complete(fs):
            os.kill(os.getpid(), signal.SIGINT)
            yield from original_as_completed(fs)

        with patch("heic2jpg.runner.as_completed", side_effect=sigint_then_complete):
            self._pool(files)

        assert "Interrupted" in capsys.readouterr().err
