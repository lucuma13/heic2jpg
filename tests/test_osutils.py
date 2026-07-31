"""Tests for heic2jpg.osutils."""

import ctypes
import ctypes.wintypes
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings, strategies

from heic2jpg import osutils

# ---------------------------------------------------------------------------
# _set_creation_time_windows  →  Windows creation-time helper
# ---------------------------------------------------------------------------


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
        assert osutils._set_creation_time_windows(f, 0) is None

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
    def test_sets_ctime(self, tmp_path):
        """SetFileTime runs without error and the file remains intact."""
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        osutils._set_creation_time_windows(f, int(time.time() * 1e9))
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
            osutils._set_creation_time_windows(f, 0)

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
            osutils._set_creation_time_windows(f, 0)

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
            osutils._set_creation_time_windows(f, ctime_ns)
        mock_kernel.CloseHandle.assert_called_once_with(999)

    @given(ctime_ns=strategies.integers())
    @settings(max_examples=500, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @pytest.mark.skipif(sys.platform == "win32", reason="non-Windows no-op path")
    def test_noop_on_non_windows_never_raises(self, tmp_path, ctime_ns):
        """Any integer timestamp - positive, negative, or zero - is a no-op on non-Windows."""
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        assert osutils._set_creation_time_windows(f, ctime_ns) is None


class TestSetCreationTimeWindowsFakedWin32:
    """
    Exercise the Windows body on every platform by faking win32: patch
    sys.platform and stub ctypes.WinDLL (which doesn't exist off Windows, hence
    create=True). This reaches the epoch arithmetic, FILETIME packing, and error
    paths that the skipif(Windows-only) tests never run on a non-Windows host.
    """

    def _fake_kernel(self, mocker, *, handle=999, setfiletime=1):
        kernel = MagicMock()
        kernel.CreateFileW.return_value = handle
        kernel.SetFileTime.return_value = setfiletime
        mocker.patch("heic2jpg.osutils.sys.platform", "win32")
        mocker.patch("ctypes.WinDLL", return_value=kernel, create=True)
        # get_last_error is Windows-only; the error paths call it after a failure.
        mocker.patch("ctypes.get_last_error", return_value=5, create=True)
        return kernel

    def test_success_sets_time_and_closes_handle(self, tmp_path, mocker):
        kernel = self._fake_kernel(mocker)
        f = tmp_path / "x.txt"
        f.write_bytes(b"y")
        osutils._set_creation_time_windows(f, int(time.time() * 1e9))
        kernel.SetFileTime.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(999)

    def test_invalid_handle_raises_before_open_handle(self, tmp_path, mocker):
        invalid = ctypes.wintypes.HANDLE(-1).value
        kernel = self._fake_kernel(mocker, handle=invalid)
        f = tmp_path / "x.txt"
        f.write_bytes(b"y")
        with pytest.raises(OSError, match="CreateFileW failed"):
            osutils._set_creation_time_windows(f, 0)
        # We never got a valid handle, so nothing to close.
        kernel.CloseHandle.assert_not_called()

    def test_setfiletime_failure_raises_but_still_closes(self, tmp_path, mocker):
        kernel = self._fake_kernel(mocker, setfiletime=0)
        f = tmp_path / "x.txt"
        f.write_bytes(b"y")
        with pytest.raises(OSError, match="SetFileTime failed"):
            osutils._set_creation_time_windows(f, 0)
        # finally-clause closes the handle even on failure.
        kernel.CloseHandle.assert_called_once_with(999)

    @given(ctime_ns=strategies.integers(min_value=0, max_value=2**63 - 1))
    @settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_epoch_arithmetic_never_raises(self, tmp_path, ctime_ns):
        """Any nanosecond timestamp must pack into a FILETIME without overflow."""
        kernel = MagicMock()
        kernel.CreateFileW.return_value = 999
        kernel.SetFileTime.return_value = 1
        f = tmp_path / "dummy.txt"
        f.write_bytes(b"x")
        with (
            patch("heic2jpg.osutils.sys.platform", "win32"),
            patch("ctypes.WinDLL", return_value=kernel, create=True),
        ):
            osutils._set_creation_time_windows(f, ctime_ns)
        kernel.CloseHandle.assert_called_once_with(999)
