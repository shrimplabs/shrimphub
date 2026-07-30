"""
Tools for submitting jobs to the Athena GPU Scheduler (http://athena.local:8767).

Provides two agent-callable functions:
  generate_image(prompt, width, height, steps, cfg, slug)   -> {"ok": True, "path": "/abs/path.png", ...}
  generate_3d_asset(image_path, quality, slug)              -> {"ok": True, "path": "/abs/path.glb", ...}

Both block until the job reaches succeeded/failed status, then copy the result into
the calling agent's data directory (data/generated/<job_id>/) so it's accessible
to subsequent tool calls without knowing the scheduler's results path.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import requests

_BASE_URL = os.environ.get("ATHENA_SCHEDULER_URL", "http://athena.local:8767")
_POLL_INTERVAL = 5   # seconds between status polls
_TIMEOUT = 1200      # 20 min hard cap per job


def _submit(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{_BASE_URL}/jobs"
    resp = requests.post(url, json={"type": job_type, "payload": payload}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _poll_job(job_id: str) -> dict[str, Any]:
    url = f"{_BASE_URL}/jobs/{job_id}"
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        job = resp.json()
        status = job.get("status", "")
        if status in ("succeeded", "failed", "canceled"):
            return job
        time.sleep(_POLL_INTERVAL)
    raise TimeoutError(f"GPU scheduler job {job_id} did not complete within {_TIMEOUT}s")


def _copy_result_files(job: dict[str, Any], dest_dir: Path) -> list[str]:
    """Copy files listed in job result.json (or result dict) into dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = job.get("result") or {}
    files_meta = result.get("files") or []
    copied = []
    for f in files_meta:
        src = Path(f.get("path", ""))
        if src.exists():
            dst = dest_dir / src.name
            shutil.copy2(src, dst)
            copied.append(str(dst))
    return copied


def generate_image(
    prompt: str,
    width: int = 512,
    height: int = 512,
    steps: int = 1,
    cfg: float = 0.0,
    slug: str = "",
) -> dict[str, Any]:
    """
    Generate a 2D image via the Athena GPU Scheduler (Stable Diffusion / sd-turbo).

    Returns {"ok": True, "job_id": "...", "path": "/abs/path/to/image.png", "files": [...]}
    or      {"ok": False, "error": "...", "job_id": "..."}
    """
    payload: dict[str, Any] = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg": cfg,
    }
    if slug:
        payload["slug"] = slug

    job = _submit("image.generate", payload)
    job_id = job["id"]

    try:
        completed = _poll_job(job_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "job_id": job_id}

    if completed.get("status") != "succeeded":
        return {"ok": False, "error": f"Job {job_id} ended with status {completed.get('status')}", "job_id": job_id}

    dest = Path("data/generated") / job_id
    copied = _copy_result_files(completed, dest)
    first = copied[0] if copied else ""
    return {"ok": True, "job_id": job_id, "path": first, "files": copied}


def generate_3d_asset(
    image_path: str,
    quality: str = "draft",
    slug: str = "",
) -> dict[str, Any]:
    """
    Convert an image into a 3D asset (.glb) via Trellis2 on the Athena GPU Scheduler.

    quality: "draft" (faster, 2k texture) or "high" (32 steps, 4k texture).
    image_path must be an absolute path readable by the scheduler host (athena.local).

    Returns {"ok": True, "job_id": "...", "path": "/abs/path/to/model.glb", "files": [...]}
    or      {"ok": False, "error": "...", "job_id": "..."}
    """
    src = Path(image_path)
    if not src.exists():
        return {"ok": False, "error": f"image_path does not exist: {image_path}", "job_id": ""}

    payload: dict[str, Any] = {
        "image_path": str(src.resolve()),
        "quality": quality,
    }
    if slug:
        payload["slug"] = slug
    elif src.stem:
        payload["slug"] = src.stem

    job = _submit("model.generate", payload)
    job_id = job["id"]

    try:
        completed = _poll_job(job_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "job_id": job_id}

    if completed.get("status") != "succeeded":
        return {"ok": False, "error": f"Job {job_id} ended with status {completed.get('status')}", "job_id": job_id}

    dest = Path("data/generated") / job_id
    copied = _copy_result_files(completed, dest)
    first = copied[0] if copied else ""
    return {"ok": True, "job_id": job_id, "path": first, "files": copied}
