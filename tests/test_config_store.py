"""Tests for swarm.config_store -- the guarded, atomic config.json writer.

Regression coverage for the 2026-09-14 incident where concurrent unguarded
read-modify-write cycles reduced the real config.json to two keys.
"""
import json
import threading
import time

import pytest

from swarm.config_store import (
    SHRINK_GUARD_MIN_KEYS,
    ConfigReadError,
    ConfigWriteRefused,
    read_config_file,
    update_config_file,
)


def _write(path, data):
    path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# read_config_file
# ---------------------------------------------------------------------------

def test_read_missing_file_returns_empty_dict(tmp_path):
    assert read_config_file(tmp_path / "config.json") == {}


def test_read_valid_file(tmp_path):
    cf = tmp_path / "config.json"
    _write(cf, {"a": 1, "b": [1, 2]})
    assert read_config_file(cf) == {"a": 1, "b": [1, 2]}


def test_read_malformed_json_raises(tmp_path):
    cf = tmp_path / "config.json"
    cf.write_text('{"a": 1,')
    with pytest.raises(ConfigReadError):
        read_config_file(cf)


def test_read_empty_file_raises(tmp_path):
    """A zero-byte file is what a torn read sees mid-write_text -- must NOT be {}."""
    cf = tmp_path / "config.json"
    cf.write_text("")
    with pytest.raises(ConfigReadError):
        read_config_file(cf)


def test_read_non_object_raises(tmp_path):
    cf = tmp_path / "config.json"
    cf.write_text("[1, 2, 3]")
    with pytest.raises(ConfigReadError):
        read_config_file(cf)


# ---------------------------------------------------------------------------
# update_config_file: basic behaviour
# ---------------------------------------------------------------------------

def test_missing_file_is_created(tmp_path):
    cf = tmp_path / "config.json"
    result = update_config_file(cf, {"auto_mode_enabled": True})
    assert result == {"auto_mode_enabled": True}
    assert json.loads(cf.read_text()) == {"auto_mode_enabled": True}


def test_update_merges_and_preserves_unrelated_keys(tmp_path):
    cf = tmp_path / "config.json"
    _write(cf, {"a": 1, "b": 2, "c": 3})
    result = update_config_file(cf, {"b": 20, "d": 4})
    assert result == {"a": 1, "b": 20, "c": 3, "d": 4}
    assert json.loads(cf.read_text()) == result


def test_remove_keys(tmp_path):
    cf = tmp_path / "config.json"
    _write(cf, {"a": 1, "b": 2, "c": 3})
    result = update_config_file(cf, {}, remove=["b", "nonexistent"])
    assert result == {"a": 1, "c": 3}
    assert json.loads(cf.read_text()) == result


def test_atomic_write_leaves_no_tmp_file(tmp_path):
    cf = tmp_path / "config.json"
    _write(cf, {"a": 1})
    update_config_file(cf, {"b": 2})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "config.json"]
    assert leftovers == [], leftovers
    assert not cf.with_suffix(".json.tmp").exists()


def test_written_file_ends_with_newline_and_is_pretty(tmp_path):
    cf = tmp_path / "config.json"
    update_config_file(cf, {"a": 1})
    text = cf.read_text()
    assert text.endswith("\n")
    assert "\n  " in text  # indent=2


# ---------------------------------------------------------------------------
# update_config_file: refusal guards
# ---------------------------------------------------------------------------

def test_malformed_existing_file_is_refused_and_untouched(tmp_path, capsys):
    cf = tmp_path / "config.json"
    original = '{"managed_projects": ["x"], "broken": '
    cf.write_text(original)
    with pytest.raises(ConfigWriteRefused):
        update_config_file(cf, {"auto_mode_enabled": True})
    assert cf.read_text() == original
    assert not cf.with_suffix(".json.tmp").exists()
    assert "[Config] REFUSED write" in capsys.readouterr().out


def test_empty_existing_file_is_refused(tmp_path):
    """Simulates the torn-read window of the old write_text pattern."""
    cf = tmp_path / "config.json"
    cf.write_text("")
    with pytest.raises(ConfigWriteRefused):
        update_config_file(cf, {"auto_mode_enabled": True})
    assert cf.read_text() == ""


def test_shrink_guard_refuses_large_shrink(tmp_path, capsys):
    cf = tmp_path / "config.json"
    original = {f"key{i}": i for i in range(10)}
    _write(cf, original)
    with pytest.raises(ConfigWriteRefused):
        update_config_file(
            cf,
            {"key0": "kept"},
            remove=[f"key{i}" for i in range(2, 10)],  # would leave 2 of 10 keys
        )
    assert json.loads(cf.read_text()) == original
    assert "shrink guard" in capsys.readouterr().out


def test_shrink_guard_allows_normal_single_key_update(tmp_path):
    cf = tmp_path / "config.json"
    original = {f"key{i}": i for i in range(10)}
    _write(cf, original)
    result = update_config_file(cf, {"key3": "new"})
    assert result == {**original, "key3": "new"}
    result = update_config_file(cf, {}, remove=["key9"])
    assert len(result) == 9


def test_shrink_guard_allows_removing_just_under_half(tmp_path):
    cf = tmp_path / "config.json"
    _write(cf, {f"key{i}": i for i in range(10)})
    result = update_config_file(cf, {}, remove=[f"key{i}" for i in range(5)])
    assert len(result) == 5  # exactly half is allowed (not < half)


def test_shrink_guard_not_applied_to_small_files(tmp_path):
    cf = tmp_path / "config.json"
    small = {f"key{i}": i for i in range(SHRINK_GUARD_MIN_KEYS - 1)}
    _write(cf, small)
    result = update_config_file(cf, {}, remove=list(small))
    assert result == {}


# ---------------------------------------------------------------------------
# Concurrency -- the incident regression test
# ---------------------------------------------------------------------------

def test_concurrent_writers_keep_all_keys(tmp_path):
    cf = tmp_path / "config.json"
    seed = {f"seed{i}": i for i in range(10)}
    _write(cf, seed)
    n_threads = 8
    rounds = 25
    barrier = threading.Barrier(n_threads)
    errors = []

    def worker(idx):
        try:
            barrier.wait(timeout=10)
            for r in range(rounds):
                update_config_file(cf, {f"writer{idx}": r})
        except Exception as e:  # pragma: no cover - reported via assertion
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    final = json.loads(cf.read_text())
    for k, v in seed.items():
        assert final[k] == v
    for i in range(n_threads):
        assert final[f"writer{i}"] == rounds - 1
    assert len(final) == len(seed) + n_threads
    assert not cf.with_suffix(".json.tmp").exists()


def test_explicit_lock_is_honoured(tmp_path):
    cf = tmp_path / "config.json"
    _write(cf, {"a": 1})
    lock = threading.Lock()
    lock.acquire()
    done = threading.Event()

    def writer():
        update_config_file(cf, {"b": 2}, lock=lock)
        done.set()

    t = threading.Thread(target=writer)
    t.start()
    # The writer must block while we hold the lock.
    assert not done.wait(0.3)
    assert json.loads(cf.read_text()) == {"a": 1}
    lock.release()
    assert done.wait(5)
    t.join(timeout=5)
    assert json.loads(cf.read_text()) == {"a": 1, "b": 2}


def test_lock_is_released_after_refusal(tmp_path):
    cf = tmp_path / "config.json"
    cf.write_text("not json")
    lock = threading.Lock()
    with pytest.raises(ConfigWriteRefused):
        update_config_file(cf, {"x": 1}, lock=lock)
    assert lock.acquire(blocking=False)
    lock.release()


def test_updates_none_is_a_noop_rewrite(tmp_path):
    cf = tmp_path / "config.json"
    _write(cf, {"a": 1})
    assert update_config_file(cf, None) == {"a": 1}


# ---------------------------------------------------------------------------
# Write-forbidden paths (uo8m: tests/conftest.py forbids the real config.json)
# ---------------------------------------------------------------------------

def test_forbidden_path_refuses_write_and_leaves_file_intact(tmp_path, monkeypatch):
    from swarm import config_store
    cfg = tmp_path / "config.json"
    _write(cfg, {"a": 1})
    monkeypatch.setattr(config_store, "_WRITE_FORBIDDEN", {str(cfg.resolve())})
    with pytest.raises(ConfigWriteRefused, match="forbidden"):
        update_config_file(cfg, {"b": 2})
    assert json.loads(cfg.read_text()) == {"a": 1}


def test_real_config_json_is_forbidden_during_tests():
    from pathlib import Path
    from swarm import config_store
    real = Path(__file__).resolve().parent.parent / "config.json"
    assert str(real.resolve()) in config_store._WRITE_FORBIDDEN
