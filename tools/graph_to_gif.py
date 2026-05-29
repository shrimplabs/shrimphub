#!/usr/bin/env python3
"""
graph_to_gif.py — Render the dep graph playback as an animated GIF.

Fetches task history from the swarm API, replays it step-by-step using
Graphviz, and assembles frames into a looping animated GIF.
No browser required — works even when the server is busy.

Usage:
    python3 tools/graph_to_gif.py --project pebble-pop --output pebble-pop.gif
    python3 tools/graph_to_gif.py --project pebble-pop --output out.gif --fps 4 --width 1400 --height 800
    python3 tools/graph_to_gif.py --project pebble-pop --output out.gif --steps 60 --pause-start 2 --pause-end 3

Options:
    --project       Project name (required)
    --output        Output GIF path (default: <project>.gif)
    --api           Swarm API base URL (default: http://localhost:5001)
    --fps           Playback speed in frames per second (default: 6)
    --width         Output image width in px (default: 1400)
    --height        Output image height in px (default: 820)
    --steps         Max frames to render (default: all)
    --pause-start   Extra seconds to hold first frame (default: 1.5)
    --pause-end     Extra seconds to hold last frame (default: 3)
    --history       Number of historical snapshots to replay (default: 20)
    --dpi           Graphviz render DPI (default: 96)
"""

import argparse
import io
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow")
    sys.exit(1)


# ── Status colours (match dashboard CSS) ────────────────────────────────────
STATUS_COLOR = {
    "completed":   ("#2ea043", "#ffffff"),   # green fill, white text
    "in_progress": ("#0e7a8a", "#00ffff"),   # teal fill, cyan text
    "failed":      ("#6e1535", "#ff5b9c"),   # dark red fill, pink text
    "pending":     ("#1c2128", "#c8a84b"),   # dark fill, amber text
    "cancelled":   ("#161b22", "#666666"),
}
BG_COLOR = "#0d1117"
EDGE_COLOR = "#3d444d"
FONT = "IBM Plex Mono"


def _api(base: str, path: str):
    url = f"{base.rstrip('/')}{path}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def _task_dot(tasks: list[dict], highlight_ids: set[str] | None = None) -> str:
    """Build a Graphviz DOT string for a snapshot of tasks."""
    lines = [
        'digraph G {',
        f'  bgcolor="{BG_COLOR}"',
        '  rankdir=LR',
        '  node [fontname="IBM Plex Mono" fontsize=10 style=filled penwidth=1.5 margin="0.15,0.08"]',
        f'  edge [color="{EDGE_COLOR}" penwidth=1.2 arrowsize=0.7]',
        '  graph [pad="0.4" nodesep="0.35" ranksep="0.7"]',
    ]

    for t in tasks:
        tid = t["id"]
        status = t.get("status", "pending")
        fill, font_color = STATUS_COLOR.get(status, STATUS_COLOR["pending"])
        label_parts = [t.get("type", "?"), tid[-8:]]
        label = "\\n".join(label_parts)
        border = "#58a6ff" if highlight_ids and tid in highlight_ids else fill
        lw = "3" if highlight_ids and tid in highlight_ids else "1.5"
        lines.append(
            f'  "{tid}" [label="{label}" fillcolor="{fill}" fontcolor="{font_color}" '
            f'color="{border}" penwidth={lw}]'
        )

    # Draw edges
    seen_edges = set()
    for t in tasks:
        for dep in t.get("dependencies", []):
            edge = (dep, t["id"])
            if edge not in seen_edges:
                seen_edges.add(edge)
                lines.append(f'  "{dep}" -> "{t["id"]}"')

    lines.append("}")
    return "\n".join(lines)


def _dot_to_image(dot_src: str, width: int, height: int, dpi: int) -> Image.Image:
    """Render a DOT string to a PIL Image using the graphviz `dot` command."""
    result = subprocess.run(
        ["dot", "-Tpng", f"-Gdpi={dpi}"],
        input=dot_src.encode(),
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"dot failed: {result.stderr.decode()[:200]}")
    img = Image.open(io.BytesIO(result.stdout)).convert("RGB")

    # Fit into (width, height) preserving aspect ratio, pad with bg
    iw, ih = img.size
    scale = min(width / iw, height / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    img = img.resize((nw, nh), Image.LANCZOS)

    canvas = Image.new("RGB", (width, height), BG_COLOR)
    ox, oy = (width - nw) // 2, (height - nh) // 2
    canvas.paste(img, (ox, oy))
    return canvas


def build_snapshots(tasks: list[dict], history_limit: int) -> list[list[dict]]:
    """
    Replay tasks in chronological order to produce a list of graph snapshots.
    Each snapshot is the full task list with statuses as they were at that point.

    Strategy:
      - Sort completed tasks by their approximate completion order.
      - Walk through them, building up the visible set frame by frame.
      - Pending tasks appear from the start; status changes are the "events".
    """
    # Separate by status
    completed = [t for t in tasks if t["status"] == "completed"]
    in_progress = [t for t in tasks if t["status"] == "in_progress"]
    failed = [t for t in tasks if t["status"] == "failed"]
    pending = [t for t in tasks if t["status"] == "pending"]
    other = [t for t in tasks if t["status"] not in ("completed", "in_progress", "failed", "pending")]

    # Sort completed tasks — use task ID suffix as a rough proxy for creation order
    def _sort_key(t):
        # IDs often end in a numeric timestamp suffix
        parts = t["id"].rsplit("-", 1)
        try:
            return (0, int(parts[-1]))
        except ValueError:
            return (1, t["id"])

    completed_sorted = sorted(completed, key=_sort_key)

    # Limit history
    if history_limit and len(completed_sorted) > history_limit:
        completed_sorted = completed_sorted[-history_limit:]

    # Build snapshots: start with everything pending, then flip completed one by one
    all_ids = {t["id"] for t in tasks}
    status_map = {t["id"]: t["status"] for t in tasks}

    # Initial state: all non-completed tasks at current status, completed = pending
    def _snapshot(flip_completed: set[str]) -> list[dict]:
        result = []
        for t in tasks:
            if t["id"] in flip_completed:
                fake = dict(t)
                fake["status"] = "completed"
                result.append(fake)
            else:
                result.append(t)
        return result

    snapshots = []
    flipped = set()

    # Frame 0: nothing completed yet (all completed shown as pending)
    snapshots.append(_snapshot(flipped))

    for t in completed_sorted:
        flipped.add(t["id"])
        snapshots.append(_snapshot(flipped))

    # Final frame: actual current state
    snapshots.append(list(tasks))

    return snapshots


def capture_gif(project: str, output: Path, api: str, fps: int, width: int, height: int,
                steps: int | None, pause_start: float, pause_end: float,
                history: int, dpi: int):

    frame_delay_ms = int(1000 / fps)

    print(f"Fetching tasks for project: {project}")
    try:
        data = _api(api, f"/api/tasks?project={project}&include_completed=true")
    except Exception as e:
        print(f"ERROR fetching tasks: {e}")
        sys.exit(1)

    tasks = data if isinstance(data, list) else data.get("tasks", [])
    if not tasks:
        print(f"No tasks found for project '{project}'")
        sys.exit(1)

    print(f"Found {len(tasks)} tasks")

    # Verify graphviz is available
    try:
        subprocess.run(["dot", "-V"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("ERROR: graphviz not installed. Run: brew install graphviz")
        sys.exit(1)

    snapshots = build_snapshots(tasks, history_limit=history)
    print(f"Built {len(snapshots)} playback snapshots")

    # Limit steps
    if steps and len(snapshots) > steps:
        # Keep first, last, and evenly-spaced middle frames
        indices = [0] + [int(i * (len(snapshots) - 1) / (steps - 1)) for i in range(1, steps - 1)] + [len(snapshots) - 1]
        snapshots = [snapshots[i] for i in sorted(set(indices))]
        print(f"Downsampled to {len(snapshots)} frames")

    frames: list[Image.Image] = []

    for i, snapshot in enumerate(snapshots):
        if i % 5 == 0:
            print(f"  Rendering frame {i+1}/{len(snapshots)}...")
        dot = _task_dot(snapshot)
        try:
            frame = _dot_to_image(dot, width, height, dpi)
            frames.append(frame)
        except Exception as e:
            print(f"  Warning: frame {i} render failed: {e}")
            if frames:
                frames.append(frames[-1].copy())  # duplicate last good frame

    if not frames:
        print("ERROR: No frames rendered")
        sys.exit(1)

    # Hold first and last frames
    hold_start = max(1, int(pause_start * fps))
    hold_end = max(1, int(pause_end * fps))
    final_frames = [frames[0].copy()] * hold_start + frames + [frames[-1].copy()] * hold_end
    print(f"Total frames: {len(final_frames)} ({hold_start} start hold + {len(frames)} playback + {hold_end} end hold)")

    print(f"Saving → {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    palette_frames = [f.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT) for f in final_frames]
    palette_frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=palette_frames[1:],
        duration=frame_delay_ms,
        loop=0,
        optimize=True,
    )

    size_kb = output.stat().st_size // 1024
    print(f"Done! {output} ({size_kb} KB, {len(final_frames)} frames @ {fps}fps)")


def main():
    parser = argparse.ArgumentParser(description="Render dep graph playback as animated GIF")
    parser.add_argument("--project", required=True, help="Project name")
    parser.add_argument("--output", help="Output GIF path (default: <project>.gif)")
    parser.add_argument("--api", default="http://localhost:5001", help="Swarm API URL")
    parser.add_argument("--fps", type=int, default=6, help="Frames per second")
    parser.add_argument("--width", type=int, default=1400, help="Output width in px")
    parser.add_argument("--height", type=int, default=820, help="Output height in px")
    parser.add_argument("--steps", type=int, default=None, help="Max frames to render")
    parser.add_argument("--pause-start", type=float, default=1.5, help="Seconds to hold first frame")
    parser.add_argument("--pause-end", type=float, default=3.0, help="Seconds to hold last frame")
    parser.add_argument("--history", type=int, default=20, help="Completed tasks to replay (most recent N)")
    parser.add_argument("--dpi", type=int, default=96, help="Graphviz render DPI")
    args = parser.parse_args()

    output = Path(args.output) if args.output else Path(f"{args.project}.gif")

    capture_gif(
        project=args.project,
        output=output,
        api=args.api,
        fps=args.fps,
        width=args.width,
        height=args.height,
        steps=args.steps,
        pause_start=args.pause_start,
        pause_end=args.pause_end,
        history=args.history,
        dpi=args.dpi,
    )


if __name__ == "__main__":
    main()
