import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

from swarm import db
from swarm.branch_intent import branch_intent_metadata, format_branch_intent

MAX_WORKTREE_AGE_SECONDS: int = 21600  # 6 hours


def _is_git_repo(project_path: Path) -> bool:
    """Return True if the directory is tracked by git."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(project_path), capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _create_worktree(project_path: Path, agent_id: str) -> Optional[Tuple[Path, str]]:
    """
    Create an isolated git worktree for an agent.
    Returns (worktree_path, branch_name) on success, None on failure.
    """
    branch = f"agent/{agent_id[:8]}"
    worktree_path = project_path.parent / f".wt-{project_path.name}-{agent_id[:8]}"
    try:
        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), "-b", branch],
            cwd=str(project_path), capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print(f"[Swarm] Created worktree {worktree_path.name} on branch {branch}")
            src_cache = project_path / ".godot"
            dst_cache = worktree_path / ".godot"
            if src_cache.exists() and not dst_cache.exists():
                try:
                    import shutil as _shutil
                    _shutil.copytree(str(src_cache), str(dst_cache))
                    print(f"[Swarm] Seeded .godot cache into {worktree_path.name}")
                except Exception as _ce:
                    print(f"[Swarm] WARNING: could not seed .godot cache: {_ce}")
            return worktree_path, branch
        print(f"[Swarm] Worktree creation failed: {result.stderr.strip()}")
    except Exception as e:
        print(f"[Swarm] Worktree creation error: {e}")
    return None


def _merge_worktree_branch(project_path: Path, branch: str, agent_id: str,
                           task_id: Optional[str],
                           worktree_path: Optional[Path] = None) -> bool:
    """
    Merge agent branch into current HEAD of the main project.
    Returns True on success, False on conflict (spawns a bug-merge-* task).
    """
    try:
        result = subprocess.run(
            ["git", "merge", "--ff-only", branch],
            cwd=str(project_path), capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print(f"[Swarm] Fast-forward merged {branch} into {project_path.name}")
            return True

        result = subprocess.run(
            ["git", "merge", "--no-edit", branch],
            cwd=str(project_path), capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print(f"[Swarm] Merged {branch} into {project_path.name}")
            return True

        subprocess.run(["git", "merge", "--abort"], cwd=str(project_path),
                       capture_output=True, timeout=10)
        conflict_info = result.stdout + result.stderr
        print(f"[Swarm] Merge conflict for agent {agent_id[:8]}: {conflict_info[:200]}")

        # Try rebase-then-merge before giving up and spawning a bug task.
        # Many "conflicts" are simply main having moved forward — rebase resolves them.
        if worktree_path and worktree_path.exists():
            rebase_result = subprocess.run(
                ["git", "rebase", "origin/main"],
                cwd=str(worktree_path), capture_output=True, text=True, timeout=60,
            )
            if rebase_result.returncode == 0:
                retry = subprocess.run(
                    ["git", "merge", "--ff-only", branch],
                    cwd=str(project_path), capture_output=True, text=True, timeout=30,
                )
                if retry.returncode == 0:
                    print(f"[Swarm] Resolved conflict via rebase for agent {agent_id[:8]}")
                    return True
            else:
                subprocess.run(["git", "rebase", "--abort"], cwd=str(worktree_path),
                               capture_output=True, timeout=10)

        if task_id and worktree_path:
            project_name = project_path.name
            branch_short = branch.replace("agent/", "")[:12]
            conflict_task_id = f"bug-merge-{branch_short}"
            original_task = db.task_get(task_id) or {}
            description = (
                f"[WORKTREE MODE — read this carefully before doing anything]\n\n"
                f"You are resolving a merge conflict. Your branch has commits that "
                f"conflict with changes that landed on main while you were working. "
                f"Your job is to rebase your branch onto the current main so the "
                f"orchestrator can merge it cleanly.\n\n"
                f"{format_branch_intent(original_task)}\n\n"
                f"--- THE CONFLICT ---\n{conflict_info[:1000]}\n\n"
                f"--- YOUR WORKING DIRECTORY ---\n"
                f"Your worktree is already checked out at: {worktree_path}\n"
                f"Branch: {branch}\nOriginal task: {task_id}\n\n"
                f"--- YOUR STEPS ---\n"
                f"1. run_command('git -C {worktree_path} rebase main')\n"
                f"2. Resolve any conflict markers, then git add + rebase --continue.\n"
                f"3. Validate: run_command('godot --headless --path {worktree_path} "
                f"--script res://check_scripts.gd --quit 2>&1')\n"
                f"4. Say TASK_COMPLETE.\n\n"
                f"Do NOT call git_push(). The orchestrator will re-attempt the merge."
            )
            existing = db.task_get(conflict_task_id)
            if existing:
                db.task_update(conflict_task_id, description=description)
                print(f"[Swarm] Updated existing merge conflict task {conflict_task_id}")
            else:
                all_merge = [
                    t for t in db.task_get_all()
                    if t.get("project") == project_name
                    and t.get("id", "").startswith("bug-merge-")
                    and t.get("status") in ("pending", "in_progress")
                ]
                merge_ids = {t["id"] for t in all_merge}
                depended_on = {d for t in all_merge for d in t.get("dependencies", []) if d in merge_ids}
                tail = [t["id"] for t in all_merge if t["id"] not in depended_on]
                merge_deps = tail or ([task_id] if task_id else [])
                conflict_task = {
                    "id": conflict_task_id,
                    "project": project_name,
                    "type": "bug",
                    "priority": 200,
                    "description": description,
                    "status": "pending",
                    "dependencies": merge_deps,
                    "metadata": {
                        "branch": branch,
                        "agent_id": agent_id,
                        "parent_task": task_id,
                        "worktree_path": str(worktree_path),
                        "worktree_branch": branch,
                        "worktree_inherited": True,
                        "conflict_resolution": True,
                        **branch_intent_metadata(original_task),
                    },
                }
                db.task_upsert(conflict_task)
                print(f"[Swarm] Created merge conflict task {conflict_task_id} (deps: {tail})")
        elif task_id:
            project_name = project_path.name
            conflict_task = {
                "id": f"bug-merge-{agent_id[:8]}",
                "project": project_name,
                "type": "bug",
                "priority": 100,
                "description": (
                    f"Merge conflict: branch {branch} could not be merged into main.\n"
                    f"Conflict details:\n{conflict_info[:1000]}\n\n"
                    f"Resolve manually in the project directory, validate, and commit."
                ),
                "status": "pending",
                "dependencies": [task_id],
                "metadata": {"branch": branch, "agent_id": agent_id, "parent_task": task_id},
            }
            db.task_upsert(conflict_task)
            print(f"[Swarm] Created merge conflict task bug-merge-{agent_id[:8]} (no worktree)")
        return False
    except Exception as e:
        print(f"[Swarm] Merge error: {e}")
        return False


def _cleanup_worktree(project_path: Path, worktree_path: Path, branch: str):
    """Remove a git worktree and its associated branch."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(project_path), capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=str(project_path), capture_output=True, timeout=10,
        )
        print(f"[Swarm] Cleaned up worktree {worktree_path.name} and branch {branch}")
    except Exception as e:
        print(f"[Swarm] Worktree cleanup error: {e}")


def cleanup_orphaned_worktrees(workspace: Optional[Path] = None):
    """Remove .wt-* worktree dirs left by agents killed mid-run.

    Also cleans worktrees that are older than MAX_WORKTREE_AGE_SECONDS (6 hours)
    even if they were intentionally preserved for chained bug agents.
    """
    from swarm import orchestrator
    ws = workspace or orchestrator.WORKSPACE
    if not ws or not ws.exists():
        return

    now = time.time()
    cleaned = 0
    for wt_dir in ws.glob(".wt-*"):
        name_no_prefix = wt_dir.name[4:]
        last_dash = name_no_prefix.rfind("-")
        if last_dash < 0:
            continue
        project_name = name_no_prefix[:last_dash]
        agent_short = name_no_prefix[last_dash + 1:]
        project_path = ws / project_name
        if not project_path.exists():
            continue

        try:
            age = now - wt_dir.stat().st_mtime
        except Exception:
            age = 0
        if age <= MAX_WORKTREE_AGE_SECONDS:
            continue

        branch = f"agent/{agent_short}"
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_dir)],
                cwd=str(project_path), capture_output=True, timeout=15,
            )
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=str(project_path), capture_output=True, timeout=10,
            )
            cleaned += 1
            print(f"[Swarm] Cleaned aged worktree ({age/3600:.1f}h old): {wt_dir.name}")

            wt_path_str = str(wt_dir)
            for task in db.task_get_by_status("pending"):
                meta = task.get("metadata") or {}
                if meta.get("worktree_path") == wt_path_str:
                    meta = dict(meta)
                    meta.pop("worktree_path", None)
                    meta.pop("worktree_branch", None)
                    meta.pop("worktree_inherited", None)
                    db.task_update(task["id"], {"metadata": meta})
                    print(f"[Swarm] Cleared stale worktree ref from task {task['id']}")
        except Exception as ce:
            print(f"[Swarm] Could not clean worktree {wt_dir.name}: {ce}")

    # Sweep all worktrees for truly orphaned ones (no active agent, no pending task)
    for wt_dir in ws.glob(".wt-*"):
        name_no_prefix = wt_dir.name[4:]
        last_dash = name_no_prefix.rfind("-")
        if last_dash < 0:
            continue
        project_name = name_no_prefix[:last_dash]
        project_path = ws / project_name
        if not project_path.exists():
            continue

        try:
            wt_path_str = str(wt_dir)
            active_tasks = (
                db.task_get_by_status("pending") + db.task_get_by_status("in_progress")
            )
            referenced = any(
                (t.get("metadata") or {}).get("worktree_path") == wt_path_str
                for t in active_tasks
            )
            if referenced:
                continue
        except Exception:
            continue

        agent_short = name_no_prefix[last_dash + 1:]
        branch = f"agent/{agent_short}"
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_dir)],
                cwd=str(project_path), capture_output=True, timeout=15,
            )
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=str(project_path), capture_output=True, timeout=10,
            )
            cleaned += 1
            print(f"[Swarm] Cleaned orphaned worktree: {wt_dir.name}")
        except Exception as ce:
            print(f"[Swarm] Could not clean worktree {wt_dir.name}: {ce}")

    if cleaned:
        print(f"[Swarm] Cleaned {cleaned} worktree(s)")


def check_ghost_merge_tasks():
    """Complete any pending bug-merge-* tasks whose worktree no longer exists."""
    all_tasks = db.task_get_all()
    for t in all_tasks:
        if (
            t.get("id", "").startswith("bug-merge-")
            and t.get("status") in ("pending", "in_progress")
        ):
            wt_path = (t.get("metadata") or {}).get("worktree_path")
            if wt_path and not Path(wt_path).exists():
                print(f"[Swarm] Ghost merge task {t['id']}: worktree {wt_path} gone — auto-completing")
                db.task_update_status(t["id"], "completed")
