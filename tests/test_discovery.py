"""Tests for heic2jpg.discovery."""

import contextlib
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies

from heic2jpg import discovery

# ---------------------------------------------------------------------------
# collect_files
# ---------------------------------------------------------------------------


class TestCollectFiles:
    def test_single_heic_file(self, single_heic):
        assert discovery.collect_files(single_heic) == [single_heic]

    def test_single_non_heic_file(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"\x89PNG\r\n")
        assert discovery.collect_files(f) == []

    def test_empty_directory(self, tmp_path):
        assert discovery.collect_files(tmp_path) == []

    def test_directory_returns_three_files(self, heic_dir):
        assert len(discovery.collect_files(heic_dir)) == 3

    def test_result_is_sorted(self, heic_dir):
        result = discovery.collect_files(heic_dir)
        assert result == sorted(result)

    def test_nonexistent_path_returns_empty(self, tmp_path):
        assert discovery.collect_files(tmp_path / "ghost") == []

    def test_ignores_non_heic_files(self, tmp_path):
        (tmp_path / "photo.heic").write_bytes(b"")
        (tmp_path / "photo.jpg").write_bytes(b"")
        (tmp_path / "notes.txt").write_bytes(b"")
        assert len(discovery.collect_files(tmp_path)) == 1

    def test_case_insensitive_extension(self, tmp_path):
        (tmp_path / "upper.HEIC").write_bytes(b"")
        (tmp_path / "lower.heic").write_bytes(b"")
        (tmp_path / "mixed.HeIc").write_bytes(b"")
        assert len(discovery.collect_files(tmp_path)) == 3

    def test_non_recursive_does_not_descend(self, nested_heic_dir):
        result = discovery.collect_files(nested_heic_dir, recursive=False)
        assert [p.name for p in result] == ["top.heic"]

    def test_recursive_finds_all(self, nested_heic_dir):
        assert len(discovery.collect_files(nested_heic_dir, recursive=True)) == 4

    def test_recursive_result_is_sorted(self, nested_heic_dir):
        result = discovery.collect_files(nested_heic_dir, recursive=True)
        assert result == sorted(result)


# ---------------------------------------------------------------------------
# plan_outputs
# ---------------------------------------------------------------------------


class TestPlanOutputs:
    """Batch-collision cases use fake /x paths (nothing exists there);
    disk-diversion cases create real files in tmp_path."""

    def test_empty(self):
        assert discovery.plan_outputs([]) == []

    def test_no_collision_maps_to_jpg(self):
        files = [Path("/x/a.heic"), Path("/x/b.HEIC")]
        assert discovery.plan_outputs(files) == [Path("/x/a.jpg"), Path("/x/b.jpg")]

    def test_case_variant_collision_gets_numeric_suffix(self):
        files = [Path("/x/IMG.HEIC"), Path("/x/IMG.heic")]
        assert discovery.plan_outputs(files) == [Path("/x/IMG.jpg"), Path("/x/IMG-1.jpg")]

    def test_same_name_in_different_dirs_does_not_collide(self):
        files = [Path("/x/IMG.heic"), Path("/y/IMG.heic")]
        assert discovery.plan_outputs(files) == [Path("/x/IMG.jpg"), Path("/y/IMG.jpg")]

    def test_suffix_cascades_when_suffixed_name_also_taken(self):
        files = [Path("/x/IMG-1.heic"), Path("/x/IMG.HEIC"), Path("/x/IMG.heic")]
        assert discovery.plan_outputs(files) == [
            Path("/x/IMG-1.jpg"),
            Path("/x/IMG.jpg"),
            Path("/x/IMG-2.jpg"),
        ]

    def test_outputs_are_always_unique(self):
        files = [Path(f"/x/IMG.{ext}") for ext in ("heic", "HEIC", "HeIc", "hEiC")]
        outs = discovery.plan_outputs(files)
        assert len(set(outs)) == len(files)

    def test_diverts_around_existing_file(self, tmp_path):
        (tmp_path / "IMG.jpg").write_bytes(b"bystander")
        assert discovery.plan_outputs([tmp_path / "IMG.heic"]) == [tmp_path / "IMG-1.jpg"]

    def test_diverts_past_existing_suffixed_file(self, tmp_path):
        (tmp_path / "IMG.jpg").write_bytes(b"bystander")
        (tmp_path / "IMG-1.jpg").write_bytes(b"also taken")
        assert discovery.plan_outputs([tmp_path / "IMG.heic"]) == [tmp_path / "IMG-2.jpg"]

    def test_force_reclaims_natural_name_only(self, tmp_path):
        """-f overwrites IMG.heic's own IMG.jpg, but an existing IMG-1.jpg
        may be an unrelated photo and is still diverted around."""
        (tmp_path / "IMG.jpg").write_bytes(b"mine")
        (tmp_path / "IMG-1.jpg").write_bytes(b"bystander")
        files = [tmp_path / "IMG.heic", tmp_path / "IMG.HEIC"]
        assert discovery.plan_outputs(files, force=True) == [
            tmp_path / "IMG.jpg",
            tmp_path / "IMG-2.jpg",
        ]


# ---------------------------------------------------------------------------
# Exotic filenames  →  collect_files
# ---------------------------------------------------------------------------

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
                # Skip generated names that are invalid on this OS
                pass

        result = discovery.collect_files(tmp_path, recursive=False)
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

        result = discovery.collect_files(tmp_path, recursive=False)
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

        flat = discovery.collect_files(tmp_path, recursive=False)
        deep = discovery.collect_files(tmp_path, recursive=True)
        assert len(deep) >= len(flat)
