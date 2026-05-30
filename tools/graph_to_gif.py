#!/usr/bin/env python3
"""
graph_to_gif.py — Render the dep graph playback as an animated GIF.

Fetches task history from the swarm API, replays completions chronologically,
renders each frame with Graphviz (fixed layout so nodes don't jump), and
assembles a looping animated GIF via ffmpeg.

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
    --steps         Max animation frames (default: all)
    --pause-start   Extra seconds to hold first frame (default: 1.5)
    --pause-end     Extra seconds to hold last frame (default: 3)
    --history       Number of completed tasks to replay (default: 40)
    --dpi           Graphviz render DPI (default: 96)
"""

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageFilter, ImageChops
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow")
    sys.exit(1)


# ── Status colours (match dashboard CSS) ────────────────────────────────────
STATUS_COLOR = {
    "completed":   ("#3fb950", "#ffffff"),   # bright green — easier to isolate for glow
    "in_progress": ("#0e7a8a", "#00ffff"),
    "failed":      ("#6e1535", "#ff5b9c"),
    "pending":     ("#1c2128", "#c8a84b"),
    "cancelled":   ("#161b22", "#666666"),
}
COMPLETED_GLOW_COLOR = (63, 185, 80)   # RGB of #3fb950
BG_COLOR = "#0d1117"
EDGE_COLOR = "#3d444d"


def _add_glow(img: Image.Image) -> Image.Image:
    """Add a green glow around completed (bright green) nodes."""
    rgb = img.convert("RGB")
    r, g, b = rgb.split()

    # Mask: pixels that are "green enough" — completed node fill colour
    # #3fb950 = (63, 185, 80); isolate by: g high, r low-mid, b low
    r_arr = r.point(lambda x: 255 if x < 120 else 0)   # r < 120
    g_arr = g.point(lambda x: 255 if x > 140 else 0)   # g > 140
    b_arr = b.point(lambda x: 255 if x < 110 else 0)   # b < 110

    # Combine: all three conditions must hold
    mask = ImageChops.multiply(ImageChops.multiply(r_arr, g_arr), b_arr)

    # Build glow layer: green tinted, blurred outward
    glow_layer = Image.new("RGB", img.size, (0, 0, 0))
    glow_layer.paste(Image.new("RGB", img.size, (40, 220, 80)), mask=mask)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=6))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=4))  # double for intensity

    # Screen blend: lighten where glow is bright
    return ImageChops.screen(rgb, glow_layer)


def _api(base: str, path: str):
    url = f"{base.rstrip('/')}{path}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def _task_dot(tasks: list[dict], positions: dict[str, tuple] | None = None) -> str:
    """Build a DOT string. If positions provided, freeze layout with pos=."""
    known_ids = {t["id"] for t in tasks}

    lines = [
        'digraph G {',
        f'  bgcolor="{BG_COLOR}"',
        '  rankdir=LR',
        '  node [fontname="Courier" fontsize=9 style="filled,rounded" shape=box penwidth=1.5 margin="0.12,0.08"]',
        f'  edge [color="#7d8590" penwidth=1.5 arrowsize=0.7]',
        '  graph [pad="0.5" nodesep="0.3" ranksep="0.6"]',
    ]

    for t in tasks:
        tid = t["id"]
        status = t.get("status", "pending")
        fill, font_color = STATUS_COLOR.get(status, STATUS_COLOR["pending"])
        label = f"{t.get('type','?')}\\n{tid[-6:]}"
        pos_attr = ""
        if positions and tid in positions:
            x, y = positions[tid]
            pos_attr = f' pos="{x:.2f},{y:.2f}!"'
        lines.append(
            f'  "{tid}" [label="{label}" fillcolor="{fill}" fontcolor="{font_color}" '
            f'color="{fill}"{pos_attr}]'
        )

    seen = set()
    for t in tasks:
        for dep in t.get("dependencies", []):
            # Skip ghost deps (tasks not in this snapshot)
            if dep not in known_ids:
                continue
            if (dep, t["id"]) not in seen:
                seen.add((dep, t["id"]))
                lines.append(f'  "{dep}" -> "{t["id"]}"')

    lines.append("}")
    return "\n".join(lines)


def _compute_positions(tasks: list[dict], dpi: int) -> dict[str, tuple]:
    """Run dot once to get node positions; returns {task_id: (x_pts, y_pts)}."""
    dot_src = _task_dot(tasks)
    result = subprocess.run(
        ["dot", "-Txdot", f"-Gdpi={dpi}"],
        input=dot_src.encode(), capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        return {}

    xdot = result.stdout.decode()
    positions = {}
    # xdot format: "node_id" [pos="x,y" ...]
    for m in re.finditer(r'"([^"]+)"\s*\[([^\]]*)\]', xdot):
        node_id, attrs = m.group(1), m.group(2)
        pm = re.search(r'pos="([\d.]+),([\d.]+)"', attrs)
        if pm:
            positions[node_id] = (float(pm.group(1)), float(pm.group(2)))
    return positions


def _dot_to_image(dot_src: str, width: int, height: int, dpi: int,
                  use_neato: bool = False) -> Image.Image:
    """Render DOT to PIL Image."""
    if use_neato:
        cmd = ["neato", "-Tpng", f"-Gdpi={dpi}", "-n1"]
    else:
        cmd = ["dot", "-Tpng", f"-Gdpi={dpi}"]
    result = subprocess.run(cmd, input=dot_src.encode(), capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode()[:300])

    img = Image.open(io.BytesIO(result.stdout)).convert("RGB")
    img = _add_glow(img)   # glow at native res before downscale (sharper mask)
    iw, ih = img.size
    scale = min(width / iw, height / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), BG_COLOR)
    canvas.paste(img, ((width - nw) // 2, (height - nh) // 2))
    return canvas


def build_snapshots(tasks: list[dict], history_limit: int) -> list[list[dict]]:
    """
    Replay completed tasks in chronological order (by completed timestamp).
    Returns a list of snapshots; each is the full task list with statuses
    as they were at that point in time.
    """
    completed = [t for t in tasks if t["status"] == "completed" and t.get("completed")]

    # Sort by actual completion timestamp
    completed_sorted = sorted(completed, key=lambda t: t["completed"])

    # Limit to most recent N
    if history_limit and len(completed_sorted) > history_limit:
        completed_sorted = completed_sorted[-history_limit:]

    # We'll show all tasks in every frame, just changing statuses
    # Initially: completed tasks shown as pending, everything else at current status
    completed_ids = {t["id"] for t in completed_sorted}

    def _snapshot(flipped: set[str]) -> list[dict]:
        result = []
        for t in tasks:
            if t["id"] in completed_ids and t["id"] not in flipped:
                # Show as pending (not yet completed in this frame)
                fake = dict(t)
                fake["status"] = "pending"
                result.append(fake)
            else:
                result.append(t)
        return result

    snapshots = [_snapshot(set())]  # start: all replayed tasks shown as pending
    flipped = set()
    for t in completed_sorted:
        flipped.add(t["id"])
        snapshots.append(_snapshot(flipped))

    return snapshots


def capture_gif(project: str, output: Path, api: str, fps: int, width: int, height: int,
                steps: int | None, pause_start: float, pause_end: float,
                history: int, dpi: int):

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

    for tool in ["dot", "neato"]:
        if not shutil.which(tool):
            print(f"ERROR: graphviz not installed (missing '{tool}'). Run: brew install graphviz")
            sys.exit(1)
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not installed. Run: brew install ffmpeg")
        sys.exit(1)

    snapshots = build_snapshots(tasks, history_limit=history)
    print(f"Built {len(snapshots)} playback snapshots")

    # Compute fixed layout from the final (fully-completed) snapshot
    print("Computing fixed layout...")
    positions = _compute_positions(snapshots[-1], dpi)
    print(f"Got positions for {len(positions)} nodes")

    # Downsample if needed
    if steps and len(snapshots) > steps:
        indices = sorted(set(
            [0] + [int(i * (len(snapshots) - 1) / (steps - 1)) for i in range(1, steps - 1)] + [len(snapshots) - 1]
        ))
        snapshots = [snapshots[i] for i in indices]
        print(f"Downsampled to {len(snapshots)} frames")

    # Render frames
    frames: list[Image.Image] = []
    for i, snapshot in enumerate(snapshots):
        if i % 5 == 0:
            print(f"  Rendering frame {i+1}/{len(snapshots)}...")
        dot = _task_dot(snapshot, positions=positions if positions else None)
        try:
            frame = _dot_to_image(dot, width, height, dpi, use_neato=bool(positions))
            frames.append(frame)
        except Exception as e:
            print(f"  Warning: frame {i} failed: {e}")
            if frames:
                frames.append(frames[-1].copy())

    if not frames:
        print("ERROR: No frames rendered")
        sys.exit(1)

    # Hold first and last frames
    hold_start = max(1, int(pause_start * fps))
    hold_end = max(1, int(pause_end * fps))
    final_frames = [frames[0].copy()] * hold_start + frames + [frames[-1].copy()] * hold_end
    print(f"Total frames: {len(final_frames)} ({hold_start} + {len(frames)} + {hold_end})")

    # Assemble GIF via ffmpeg (most reliable)
    print(f"Saving → {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp())
    try:
        for i, f in enumerate(final_frames):
            f.convert("RGB").save(tmpdir / f"frame_{i:04d}.png")

        palette = tmpdir / "palette.png"
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", str(tmpdir / "frame_%04d.png"),
            "-vf", "palettegen=max_colors=256:stats_mode=full",
            str(palette),
        ], check=True, capture_output=True)
        subprocess.run([
            "ffmpeg", "-y", "-framerate", str(fps),
            "-i", str(tmpdir / "frame_%04d.png"),
            "-i", str(palette),
            "-lavfi", "paletteuse=dither=bayer:bayer_scale=5",
            "-loop", "0",
            str(output),
        ], check=True, capture_output=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    size_kb = output.stat().st_size // 1024
    print(f"Done! {output} ({size_kb} KB, {len(final_frames)} frames @ {fps}fps)")


def main():
    parser = argparse.ArgumentParser(description="Render dep graph playback as animated GIF")
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", help="Output GIF path (default: <project>.gif)")
    parser.add_argument("--api", default="http://localhost:5001")
    parser.add_argument("--fps", type=int, default=6)
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--height", type=int, default=820)
    parser.add_argument("--steps", type=int, default=None)
    parser.add_argument("--pause-start", type=float, default=1.5)
    parser.add_argument("--pause-end", type=float, default=3.0)
    parser.add_argument("--history", type=int, default=40)
    parser.add_argument("--dpi", type=int, default=96)
    args = parser.parse_args()

    output = Path(args.output) if args.output else Path(f"{args.project}.gif")
    capture_gif(
        project=args.project, output=output, api=args.api,
        fps=args.fps, width=args.width, height=args.height,
        steps=args.steps, pause_start=args.pause_start, pause_end=args.pause_end,
        history=args.history, dpi=args.dpi,
    )


if __name__ == "__main__":
    main()
