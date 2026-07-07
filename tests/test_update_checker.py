"""Tests for the PyPI update-notice helper."""

import importlib
import io
import json
import re
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.version import Version

# The only package-specific value: set this to port the suite to another project.
PACKAGE = "heic2jpg"

update_checker = importlib.import_module(f"{PACKAGE}.update_checker")
UpdateNotifier = update_checker.UpdateNotifier
_detect_upgrade_command = update_checker._detect_upgrade_command
_is_newer = update_checker._is_newer
_parse_version = update_checker._parse_version

# Derived, mirroring update_checker._enabled()'s env-var derivation.
ENV_PREFIX = re.sub(r"[^A-Z0-9]", "_", PACKAGE.upper())
OPT_OUT_VARS = ("CI", "NO_UPDATE_CHECK", f"{ENV_PREFIX}_NO_UPDATE_CHECK")
UPGRADE_COMMAND = f"uv tool upgrade {PACKAGE}"


# ===========================================================================
# Helpers / fixtures
# ===========================================================================


@pytest.fixture
def enabled_env(monkeypatch):
    """Make the notifier believe it's an interactive, non-CI run."""
    for var in OPT_OUT_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(update_checker, "_stderr_is_interactive", lambda: True)


def fake_pypi(monkeypatch, version: str):
    """Replace urllib.request.urlopen with a canned PyPI JSON response."""
    body = json.dumps({"info": {"version": version}}).encode()
    captured = SimpleNamespace(url=None)

    def fake_urlopen(request, timeout=None):
        captured.url = request.full_url
        return io.BytesIO(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


def write_cache(path: Path, latest: str, age_seconds: float = 0.0) -> None:
    path.write_text(json.dumps({"latest": latest, "checked_at": time.time() - age_seconds}))


# ===========================================================================
# Version parsing / comparison
# ===========================================================================


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.3.1", Version("2.3.1")),
        ("2.3.1rc1", Version("2.3.1rc1")),  # pre-release preserved (PEP 440)
        ("10", Version("10")),
        (" 1.2 ", Version("1.2")),  # surrounding whitespace tolerated
        ("v1.2", Version("1.2")),  # PEP 440 allows a leading 'v'
        ("unknown", None),
        ("", None),
    ],
)
def test_parse_version(version, expected):
    assert _parse_version(version) == expected


@pytest.mark.parametrize(
    ("latest", "current", "newer"),
    [
        ("2.3.0", "2.2.1", True),
        ("2.10.0", "2.9.9", True),  # numeric, not lexicographic
        ("2.2.0.1", "2.2", True),  # longer release ordered correctly
        ("2.2.1", "2.2.1rc1", True),  # a final release is newer than its own rc
        ("2.2.1", "2.2.1", False),
        ("2.2", "2.2.0", False),  # equal under PEP 440
        ("2.2.1", "2.3.0", False),  # local version ahead of PyPI
        ("2.2.1rc1", "2.2.1", False),  # an rc is not newer than the final
        ("unknown", "2.2.1", False),
        ("2.3.0", "unknown", False),
    ],
)
def test_is_newer(latest, current, newer):
    assert _is_newer(latest, current) is newer


# ===========================================================================
# Opt-outs
# ===========================================================================


@pytest.mark.parametrize("var", OPT_OUT_VARS)
def test_disabled_by_env_var(enabled_env, monkeypatch, tmp_path, var):
    monkeypatch.setenv(var, "1")
    notifier = UpdateNotifier(PACKAGE, "1.0", cache_dir=tmp_path)
    assert not notifier._enabled()


def test_disabled_for_unparsable_version(enabled_env, tmp_path):
    notifier = UpdateNotifier(PACKAGE, "unknown", cache_dir=tmp_path)
    assert not notifier._enabled()


def test_disabled_when_stderr_not_a_tty(enabled_env, monkeypatch, tmp_path):
    monkeypatch.setattr(update_checker, "_stderr_is_interactive", lambda: False)
    notifier = UpdateNotifier(PACKAGE, "1.0", cache_dir=tmp_path)
    assert not notifier._enabled()


def test_disabled_start_does_nothing(enabled_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CI", "true")

    def boom(*args, **kwargs):
        raise AssertionError("network must not be touched when disabled")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    notifier = UpdateNotifier(PACKAGE, "0.1", cache_dir=tmp_path)
    notifier.start()
    notifier.notify()
    assert capsys.readouterr().err == ""


# ===========================================================================
# Cache behaviour
# ===========================================================================


def test_fresh_cache_skips_network(enabled_env, monkeypatch, tmp_path, capsys):
    def boom(*args, **kwargs):
        raise AssertionError("network must not be touched with a fresh cache")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    notifier = UpdateNotifier(PACKAGE, "1.0", upgrade_command=UPGRADE_COMMAND, cache_dir=tmp_path)
    write_cache(notifier._cache_path, "2.0")

    notifier.start()
    notifier.notify()
    assert f"Update available! Run: '{UPGRADE_COMMAND}'" in capsys.readouterr().err


def test_stale_cache_triggers_fetch_and_rewrite(enabled_env, monkeypatch, tmp_path, capsys):
    fake_pypi(monkeypatch, "3.0")
    notifier = UpdateNotifier(PACKAGE, "1.0", cache_dir=tmp_path)
    write_cache(notifier._cache_path, "2.0", age_seconds=2 * 24 * 3600)

    notifier.start()
    notifier.notify(timeout=5)
    assert "Update available!" in capsys.readouterr().err
    assert json.loads(notifier._cache_path.read_text())["latest"] == "3.0"


def test_corrupt_cache_is_ignored(enabled_env, monkeypatch, tmp_path, capsys):
    fake_pypi(monkeypatch, "3.0")
    notifier = UpdateNotifier(PACKAGE, "1.0", cache_dir=tmp_path)
    notifier._cache_path.write_text("{not json")

    notifier.start()
    notifier.notify(timeout=5)
    assert "Update available!" in capsys.readouterr().err


# ===========================================================================
# Fetch / notify
# ===========================================================================


def test_fetch_queries_pypi_and_notifies(enabled_env, monkeypatch, tmp_path, capsys):
    fake = fake_pypi(monkeypatch, "9.9.9")
    notifier = UpdateNotifier(PACKAGE, "1.0", upgrade_command=UPGRADE_COMMAND, cache_dir=tmp_path)

    notifier.start()
    notifier.notify(timeout=5)
    assert fake.url == f"https://pypi.org/pypi/{PACKAGE}/json"
    assert f"Update available! Run: '{UPGRADE_COMMAND}'" in capsys.readouterr().err


def test_no_hint_when_up_to_date(enabled_env, monkeypatch, tmp_path, capsys):
    fake_pypi(monkeypatch, "1.0")
    notifier = UpdateNotifier(PACKAGE, "1.0", cache_dir=tmp_path)

    notifier.start()
    notifier.notify(timeout=5)
    assert capsys.readouterr().err == ""


def test_network_failure_is_silent(enabled_env, monkeypatch, tmp_path, capsys):
    def offline(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    notifier = UpdateNotifier(PACKAGE, "1.0", cache_dir=tmp_path)

    notifier.start()
    notifier.notify(timeout=5)
    assert capsys.readouterr().err == ""
    assert not notifier._cache_path.exists()


def test_custom_upgrade_command(enabled_env, monkeypatch, tmp_path, capsys):
    # A deliberately different package name proves the custom command is honoured verbatim.
    fake_pypi(monkeypatch, "2.0")
    notifier = UpdateNotifier("other-pkg", "1.0", upgrade_command="pipx upgrade other-pkg", cache_dir=tmp_path)

    notifier.start()
    notifier.notify(timeout=5)
    assert "Run: 'pipx upgrade other-pkg'" in capsys.readouterr().err


# ===========================================================================
# Colour
# ===========================================================================


def test_hint_is_coloured_when_stderr_supports_it(enabled_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(update_checker, "_stderr_supports_color", lambda: True)
    notifier = UpdateNotifier(PACKAGE, "1.0", upgrade_command=UPGRADE_COMMAND, cache_dir=tmp_path)
    write_cache(notifier._cache_path, "2.0")

    notifier.start()
    notifier.notify()
    err = capsys.readouterr().err
    assert err.startswith(update_checker.ORANGE)
    assert err.rstrip("\n").endswith(update_checker.RESET)


def test_hint_is_plain_when_stderr_lacks_colour(enabled_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(update_checker, "_stderr_supports_color", lambda: False)
    notifier = UpdateNotifier(PACKAGE, "1.0", upgrade_command=UPGRADE_COMMAND, cache_dir=tmp_path)
    write_cache(notifier._cache_path, "2.0")

    notifier.start()
    notifier.notify()
    err = capsys.readouterr().err
    assert "\033[" not in err
    assert f"Update available! Run: '{UPGRADE_COMMAND}'" in err


# ===========================================================================
# Installer detection
# ===========================================================================


@pytest.mark.parametrize(
    ("module_path", "expected"),
    [
        (
            f"/Users/u/.local/share/uv/tools/{PACKAGE}/lib/python3.14/site-packages/{PACKAGE}/update_checker.py",
            f"uv tool upgrade {PACKAGE}",
        ),
        (
            f"/home/u/.local/pipx/venvs/{PACKAGE}/lib/python3.14/site-packages/{PACKAGE}/update_checker.py",
            f"pipx upgrade {PACKAGE}",
        ),
        (
            f"/opt/homebrew/Cellar/{PACKAGE}/2.3.0/libexec/lib/python3.14/site-packages/{PACKAGE}/update_checker.py",
            f"brew upgrade {PACKAGE}",
        ),
        (
            f"/home/linuxbrew/.linuxbrew/Cellar/{PACKAGE}/2.3.0/libexec/lib/python3.14/site-packages/{PACKAGE}/update_checker.py",
            f"brew upgrade {PACKAGE}",
        ),
        (
            # Nothing recognizable and no venv: recommend the default install method.
            f"/usr/lib/python3.14/site-packages/{PACKAGE}/update_checker.py",
            f"uv tool upgrade {PACKAGE}",
        ),
    ],
)
def test_detect_upgrade_command_from_install_path(tmp_path, module_path, expected):
    # tmp_path stands in for sys.prefix: no pyvenv.cfg there, so only the path decides.
    assert _detect_upgrade_command(PACKAGE, Path(module_path), sys_prefix=str(tmp_path)) == expected


def test_detect_upgrade_command_uv_venv(tmp_path):
    (tmp_path / "pyvenv.cfg").write_text("home = /usr/local/bin\nuv = 0.7.2\nversion_info = 3.14.0\n")
    module_path = tmp_path / "lib" / "python3.14" / "site-packages" / PACKAGE / "update_checker.py"
    assert (
        _detect_upgrade_command(PACKAGE, module_path, sys_prefix=str(tmp_path))
        == f"uv pip install --upgrade {PACKAGE}"
    )


def test_detect_upgrade_command_plain_venv(tmp_path):
    (tmp_path / "pyvenv.cfg").write_text("home = /usr/local/bin\nversion = 3.14.0\n")
    module_path = tmp_path / "lib" / "python3.14" / "site-packages" / PACKAGE / "update_checker.py"
    assert (
        _detect_upgrade_command(PACKAGE, module_path, sys_prefix=str(tmp_path))
        == f"pip install --upgrade {PACKAGE}"
    )


def test_detect_upgrade_command_path_beats_pyvenv_cfg(tmp_path):
    # uv tool environments are uv-stamped venvs too; the tool path must win.
    prefix = tmp_path / "uv" / "tools" / PACKAGE
    prefix.mkdir(parents=True)
    (prefix / "pyvenv.cfg").write_text("uv = 0.7.2\n")
    module_path = prefix / "lib" / "python3.14" / "site-packages" / PACKAGE / "update_checker.py"
    assert _detect_upgrade_command(PACKAGE, module_path, sys_prefix=str(prefix)) == f"uv tool upgrade {PACKAGE}"
