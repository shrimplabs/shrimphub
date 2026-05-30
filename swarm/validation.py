"""
Swarm Validation

Post-task validation logic extracted from swarm/orchestrator.py.
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from swarm import db
from swarm.closure.regressions import upsert_regressions_for_run, resolve_regressions_for_passing_run
from swarm.closure.repair_planning import plan_repair_tasks_for_run
from swarm.branch_intent import branch_intent_metadata, format_branch_intent
from swarm.closure.runs import (
    complete_verification_run,
    create_verification_run,
    describe_verification_guard,
    load_verification_run,
    mark_verification_run_started,
)
from swarm.closure.specs import normalize_project_spec
from swarm.closure.status import derive_closure_status
from swarm.closure.verification import execute_check, execute_ready_check
from swarm.platform import (
    godot_binary_available,
    project_venv_executable,
    resolve_godot_binary,
    shell_quote_command_arg,
)
from swarm.task_mutations import reparent_dependents


CONTROLLER_CONFIG_BLOCKER_PREFIX = "CONTROLLER CONFIGURATION BLOCKER:"


# ---------------------------------------------------------------------------
# Pre-flight baseline validation
# ---------------------------------------------------------------------------
# Before an agent starts, we snapshot which errors already exist in the project.
# After the agent finishes, we only fail if NEW errors were introduced.
# This eliminates false-positive validation cascades caused by pre-existing
# environmental issues (e.g. missing class_name declarations, removed Godot 4
# properties) that the agent cannot fix because they pre-date the task.
#
# Error signatures are normalised so whitespace/line-number drift doesn't break
# comparisons between the pre- and post-agent runs.
# ---------------------------------------------------------------------------

def _normalise_error_line(line: str) -> str:
    """Normalise a single error line to a stable signature for comparison.

    Strips: absolute paths (keep filename only), line numbers, memory addresses,
    leading whitespace, and common noise prefixes so two logically identical
    errors produced by different Godot runs compare equal.
    """
    import hashlib as _hashlib
    # Strip leading/trailing whitespace
    s = line.strip()
    # Drop memory addresses (0x...)
    s = re.sub(r'\b0x[0-9a-fA-F]+\b', '<addr>', s)
    # Normalise absolute paths to basename only
    s = re.sub(r'(?:/[^\s:,]+)+/([^\s:/,]+\.(?:gd|tscn|py|cs|ts|swift|go|rs))', r'\1', s)
    # Drop :NNN line-number suffixes that vary between runs
    s = re.sub(r':\d+(?::\d+)?(?=\s|$|,)', '', s)
    return s


def _extract_error_signatures(output: str, project_type: str) -> list[str]:
    """Extract normalised error signatures from raw validator output.

    Returns a list of stable strings that can be set-diffed between two runs.
    Filters out known noise lines that are always present regardless of code
    correctness (e.g. Godot renderer warnings, RID allocations).
    """
    _NOISE_PATTERNS = [
        r"RID allocations",
        r"PN18TextServer",
        r"TextServer: Primary interface",
        r"Godot Engine v",
        r"^$",                      # blank lines
        r"^\s+$",                   # whitespace-only lines
        r"All scripts OK",
        r"All scenes OK",
        r"Main scene startup OK",
    ]
    sigs: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Only capture lines that look like errors/warnings
        is_error = any(marker in line for marker in (
            "ERROR:", "SCRIPT ERROR:", "SCENE ERROR:", "FAILED", "Error:", "error:",
        ))
        if not is_error:
            continue
        # Drop known noise
        if any(re.search(pat, line) for pat in _NOISE_PATTERNS):
            continue
        sigs.append(_normalise_error_line(line))
    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for s in sigs:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def capture_validation_baseline(
    project: str,
    task_id: str,
    worktree_path: Optional[Path] = None,
) -> list[str]:
    """Run validation and capture the current error signatures as a baseline.

    The baseline is stored in task metadata under ``validation_baseline`` so
    post-task validation can diff against it.  Returns the list of signatures
    (may be empty if project is clean).

    Safe to call even if validation itself fails — errors are caught and an
    empty baseline is returned (conservative: no baseline means all errors are
    treated as new).
    """
    try:
        failed, output = _post_task_validation_in_worktree(
            project, task_id, worktree_path=worktree_path, timeout=60
        )
        # Determine project type for signature extraction
        from swarm import orchestrator as _orch
        project_path = worktree_path if worktree_path is not None else (_orch.WORKSPACE / project)
        if (project_path / "project.godot").exists():
            ptype = "godot"
        elif (project_path / "requirements.txt").exists() or (project_path / "pyproject.toml").exists():
            ptype = "python"
        else:
            ptype = "unknown"
        sigs = _extract_error_signatures(output, ptype)
        # Persist to task metadata
        task = db.task_get(task_id)
        if task:
            meta = dict(task.get("metadata") or {})
            meta["validation_baseline"] = sigs
            meta["validation_baseline_captured"] = True
            db.task_update(task_id, {"metadata": meta})
        print(f"[PreFlight] Baseline captured for {project} {task_id[:8]}: {len(sigs)} pre-existing error(s)")
        return sigs
    except Exception as e:
        print(f"[PreFlight] WARNING: baseline capture failed for {project} {task_id[:8]}: {e}")
        return []


def filter_new_errors(
    output: str,
    baseline_sigs: list[str],
    project_type: str = "unknown",
) -> tuple[list[str], list[str]]:
    """Diff post-agent validation output against the baseline.

    Returns (new_errors, inherited_errors) where:
    - new_errors: signatures present after agent run but NOT in baseline → real failures
    - inherited_errors: signatures in both baseline and current output → pre-existing, not charged to agent
    """
    baseline_set = set(baseline_sigs)
    current_sigs = _extract_error_signatures(output, project_type)
    new_errors = [s for s in current_sigs if s not in baseline_set]
    inherited = [s for s in current_sigs if s in baseline_set]
    return new_errors, inherited


def is_controller_config_blocker(error_output: str | None) -> bool:
    return bool(error_output and error_output.startswith(CONTROLLER_CONFIG_BLOCKER_PREFIX))


def _check_autoloads(project_path: Path) -> List[str]:
    """Parse project.godot [autoload] section and verify every referenced file exists."""
    errors: List[str] = []
    godot_proj = project_path / "project.godot"
    if not godot_proj.exists():
        return errors
    try:
        content = godot_proj.read_text()
    except Exception:
        return errors
    in_autoload = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_autoload = stripped == "[autoload]"
            continue
        if not in_autoload or "=" not in stripped:
            continue
        _, value = stripped.split("=", 1)
        value = value.strip().strip('"')
        if value.startswith("*"):
            value = value[1:]
        if value.startswith("res://"):
            rel = value[len("res://"):]
            if not (project_path / rel).exists():
                errors.append(f"Missing autoload: {value}")
    return errors


def _get_main_scene(project_path: Path) -> Optional[str]:
    """Return the res:// path of the main scene from project.godot, or None."""
    godot_proj = project_path / "project.godot"
    if not godot_proj.exists():
        return None
    try:
        content = godot_proj.read_text()
        m = re.search(r'run/main_scene="([^"]+)"', content)
        return m.group(1) if m else None
    except Exception:
        return None


def _post_task_validation_in_worktree(
    project: str,
    task_id: str,
    worktree_path: Optional[Path] = None,
    timeout: int = 60,
) -> Tuple[bool, str]:
    """
    Run validation against the given path (worktree or main project).

    Returns (validation_failed: bool, error_output: str).
    Does NOT spawn a bug task -- that is the caller's responsibility.

    If worktree_path is None, falls back to WORKSPACE / project (main project dir).
    """
    # Import here to avoid circular imports; orchestrator sets WORKSPACE at runtime
    from swarm import orchestrator as _orch
    WORKSPACE = _orch.WORKSPACE

    project_path = worktree_path if worktree_path is not None else (WORKSPACE / project)

    if not project_path.exists():
        err = f"Validation skipped: project path does not exist: {project_path}"
        print(f"[PostValidation] {err}")
        return True, err

    def _has_python_sources(path: Path) -> bool:
        candidates = [
            path,
            path / "src",
            path / "tests",
        ]
        for candidate in candidates:
            if not candidate.exists() or not candidate.is_dir():
                continue
            try:
                if any(candidate.glob("*.py")):
                    return True
            except Exception:
                continue
        return False

    def _detect_type():
        # .swarm_validate always wins -- project declares its own validator
        if (project_path / ".swarm_validate").exists():
            return "custom"
        if (project_path / "project.godot").exists():
            return "godot"
        if (project_path / "requirements.txt").exists() or (project_path / "pyproject.toml").exists():
            return "python"
        if _has_python_sources(project_path):
            return "python"
        # Swift / iOS
        if (
            (project_path / "Package.swift").exists()
            or any(project_path.glob("*.xcodeproj"))
            or any(project_path.glob("*.xcworkspace"))
        ):
            return "swift"
        # Unity
        if (project_path / "ProjectSettings" / "ProjectVersion.txt").exists():
            return "unity"
        # Node / TypeScript
        if (project_path / "package.json").exists():
            return "node"
        # Rust
        if (project_path / "Cargo.toml").exists():
            return "rust"
        # Go
        if (project_path / "go.mod").exists():
            return "go"
        # C# (standalone -- not Unity)
        if any(project_path.glob("*.csproj")) or any(project_path.glob("*.sln")):
            return "csharp"
        # C / C++
        if (
            (project_path / "CMakeLists.txt").exists()
            or any(project_path.glob("*.vcxproj"))
            or (project_path / "Makefile").exists()
            or (project_path / "makefile").exists()
        ):
            return "cpp"
        return "unknown"

    project_type = _detect_type()
    validation_failed = False
    error_output = ""

    task_record = db.task_get(task_id)
    task_type = task_record.get("type", "") if task_record else ""

    if project_type == "godot":
        _GODOT = resolve_godot_binary()
        if not godot_binary_available(_GODOT):
            error_output = (
                f"{CONTROLLER_CONFIG_BLOCKER_PREFIX} Godot executable is not configured or "
                f"discoverable by the controller. Set config.json `godot_path` or GODOT_PATH "
                f"before running Godot validation. Resolved command was: {_GODOT!r}."
            )
            print(f"[PostValidation] {error_output}")
            return True, error_output
        _GODOT_CMD = shell_quote_command_arg(_GODOT)

        if worktree_path is not None:
            main_godot_cache = WORKSPACE / project / ".godot"
            wt_godot_cache = worktree_path / ".godot"
            if main_godot_cache.exists() and not wt_godot_cache.exists():
                try:
                    import shutil as _shutil
                    _shutil.copytree(str(main_godot_cache), str(wt_godot_cache))
                    print(f"[PostValidation] Seeded .godot cache into worktree {worktree_path.name}")
                except Exception as _ce:
                    print(f"[PostValidation] WARNING: could not seed .godot cache: {_ce}")

        # Ensure .godot/ class cache exists so class_name types resolve during load().
        # New projects that have never been opened in the editor lack this cache.
        # Use --headless --import instead of --headless --editor --quit:
        # --import runs only the asset import pipeline without the full editor UI loop,
        # which avoids the macOS NSAlert "no main scene" native dialog that --editor
        # triggers before the headless display driver can suppress it.
        _godot_cache = project_path / ".godot"
        if not _godot_cache.exists():
            try:
                _init_result = subprocess.run(
                    f"{_GODOT_CMD} --headless --import",
                    shell=True, cwd=project_path,
                    capture_output=True, text=True, timeout=120,
                )
                if _godot_cache.exists():
                    print(f"[PostValidation] Initialised .godot cache for {project}")
                else:
                    print(f"[PostValidation] WARNING: .godot cache init did not create cache (exit={_init_result.returncode})")
            except Exception as _ie:
                print(f"[PostValidation] WARNING: .godot cache init failed: {_ie}")

        # Step 1: Script parse check
        if not validation_failed:
            _SCRIPT_VALIDATOR = '''\
extends SceneTree
func _initialize():
    var autoload_names: Array[String] = []
    var proj_file = FileAccess.open("res://project.godot", FileAccess.READ)
    if proj_file:
        var in_autoload = false
        while not proj_file.eof_reached():
            var line = proj_file.get_line().strip_edges()
            if line == "[autoload]":
                in_autoload = true
            elif line.begins_with("[") and line.ends_with("]"):
                in_autoload = false
            elif in_autoload and "=" in line:
                autoload_names.append(line.split("=")[0].strip_edges())
        proj_file.close()
    for _i in range(3):
        _load_class_names("res://")
    var errors: Array[String] = []
    _scan("res://", errors, autoload_names)
    if errors.size() > 0:
        for e in errors: print("SCRIPT ERROR: " + e)
        quit(1)
    else:
        print("All scripts OK"); quit(0)
func _load_class_names(path: String) -> void:
    var dir = DirAccess.open(path)
    if dir == null: return
    dir.list_dir_begin()
    var f = dir.get_next()
    while f != "":
        if dir.current_is_dir() and not f.begins_with(".") and f != "addons" and f != "tests" and f != "test":
            _load_class_names(path + f + "/")
        elif f.ends_with(".gd"):
            var full_path = path + f
            var src = FileAccess.get_file_as_string(full_path)
            if "class_name " in src:
                load(full_path)
        f = dir.get_next()
func _scan(path: String, errors: Array[String], autoload_names: Array[String]) -> void:
    var dir = DirAccess.open(path)
    if dir == null: return
    dir.list_dir_begin()
    var f = dir.get_next()
    while f != "":
        if dir.current_is_dir() and not f.begins_with(".") and f != "addons" and f != "tests" and f != "test":
            _scan(path + f + "/", errors, autoload_names)
        elif f.ends_with(".gd"):
            var full_path = path + f
            var s = load(full_path)
            if s == null:
                if not _references_autoload(full_path, autoload_names):
                    errors.append("Failed to load: " + full_path)
        f = dir.get_next()
func _references_autoload(path: String, autoload_names: Array[String]) -> bool:
    if autoload_names.is_empty(): return false
    var source = FileAccess.get_file_as_string(path)
    if source.is_empty(): return false
    for aname in autoload_names:
        if aname in source: return true
    return false
'''
            validator_path = project_path / "_swarm_check.gd"
            try:
                validator_path.write_text(_SCRIPT_VALIDATOR)
                result = subprocess.run(
                    f"{_GODOT_CMD} --headless --path . --script res://_swarm_check.gd --quit",
                    shell=True, cwd=project_path,
                    capture_output=True, text=True, timeout=120,
                )
                combined = (result.stdout or "") + (result.stderr or "")
                # Always scan for real parse errors regardless of exit code.
                # Godot exits non-zero for resource-leak warnings at shutdown
                # ("N resources still in use at exit") even when scripts parsed fine.
                # Only fail on actual ERROR:/SCRIPT ERROR: lines.
                bad = [ln for ln in combined.splitlines()
                       if ("ERROR:" in ln or "SCRIPT ERROR:" in ln)
                       and "RID allocations" not in ln
                       and "PN18TextServer" not in ln
                       and "resources still in use" not in ln]
                if bad:
                    validation_failed = True
                    error_output = "Script parse errors:\n" + "\n".join(bad)
                    print(f"[PostValidation] Script check FAILED for {project}")
                elif result.returncode != 0:
                    # Non-zero exit but no real errors — likely resource cleanup noise, pass.
                    print(f"[PostValidation] Script check passed for {project} (exit={result.returncode}, no parse errors)")
                else:
                    print(f"[PostValidation] Script check passed for {project}")
            except Exception as e:
                print(f"[PostValidation] Script check error: {e}")
            finally:
                try:
                    validator_path.unlink()
                except Exception:
                    pass

        # Step 2: Autoload file existence
        if not validation_failed:
            autoload_errors = _check_autoloads(project_path)
            if autoload_errors:
                validation_failed = True
                error_output = "Autoload validation failed:\n" + "\n".join(autoload_errors)
                print(f"[PostValidation] Autoload check FAILED for {project}: {autoload_errors}")
            else:
                print(f"[PostValidation] Autoload check passed for {project}")

        # Step 3: Scene load check
        if not validation_failed:
            _SCENE_VALIDATOR = '''\
extends SceneTree
func _initialize():
    var errors = []
    _scan("res://", errors)
    if errors.size() > 0:
        for e in errors: print("SCENE ERROR: " + e)
        quit(1)
    else:
        print("All scenes OK"); quit(0)
func _scan(path: String, errors: Array) -> void:
    var dir = DirAccess.open(path)
    if dir == null: return
    dir.list_dir_begin()
    var f = dir.get_next()
    while f != "":
        if dir.current_is_dir() and not f.begins_with(".") and f != "addons":
            _scan(path + f + "/", errors)
        elif f.ends_with(".tscn"):
            var s = load(path + f)
            if s == null: errors.append("Failed to load scene: " + path + f)
        f = dir.get_next()
'''
            scene_check_path = project_path / "_swarm_scene_check.gd"
            try:
                scene_check_path.write_text(_SCENE_VALIDATOR)
                result = subprocess.run(
                    f"{_GODOT_CMD} --headless --path . --script res://_swarm_scene_check.gd --quit",
                    shell=True, cwd=project_path,
                    capture_output=True, text=True, timeout=60,
                )
                combined = (result.stdout or "") + (result.stderr or "")
                if result.returncode != 0:
                    validation_failed = True
                    error_output = f"Scene load errors:\n{combined}"
                else:
                    bad = [ln for ln in combined.splitlines()
                           if "SCENE ERROR:" in ln or "SCRIPT ERROR:" in ln]
                    if bad:
                        validation_failed = True
                        error_output = "Scene load errors:\n" + "\n".join(bad)
                        print(f"[PostValidation] Scene load check FAILED for {project}: {bad}")
                    else:
                        print(f"[PostValidation] Scene load check passed for {project}")
            except Exception as e:
                print(f"[PostValidation] Scene load check error: {e}")
            finally:
                try:
                    scene_check_path.unlink()
                except Exception:
                    pass

        # Step 4: Main scene instantiate check
        main_scene = None
        if not validation_failed:
            main_scene = _get_main_scene(project_path)
            if main_scene:
                _MAIN_VALIDATOR = f'''\
extends SceneTree
func _initialize():
    var packed = load("{main_scene}")
    if packed == null:
        print("SCENE ERROR: Could not load main scene: {main_scene}")
        quit(1)
    var instance = packed.instantiate()
    if instance == null:
        print("SCENE ERROR: Could not instantiate main scene: {main_scene}")
        quit(1)
    root.add_child(instance)
    instance.free()
    print("Main scene startup OK: {main_scene}")
    quit(0)
'''
                main_check_path = project_path / "_swarm_main_check.gd"
                try:
                    main_check_path.write_text(_MAIN_VALIDATOR)
                    result = subprocess.run(
                        f"{_GODOT_CMD} --headless --path . --script res://_swarm_main_check.gd --quit",
                        shell=True, cwd=project_path,
                        capture_output=True, text=True, timeout=60,
                    )
                    combined = (result.stdout or "") + (result.stderr or "")
                    if result.returncode != 0:
                        validation_failed = True
                        error_output = f"Main scene instantiate failed ({main_scene}):\n{combined}"
                    else:
                        bad = [
                            ln for ln in combined.splitlines()
                            if "SCENE ERROR:" in ln or "SCRIPT ERROR:" in ln
                        ]
                        if bad:
                            validation_failed = True
                            error_output = "Main scene startup errors:\n" + "\n".join(bad)
                            print(f"[PostValidation] Main scene FAILED for {project}: {bad}")
                        else:
                            print(f"[PostValidation] Main scene startup OK for {project}: {main_scene}")
                except Exception as e:
                    print(f"[PostValidation] Main scene check error: {e}")
                finally:
                    try:
                        main_check_path.unlink()
                    except Exception:
                        pass
            else:
                print(f"[PostValidation] No main scene set in project.godot for {project} -- skipping")

        # Step 5: Editor startup check. This catches errors that appear as soon
        # as the project opens in the Godot editor, after autoloads and project
        # settings are initialized.
        if not validation_failed and main_scene:
            editor_cmd = f"{_GODOT_CMD} --headless --path . --editor --quit"
            try:
                result = subprocess.run(
                    editor_cmd, shell=True, cwd=project_path,
                    capture_output=True, text=True, timeout=90,
                )
                combined = (result.stdout or "") + (result.stderr or "")
                bad = [
                    ln for ln in combined.splitlines()
                    if ("SCRIPT ERROR:" in ln or "ERROR:" in ln)
                    and "RID allocations" not in ln
                    and "PN18TextServer" not in ln
                    and "TextServer: Primary interface" not in ln
                ]
                if result.returncode != 0 or bad:
                    validation_failed = True
                    if bad:
                        error_output = "Godot editor startup errors:\n" + "\n".join(bad)
                    else:
                        error_output = f"Godot editor startup failed (exit {result.returncode}):\n{combined[-3000:]}"
                    print(f"[PostValidation] Editor startup FAILED for {project}")
                else:
                    print(f"[PostValidation] Editor startup OK for {project}")
            except Exception as e:
                print(f"[PostValidation] Editor startup check error: {e}")

        # Step 6: GUT unit tests
        if not validation_failed and task_type not in ("manager", "project_create", "qa", "hybrid_qa", "art_pass"):
            gut_addon = project_path / "addons" / "gut"
            tests_dir = next(
                (d for d in [project_path / "test", project_path / "tests"] if d.exists()),
                None,
            )
            from swarm.godot_bootstrap import _gut_installation_complete
            if gut_addon.exists() and _gut_installation_complete(gut_addon) and tests_dir is not None:
                gut_dir = "res://" + tests_dir.name
                gut_cmd = (
                    f"{_GODOT_CMD} --headless"
                    f" --path ."
                    f" --script res://addons/gut/gut_cmdln.gd"
                    f" -- -gdir={gut_dir} -gexit"
                )
                try:
                    result = subprocess.run(
                        gut_cmd, shell=True, cwd=project_path,
                        capture_output=True, text=True, timeout=300,
                    )
                    combined = (result.stdout or "") + (result.stderr or "")
                    if result.returncode != 0:
                        validation_failed = True
                        error_output = f"GUT tests failed:\n{combined[-3000:]}"
                        print(f"[PostValidation] GUT tests FAILED for {project} {task_id}")
                    else:
                        fail_lines = [
                            ln for ln in combined.splitlines()
                            if re.search(r"\bfailed\b", ln, re.IGNORECASE)
                            and "nothing failed" not in ln.lower()
                            and not ln.strip().startswith("[Passed]")
                            and not re.match(r"\s*WARNING:", ln)
                        ]
                        if fail_lines:
                            validation_failed = True
                            error_output = "GUT tests reported failures:\n" + "\n".join(fail_lines)
                            print(f"[PostValidation] GUT output failures for {project} {task_id}")
                        else:
                            print(f"[PostValidation] GUT tests passed for {project} {task_id}")
                except Exception as e:
                    print(f"[PostValidation] GUT run error for {project}: {e}")
            elif gut_addon.exists() and tests_dir is None:
                print(f"[PostValidation] GUT installed but no test/ or tests/ dir for {project} -- skipping")

    elif project_type == "python":
        venv_pytest = project_venv_executable(project_path, "pytest")
        venv_python = project_venv_executable(project_path, "python")
        tests_dir = next(
            (d for d in [project_path / "tests", project_path / "test"] if d.exists()),
            None,
        )
        pytest_available = False
        if tests_dir is not None:
            if venv_pytest.exists():
                try:
                    result = subprocess.run(
                        [str(venv_pytest), "--ignore=tests/test_dashboard.py", "--ignore=tests/test_agent_runtime.py", "--tb=short", "-q"],
                        cwd=project_path, capture_output=True, text=True, timeout=300,
                    )
                    pytest_available = True
                    if result.returncode == 5:
                        # Exit code 5 = "no tests collected" -- not a failure, project just has no tests
                        print(f"[PostValidation] pytest: no tests found for {project}")
                    elif result.returncode != 0:
                        validation_failed = True
                        error_output = (result.stdout or "") + (result.stderr or "")
                        print(f"[PostValidation] pytest FAILED for {project} {task_id}")
                except Exception as e:
                    validation_failed = True
                    error_output = f"pytest execution error:\n{e}"
                    print(f"[PostValidation] pytest error for {project}: {e}")
            else:
                validation_failed = True
                error_output = (
                    "Python tests exist but no project-local pytest runner is available.\n"
                    "Expected a project-local pytest runner under .venv "
                    "(for example .venv/bin/pytest or .venv/Scripts/pytest.exe)."
                )
                print(f"[PostValidation] pytest unavailable for {project} {task_id} -- tests exist but no local runner")

        if not pytest_available and not validation_failed:
            cmd = str(venv_python) if venv_python.exists() else sys.executable
            # Try *.py first (root-level Python projects), then src/*.py (src layout projects)
            patterns = ["*.py", "src/*.py"]
            validation_passed = False
            error_output = ""
            for pattern in patterns:
                try:
                    result = subprocess.run(
                        f"{cmd} -m py_compile {pattern}", shell=True, cwd=project_path,
                        capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode == 0:
                        validation_passed = True
                        break
                    elif "No such file" not in result.stderr:
                        # Real error, not just no files matching
                        validation_failed = True
                        error_output = result.stderr or result.stdout
                        break
                    # else: try next pattern
                except Exception as e:
                    error_output = str(e)
                    break
            if not validation_passed and not validation_failed:
                # All patterns failed with "no such file" -- no .py files found, skip cleanly
                print(f"[PostValidation] No Python files found for {project} -- skipping py_compile")

    elif project_type == "custom":
        # .swarm_validate: one line, shell command, exit code = result
        try:
            cmd_text = (project_path / ".swarm_validate").read_text().strip()
            if cmd_text:
                result = subprocess.run(
                    cmd_text, shell=True, cwd=project_path,
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    validation_failed = True
                    error_output = (result.stdout or "") + (result.stderr or "")
                    print(f"[PostValidation] .swarm_validate FAILED for {project} {task_id}")
                else:
                    print(f"[PostValidation] .swarm_validate passed for {project} {task_id}")
            else:
                print(f"[PostValidation] .swarm_validate is empty for {project} -- skipping")
        except Exception as e:
            print(f"[PostValidation] .swarm_validate error for {project}: {e}")

    elif project_type == "swift":
        # Collect all .swift files excluding .build/
        swift_files = [
            str(f.relative_to(project_path))
            for f in project_path.rglob("*.swift")
            if ".build" not in f.parts and ".swiftpm" not in f.parts
        ]
        if swift_files:
            try:
                result = subprocess.run(
                    ["swiftc", "-parse"] + swift_files,
                    cwd=project_path, capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    validation_failed = True
                    error_output = (result.stdout or "") + (result.stderr or "")
                    print(f"[PostValidation] swiftc -parse FAILED for {project} {task_id}")
                else:
                    print(f"[PostValidation] swiftc -parse passed for {project} {task_id} ({len(swift_files)} files)")
            except FileNotFoundError:
                print(f"[PostValidation] swiftc not found for {project} -- skipping Swift validation")
            except Exception as e:
                print(f"[PostValidation] Swift validation error for {project}: {e}")
        else:
            print(f"[PostValidation] No .swift files found for {project} -- skipping")

    elif project_type == "unity":
        # Preferred: dotnet build on the generated .csproj (Unity generates these in project root)
        # Fallback: Unity's bundled mcs --parse for syntax checking
        # Unity editor generates Assembly-CSharp.csproj when project is opened/synced.
        csproj_files = list(project_path.glob("*.csproj"))
        if csproj_files:
            try:
                result = subprocess.run(
                    ["dotnet", "build", str(csproj_files[0]), "--nologo", "-v", "quiet"],
                    cwd=project_path, capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    validation_failed = True
                    error_output = (result.stdout or "") + (result.stderr or "")
                    print(f"[PostValidation] dotnet build FAILED for {project} {task_id}")
                else:
                    print(f"[PostValidation] dotnet build passed for {project} {task_id}")
            except FileNotFoundError:
                print(f"[PostValidation] dotnet not found for {project} -- falling back to mcs --parse")
                csproj_files = []  # fall through to mcs below
            except Exception as e:
                print(f"[PostValidation] Unity dotnet build error for {project}: {e}")

        if not csproj_files and not validation_failed:
            # No .csproj yet (project not opened in editor) -- use mcs --parse for syntax check.
            # Prefer Unity's bundled mcs over system mono; find highest installed editor version.
            unity_mcs = None
            _unity_hub_candidates = [
                # macOS
                Path("/Applications/Unity/Hub/Editor"),
                # Windows
                Path("C:/Program Files/Unity/Hub/Editor"),
                Path("C:/Program Files (x86)/Unity/Hub/Editor"),
                # Linux
                Path.home() / "Unity/Hub/Editor",
                Path("/opt/unity/hub/editor"),
                Path("/opt/Unity/Hub/Editor"),
            ]
            _mcs_rel = {
                "darwin": "Unity.app/Contents/Resources/Scripting/MonoBleedingEdge/bin/mcs",
                "win32": "Editor/Data/MonoBleedingEdge/bin/mcs.bat",
                "linux": "Editor/Data/MonoBleedingEdge/bin/mcs",
            }
            import platform as _platform
            _sys = _platform.system().lower()
            _mcs_suffix = (
                _mcs_rel["darwin"] if _sys == "darwin"
                else _mcs_rel["win32"] if _sys == "windows"
                else _mcs_rel["linux"]
            )
            for _hub in _unity_hub_candidates:
                if not _hub.exists():
                    continue
                for editor_dir in sorted(_hub.iterdir(), reverse=True):
                    candidate = editor_dir / _mcs_suffix
                    if candidate.exists():
                        unity_mcs = str(candidate)
                        break
                if unity_mcs:
                    break
            mcs_cmd = unity_mcs or "mcs"

            cs_files = list(project_path.rglob("Assets/**/*.cs"))
            if not cs_files:
                cs_files = [f for f in project_path.rglob("*.cs") if "Library" not in f.parts and "Temp" not in f.parts]
            if cs_files:
                try:
                    result = subprocess.run(
                        [mcs_cmd, "--parse"] + [str(f) for f in cs_files[:200]],
                        cwd=project_path, capture_output=True, text=True, timeout=60,
                    )
                    if result.returncode != 0:
                        validation_failed = True
                        error_output = (result.stdout or "") + (result.stderr or "")
                        print(f"[PostValidation] mcs --parse FAILED for {project} {task_id}")
                    else:
                        src = "Unity bundled mcs" if unity_mcs else "system mcs"
                        print(f"[PostValidation] mcs --parse passed for {project} {task_id} ({len(cs_files)} files, {src})")
                except FileNotFoundError:
                    print(f"[PostValidation] mcs not found for {project} -- skipping Unity validation")
                except Exception as e:
                    print(f"[PostValidation] Unity mcs --parse error for {project}: {e}")
            else:
                print(f"[PostValidation] No .cs files found for {project} -- skipping Unity validation")

    elif project_type == "node":
        # Use tsc --noEmit if tsconfig.json exists, else node --check on entry point
        tsconfig = project_path / "tsconfig.json"
        if tsconfig.exists():
            try:
                # Prefer local tsc; fall back to global
                local_tsc = project_path / "node_modules" / ".bin" / "tsc"
                tsc_cmd = str(local_tsc) if local_tsc.exists() else "tsc"
                result = subprocess.run(
                    [tsc_cmd, "--noEmit"],
                    cwd=project_path, capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    validation_failed = True
                    error_output = (result.stdout or "") + (result.stderr or "")
                    print(f"[PostValidation] tsc --noEmit FAILED for {project} {task_id}")
                else:
                    print(f"[PostValidation] tsc --noEmit passed for {project} {task_id}")
            except FileNotFoundError:
                print(f"[PostValidation] tsc not found for {project} -- skipping TypeScript validation")
            except Exception as e:
                print(f"[PostValidation] Node/TS validation error for {project}: {e}")
        else:
            # Plain JS -- check main entry if declared in package.json
            try:
                import json as _json
                pkg = _json.loads((project_path / "package.json").read_text())
                main = pkg.get("main") or pkg.get("module")
                if main and (project_path / main).exists():
                    result = subprocess.run(
                        ["node", "--check", main],
                        cwd=project_path, capture_output=True, text=True, timeout=30,
                    )
                    if result.returncode != 0:
                        validation_failed = True
                        error_output = (result.stdout or "") + (result.stderr or "")
                        print(f"[PostValidation] node --check FAILED for {project} {task_id}")
                    else:
                        print(f"[PostValidation] node --check passed for {project} {task_id}: {main}")
                else:
                    print(f"[PostValidation] No tsconfig or main entry for {project} -- skipping Node validation")
            except Exception as e:
                print(f"[PostValidation] Node validation error for {project}: {e}")

    elif project_type == "rust":
        try:
            result = subprocess.run(
                ["cargo", "check", "--quiet"],
                cwd=project_path, capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                validation_failed = True
                error_output = (result.stdout or "") + (result.stderr or "")
                print(f"[PostValidation] cargo check FAILED for {project} {task_id}")
            else:
                print(f"[PostValidation] cargo check passed for {project} {task_id}")
        except FileNotFoundError:
            print(f"[PostValidation] cargo not found for {project} -- skipping Rust validation")
        except Exception as e:
            print(f"[PostValidation] Rust validation error for {project}: {e}")

    elif project_type == "go":
        try:
            result = subprocess.run(
                ["go", "build", "./..."],
                cwd=project_path, capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                validation_failed = True
                error_output = (result.stdout or "") + (result.stderr or "")
                print(f"[PostValidation] go build FAILED for {project} {task_id}")
            else:
                print(f"[PostValidation] go build passed for {project} {task_id}")
        except FileNotFoundError:
            print(f"[PostValidation] go not found for {project} -- skipping Go validation")
        except Exception as e:
            print(f"[PostValidation] Go validation error for {project}: {e}")

    elif project_type == "csharp":
        # Prefer solution file; fall back to first .csproj found
        sln_files = list(project_path.glob("*.sln"))
        csproj_files = list(project_path.glob("*.csproj"))
        build_target = str(sln_files[0]) if sln_files else (str(csproj_files[0]) if csproj_files else None)
        if build_target:
            try:
                result = subprocess.run(
                    ["dotnet", "build", build_target, "--nologo", "-v", "quiet"],
                    cwd=project_path, capture_output=True, text=True, timeout=180,
                )
                if result.returncode != 0:
                    validation_failed = True
                    error_output = (result.stdout or "") + (result.stderr or "")
                    print(f"[PostValidation] dotnet build FAILED for {project} {task_id}")
                else:
                    print(f"[PostValidation] dotnet build passed for {project} {task_id}")
            except FileNotFoundError:
                print(f"[PostValidation] dotnet not found for {project} -- skipping C# validation")
            except Exception as e:
                print(f"[PostValidation] C# validation error for {project}: {e}")
        else:
            print(f"[PostValidation] No .sln or .csproj found for {project} -- skipping C# validation")

    elif project_type == "cpp":
        cmake = project_path / "CMakeLists.txt"
        build_dir = project_path / "build"
        try:
            if cmake.exists():
                if build_dir.exists():
                    # Already configured -- just build
                    result = subprocess.run(
                        ["cmake", "--build", str(build_dir), "--", "-j4"],
                        cwd=project_path, capture_output=True, text=True, timeout=300,
                    )
                else:
                    # Configure only -- catches most syntax and link errors without a full build
                    result = subprocess.run(
                        ["cmake", "-S", ".", "-B", "build"],
                        cwd=project_path, capture_output=True, text=True, timeout=120,
                    )
                if result.returncode != 0:
                    validation_failed = True
                    error_output = (result.stdout or "") + (result.stderr or "")
                    print(f"[PostValidation] cmake FAILED for {project} {task_id}")
                else:
                    print(f"[PostValidation] cmake passed for {project} {task_id}")
            elif (project_path / "Makefile").exists() or (project_path / "makefile").exists():
                # Dry run -- checks syntax without actually compiling
                result = subprocess.run(
                    ["make", "-n"],
                    cwd=project_path, capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    validation_failed = True
                    error_output = (result.stdout or "") + (result.stderr or "")
                    print(f"[PostValidation] make -n FAILED for {project} {task_id}")
                else:
                    print(f"[PostValidation] make -n passed for {project} {task_id}")
            elif any(project_path.glob("*.vcxproj")):
                # MSBuild (Windows) -- dotnet build handles .vcxproj on modern toolchains
                vcxproj = next(project_path.glob("*.vcxproj"))
                result = subprocess.run(
                    ["dotnet", "build", str(vcxproj), "--nologo", "-v", "quiet"],
                    cwd=project_path, capture_output=True, text=True, timeout=180,
                )
                if result.returncode != 0:
                    validation_failed = True
                    error_output = (result.stdout or "") + (result.stderr or "")
                    print(f"[PostValidation] msbuild FAILED for {project} {task_id}")
                else:
                    print(f"[PostValidation] msbuild passed for {project} {task_id}")
        except FileNotFoundError as e:
            print(f"[PostValidation] C++ build tool not found for {project} ({e}) -- skipping")
        except Exception as e:
            print(f"[PostValidation] C++ validation error for {project}: {e}")

    else:
        # unknown -- skip cleanly, never false-positive
        print(f"[PostValidation] Unknown project type for {project} -- skipping validation")

    if validation_failed and error_output:
        print(f"[PostValidation] Failed for {project} (path={project_path.name})")
    else:
        print(f"[PostValidation] Passed for {project} {task_id} (path={project_path.name})")

    return validation_failed, error_output


def _post_task_validation(project: str, task_id: str, timeout: int = 60, original_task: Optional[dict] = None):
    """
    Backwards-compatible wrapper: run validation against the main project path and
    spawn a bug task on failure.  Used for non-worktree paths (Python projects,
    USE_WORKTREES=False).
    """
    validation_failed, error_output = _post_task_validation_in_worktree(
        project, task_id, worktree_path=None, timeout=timeout
    )
    if validation_failed and error_output:
        if is_controller_config_blocker(error_output):
            print(f"[PostValidation] Failed for {project} -- controller configuration blocker; not spawning project bug")
            return
        print(f"[PostValidation] Failed for {project} -- spawning bug task")
        _spawn_validation_bug_task(
            project,
            task_id,
            error_output,
            original_task=original_task or db.task_get(task_id),
        )


def _project_closure_checks(spec: dict) -> list[dict]:
    checks: list[dict] = []
    profile = spec.get("profile")
    def _is_executable(check: dict) -> bool:
        check_type = check.get("type")
        if check_type == "command":
            return isinstance(check.get("command"), str) and bool(check.get("command").strip())
        if check_type == "http":
            return isinstance(check.get("url"), str) and bool(check.get("url").strip())
        if check_type == "godot_scene":
            return True
        return False

    boot = spec.get("boot") or {}
    ready_check = boot.get("ready_check")
    if isinstance(ready_check, dict) and ready_check.get("type") in {"command", "http"} and _is_executable(ready_check):
        checks.append({"category": "boot", **dict(ready_check)})

    verification = spec.get("verification") or {}
    unit_test_command = verification.get("unit_test_command")
    if isinstance(unit_test_command, str) and unit_test_command.strip():
        checks.append({
            "id": "unit-tests",
            "category": "tests",
            "type": "command",
            "command": unit_test_command.strip(),
        })

    smoke_checks = [smoke for smoke in (verification.get("smoke_checks") or []) if isinstance(smoke, dict)]
    if profile == "godot" and not any((smoke.get("type") == "godot_scene") for smoke in smoke_checks):
        smoke_checks.append({"id": "main-scene-runtime", "type": "godot_scene"})

    for smoke in smoke_checks:
        if not isinstance(smoke, dict):
            continue
        if smoke.get("type") not in {"command", "http", "godot_scene"}:
            continue
        if not _is_executable(smoke):
            continue
        checks.append({"category": "smoke", **dict(smoke)})
    return checks


def run_closure_verification(
    project: str,
    trigger_task_id: Optional[str] = None,
    project_path: Optional[Path] = None,
    *,
    run_type: str = "post_task",
) -> Optional[dict]:
    """Run the closure verification checks for a project and persist the VerificationRun.

    Failed runs also flow through regression tracking and bounded repair planning.
    Scheduler policy remains a later concern.
    """
    from swarm import orchestrator as _orch

    project_row = db.project_get(project)
    if not project_row:
        return None

    spec = normalize_project_spec(project_row.get("closure_spec"), project_row.get("profile"))
    checks = _project_closure_checks(spec)
    guard = describe_verification_guard(project, run_type=run_type, trigger_task_id=trigger_task_id)
    if guard["duplicate_run_id"]:
        return load_verification_run(guard["duplicate_run_id"])
    if guard["throttled"] and guard["latest_run_id"]:
        return load_verification_run(guard["latest_run_id"])

    run = create_verification_run(
        project,
        trigger_task_id=trigger_task_id,
        run_type=run_type,
        status="pending",
        metadata={
            "profile": project_row.get("profile"),
            "check_count": len(checks),
        },
    )
    run = mark_verification_run_started(run["id"])

    resolved_project_path = project_path or (_orch.WORKSPACE / project)
    results = {
        "boot_ok": None,
        "tests_ok": None,
        "smoke_ok": None,
        "critical_flows": {},
        "errors": [],
    }
    artifacts = {"checks": []}
    fingerprints: list[str] = []

    smoke_results: list[bool] = []
    critical_flow_ids = {
        flow.get("id")
        for flow in (spec.get("critical_flows") or [])
        if isinstance(flow, dict) and flow.get("id")
    }

    for check in checks:
        if check.get("category") == "boot":
            outcome = execute_ready_check(check, cwd=resolved_project_path)
            artifacts["checks"].append({"category": "boot", "outcome": outcome})
            results["boot_ok"] = outcome["ok"]
            if not outcome["ok"]:
                fingerprints.append(f"boot:{outcome['check_type']}:{outcome['status']}")
                if outcome.get("error"):
                    results["errors"].append(outcome["error"])
            continue

        outcome = execute_check(check, cwd=resolved_project_path)
        category = check.get("category") or "smoke"
        artifacts["checks"].append({"category": category, "id": check.get("id"), "outcome": outcome})
        if category == "tests":
            results["tests_ok"] = outcome["ok"]
        elif category == "smoke":
            smoke_results.append(outcome["ok"])
            check_id = check.get("id")
            if check_id and check_id in critical_flow_ids:
                results["critical_flows"][check_id] = outcome["ok"]
        if not outcome["ok"]:
            fingerprint_prefix = "tests" if category == "tests" else "smoke"
            fingerprints.append(f"{fingerprint_prefix}:{check.get('id') or outcome['check_type']}:{outcome['status']}")
            if outcome.get("error"):
                results["errors"].append(outcome["error"])

    if smoke_results:
        results["smoke_ok"] = all(smoke_results)

    terminal_status = "passed" if not results["errors"] and all(
        value is not False for value in [results["boot_ok"], results["tests_ok"], results["smoke_ok"]]
    ) else "failed"
    completed = complete_verification_run(
        run["id"],
        status=terminal_status,
        results=results,
        artifacts=artifacts,
        fingerprints=fingerprints,
        metadata={
            "profile": project_row.get("profile"),
            "check_count": len(checks),
            "project_path": str(resolved_project_path),
        },
    )
    if terminal_status in {"failed", "error"}:
        upsert_regressions_for_run(completed["id"])
        plan_repair_tasks_for_run(completed["id"])
    elif terminal_status == "passed":
        resolve_regressions_for_passing_run(completed["id"])

    refreshed_project = db.project_get(project) or project_row
    if not checks and not completed["results_json"]["errors"]:
        closure_status = "yellow"
    else:
        closure_status = derive_closure_status(refreshed_project, spec, verification=completed["results_json"])
    db.project_update(project, {"closure_status": closure_status})
    return db.verification_run_get(run["id"])


def _llm_summarise_fix_attempt(
    error_before: str,
    error_after: str,
    worktree_path: Optional[Path],
) -> str:
    """
    Make a cheap LLM call to produce a human-readable summary of what a bug agent
    attempted and why it still failed. Returns "" on any error (caller treats as optional).
    """
    from swarm import orchestrator as _orch
    MINIMAX_API_KEY = _orch.MINIMAX_API_KEY
    MINIMAX_BASE_URL = _orch.MINIMAX_BASE_URL

    if not MINIMAX_API_KEY:
        return ""

    git_diff = ""
    if worktree_path is not None:
        try:
            result = subprocess.run(
                ["git", "diff", "main..HEAD", "--stat"],
                cwd=str(worktree_path), capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                git_diff = result.stdout.strip()
        except Exception:
            pass

    prompt = (
        "You are a concise technical summariser for a game development AI agent system.\n\n"
        "A bug-fixing agent just finished a fix attempt but validation still failed.\n"
        "Write 2-4 sentences summarising:\n"
        "1. What the agent changed (based on the git diff)\n"
        "2. What error remained after the fix\n"
        "3. What the likely root cause still is and what the next agent should try\n\n"
        "Be direct and specific. No preamble.\n\n"
        f"ERROR BEFORE FIX:\n{error_before[:600]}\n\n"
        f"GIT DIFF STAT (what changed):\n{git_diff or '(no changes committed)'}\n\n"
        f"ERROR AFTER FIX:\n{error_after[:600]}"
    )

    try:
        import requests as _requests
        resp = _requests.post(
            f"{MINIMAX_BASE_URL}/messages",
            headers={
                "Authorization": f"Bearer {MINIMAX_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "MiniMax-M2.7",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            for block in data.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    return block["text"].strip()
    except Exception as e:
        print(f"[Swarm] Fix summariser LLM call failed: {e}")

    return ""


def _spawn_validation_bug_task(
    project: str,
    original_task_id: str,
    error_output: str,
    worktree_path: Optional[Path] = None,
    worktree_branch: Optional[str] = None,
    fix_notes: Optional[list] = None,
    original_task: Optional[dict] = None,
):
    """Create (or update) a bug task for a validation failure."""
    bug_task_id = f"bug-{original_task_id}"
    existing = db.task_get(bug_task_id)
    original_task = original_task or db.task_get(original_task_id) or {}

    # --- Fix: deduplicate -- skip if a pending/in-progress bug task already covers the same error.
    # Normalise error_output to a comparable key (first non-empty line is the most stable indicator).
    error_key = (error_output or "").strip().splitlines()
    error_normalised = "".join(line.strip() for line in error_key if line.strip())[:300]
    for task in db.task_get_all():
        if task.get("id") == bug_task_id:
            continue
        if task.get("status") in ("pending", "in_progress"):
            if task.get("type") == "bug" and task.get("project") == project:
                meta = task.get("metadata") or {}
                prev_error = meta.get("error_log_excerpt", "") or ""
                prev_normalised = "".join(line.strip() for line in prev_error.splitlines() if line.strip())[:300]
                if prev_normalised and error_normalised and prev_normalised == error_normalised:
                    print(f"[Swarm] SKIP: validation bug task for {original_task_id} already exists as {task['id']} with identical error")
                    return None

    chain_depth = bug_task_id.count("bug-")
    deep_chain = chain_depth >= 4
    if deep_chain:
        print(f"[Swarm] DEEP CHAIN HARD STOP: {bug_task_id} is {chain_depth} levels deep -- routing to recovery task instead of spawning further bug tasks")
        # Don't mark the original task failed directly — that leaves attempts=0 and blocks the chain
        # forever with no recovery. Instead, exhaust its attempts so _spawn_review_task fires and
        # reparents dependents to a recovery task.
        try:
            orig = db.task_get(original_task_id) or {}
            max_att = orig.get("max_attempts", 3)
            meta = dict(orig.get("metadata") or {})
            meta["needs_human_review"] = True
            meta["deep_chain_depth"] = chain_depth
            meta["deep_chain_stopped_at"] = bug_task_id
            meta["last_failure"] = error_output[:2000]
            db.task_update(original_task_id, {
                "status": "failed",
                "attempts": max_att,  # exhaust attempts so _spawn_review_task fires
                "metadata": meta,
            })
            # Trigger recovery task spawning so dependents unblock
            from swarm.agent_recovery import _spawn_review_task
            _spawn_review_task(
                {**orig, "attempts": max_att, "metadata": meta},
                max_att,
                f"Deep bug chain stopped at depth {chain_depth}. Last error:\n{error_output[:1000]}",
            )
        except Exception as _e:
            print(f"[Swarm] Failed to trigger recovery for deep chain: {_e}")
        return None

    # Carry the research_feeder_cycles counter forward from the parent task so the
    # cycle cap in _apply_research_feeder_result accumulates across the full chain.
    parent_cycles = (original_task.get("metadata") or {}).get("research_feeder_cycles", 0)

    metadata: dict = {
        "parent_task": original_task_id,
        "last_failure": error_output[:2000],
        "error_log": error_output,
        "error_log_excerpt": error_output[:2000],
        "fix_notes": fix_notes or [],
        "chain_depth": chain_depth,
        "deep_chain": deep_chain,
        "research_feeder_cycles": parent_cycles,
        **branch_intent_metadata(original_task),
    }
    if worktree_path is not None:
        metadata["worktree_path"] = str(worktree_path)
        metadata["worktree_branch"] = worktree_branch
        metadata["worktree_inherited"] = True

    if worktree_path is not None:
        git_history_lines: list[str] = []
        try:
            result = subprocess.run(
                ["git", "log", "main..HEAD", "--oneline", "--no-decorate", "-3"],
                cwd=str(worktree_path), capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                git_history_lines = result.stdout.strip().splitlines()
        except Exception:
            pass

        concise_notes = []
        for note in (fix_notes or [])[-2:]:
            stripped = (note or "").strip()
            if stripped:
                concise_notes.append(stripped[:300])

        error_lines = []
        for line in error_output.splitlines():
            line = line.strip()
            if not line:
                continue
            error_lines.append(line)
            if len(error_lines) >= 20:
                break
        concise_error = "\n".join(error_lines)[:2400]

        attempted_section = "\n".join(f"- {note}" for note in concise_notes) if concise_notes else "- none"
        commit_section = "\n".join(f"- {line}" for line in git_history_lines) if git_history_lines else "- none"

        godot_cmd = shell_quote_command_arg(resolve_godot_binary())
        description = (
            f"[WORKTREE MODE -- read this carefully before doing anything]\n\n"
            f"You are taking over a bug-fix chain. Previous agents have already made "
            f"commits in an isolated worktree. Your job is to fix the remaining errors "
            f"listed below and nothing else. Do NOT sync with main -- the errors listed "
            f"are specific to the code already in this worktree and syncing would "
            f"introduce unrelated changes that are not your responsibility.\n\n"
            f"--- ORIGINAL GOAL ---\n"
            f"{format_branch_intent(original_task)}\n\n"
            f"--- CURRENT HANDOFF ---\n"
            f"Prior attempt summaries:\n{attempted_section}\n\n"
            f"Recent worktree commits:\n{commit_section}\n\n"
            f"--- YOUR ERRORS TO FIX ---\n"
            f"These current errors must be gone before you commit:\n{concise_error}\n\n"
            f"--- YOUR WORKING DIRECTORY ---\n"
            f"Your worktree is already checked out at: {worktree_path}\n"
            f"Branch: {worktree_branch}\n"
            f"Do NOT run git merge, git rebase, git pull, or git push. "
            f"When you are done, run the Godot validation command to confirm zero errors, "
            f"then call git_commit(). The orchestrator will handle merging to main.\n\n"
            f"--- YOUR STEPS ---\n"
            f"1. Ignore stale advice that is not directly relevant to the current errors above.\n"
            f"2. Read the files named in the current errors above.\n"
            f"3. Fix only those errors. Do not touch unrelated code.\n"
            f"4. Make sure the original goal above is still satisfied when you are done.\n"
            f"5. Run: run_command('{godot_cmd} --headless --path {worktree_path} --script res://check_scripts.gd --quit 2>&1') and confirm no errors.\n"
            f"6. Call git_commit() with a short message describing what you fixed.\n"
            f"7. Say TASK_COMPLETE.\n"
            f"\nDo NOT call git_push(). Do NOT modify refactor.md."
        )
    else:
        description = (
            "VALIDATION BUG TASK:\n\n"
            f"{format_branch_intent(original_task)}\n\n"
            f"CURRENT VALIDATION FAILURE after {original_task_id}:\n{error_output}\n\n"
            "YOUR JOB:\n"
            "1. Fix the current validation/parsing/runtime issue.\n"
            "2. Preserve and complete the original task objective above.\n"
            "3. Re-run validation and close the branch only when the original goal still holds.\n"
        )

    # Only depend on the original task if it still exists in the active table.
    # If it was already completed and pruned, inheriting it creates a ghost dep
    # that will permanently block the bug task.
    completed_ids = db.task_get_completed_ids()
    active_ids = {t["id"] for t in db.task_get_all()}
    if original_task_id in active_ids and original_task_id not in completed_ids:
        bug_task_deps = [original_task_id]
    else:
        # Original task is gone -- chain to project head so the bug task
        # is never a free-floating node in the dependency graph.
        from swarm.task_chains import chain_to_project_head
        bug_task_deps = chain_to_project_head(db, project, task_id=bug_task_id, ensure_head=True)

    # Decay priority with nested bug depth so first-level validation bugs retain
    # top priority but repeated bug-of-bug chains stop crowding out feature work.
    bug_priority = max(70, 100 - 5 * max(0, chain_depth - 1))

    bug_task = {
        "id": bug_task_id,
        "project": project,
        "type": "bug",
        "priority": bug_priority,
        "description": description,
        "status": "pending",
        "dependencies": bug_task_deps,
        "metadata": metadata,
    }
    if existing:
        db.task_update(bug_task_id, {
            "description": bug_task["description"],
            "metadata": bug_task["metadata"],
            "status": "pending",
            "priority": bug_task["priority"],
        })
    else:
        db.task_upsert(bug_task)
    action = "Updated" if existing else "Created"
    wt_info = f" (worktree={worktree_path.name})" if worktree_path else ""
    print(f"[Swarm] {action} validation bug task {bug_task_id}{wt_info}")

    # Link any open regressions for this project to the new bug task so that when
    # the bug task completes, resolve_regressions_for_linked_task() can auto-close them.
    try:
        unlinked_regressions = [
            r for r in db.regression_list_by_project(project, status="open")
            if not r.get("linked_task_id")
        ]
        for reg in unlinked_regressions:
            db.regression_update(reg["id"], {"linked_task_id": bug_task_id})
        if unlinked_regressions:
            print(f"[Swarm] Linked {len(unlinked_regressions)} open regression(s) to bug task {bug_task_id}")
    except Exception as _link_err:
        print(f"[Swarm] WARNING: failed to link regressions to bug task: {_link_err}")

    if not existing:
        for task_id in reparent_dependents(
            db,
            original_task_id,
            bug_task_id,
            project=project,
            exclude_task_ids=[bug_task_id],
        ):
            print(f"[Swarm] Reparented {task_id}: dependency {original_task_id} → {bug_task_id}")

        # Gate any lock-conflict continuations spawned by the completed task.
        # T's agent may create continuation C (reparenting downstream D from T→C)
        # before validation runs. reparent_dependents above won't find D anymore.
        # Replace T with bug_task_id inside C so C cannot run on still-broken
        # code and does not retain a stale edge after T is archived.
        for _cont_task in db.task_get_all():
            _cont_id = _cont_task.get("id")
            if not _cont_id or _cont_id == bug_task_id:
                continue
            if project and _cont_task.get("project") != project:
                continue
            if _cont_task.get("status") not in ("pending", "in_progress"):
                continue
            _cont_meta = _cont_task.get("metadata") or {}
            if _cont_meta.get("lock_conflict_followup_for") != original_task_id:
                continue
            _cont_deps = list((_cont_task.get("dependencies") or []))
            _new_deps = []
            _seen_deps = set()
            for _dep in _cont_deps:
                _next_dep = bug_task_id if _dep == original_task_id else _dep
                if not _next_dep or _next_dep == _cont_id or _next_dep in _seen_deps:
                    continue
                _seen_deps.add(_next_dep)
                _new_deps.append(_next_dep)
            if bug_task_id not in _seen_deps:
                _new_deps.append(bug_task_id)
            if _new_deps != _cont_deps:
                db.task_update(_cont_id, {"dependencies": _new_deps})
                print(
                    f"[Swarm] Gated lock-conflict continuation {_cont_id} on validation bug {bug_task_id} "
                    f"and removed stale dependency {original_task_id}"
                )
