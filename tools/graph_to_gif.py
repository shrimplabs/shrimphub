#!/usr/bin/env python3
"""
graph_to_gif.py — Capture the dep graph playback as an animated GIF.

Usage:
    python3 tools/graph_to_gif.py --project pebble-pop --output pebble-pop.gif
    python3 tools/graph_to_gif.py --project pebble-pop --output out.gif --fps 4 --width 1400 --height 800
    python3 tools/graph_to_gif.py --project pebble-pop --output out.gif --steps 60 --pause-start 2 --pause-end 3

Options:
    --project       Project name (required)
    --output        Output GIF path (default: <project>.gif)
    --api           Dashboard URL (default: http://localhost:5001)
    --fps           Playback speed in frames per second (default: 6)
    --width         Browser viewport width (default: 1400)
    --height        Browser viewport height (default: 820)
    --steps         Max playback steps to capture (default: all)
    --pause-start   Extra seconds to hold first frame (default: 1.5)
    --pause-end     Extra seconds to hold last frame (default: 3)
    --history       History limit for playback (default: 10)
    --no-headless   Show browser window while capturing
"""

import argparse
import io
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow")
    sys.exit(1)


def capture_gif(project: str, output: Path, api: str, fps: int, width: int, height: int,
                steps: int | None, pause_start: float, pause_end: float,
                history: int, headless: bool):

    frame_delay_ms = int(1000 / fps)
    frames: list[Image.Image] = []

    print(f"Opening dashboard for project: {project}")
    print(f"Output: {output}  |  FPS: {fps}  |  Size: {width}x{height}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": width, "height": height})

        # Load dashboard and select project
        page.goto(f"{api}/")
        page.wait_for_load_state("networkidle")

        # Select the project in the sidebar
        page.evaluate(f"""
            () => {{
                // Find and click the project in the sidebar
                const items = document.querySelectorAll('.sidebar-project-item, .project-item, [data-project]');
                for (const el of items) {{
                    if (el.textContent.includes('{project}') || el.dataset.project === '{project}') {{
                        el.click();
                        return true;
                    }}
                }}
                // Fallback: trigger via global if available
                if (window.selectProject) window.selectProject('{project}');
                return false;
            }}
        """)
        time.sleep(1)

        # Switch to deps tab if needed, set history limit
        page.evaluate(f"""
            () => {{
                // Set history limit
                const sel = document.getElementById('depsHistoryLimit');
                if (sel) {{ sel.value = '{history}'; sel.dispatchEvent(new Event('change')); }}
                // Click deps section if collapsed
                const h2 = document.querySelector('#depsSection h2, h2[onclick*="toggleDeps"]');
                if (h2) h2.click();
            }}
        """)
        time.sleep(0.5)

        # Wait for the dep graph to render
        page.wait_for_selector("#depsSvg svg", timeout=15000)
        time.sleep(1)

        # Get the dep graph container bounds for cropping
        graph_box = page.eval_on_selector("#depsSvg", "el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; }")
        print(f"Graph area: {graph_box}")

        # Check if playback controls exist
        has_playback = page.evaluate("() => !!document.getElementById('playbackBar') || !!document.getElementById('playbackPlayBtn')")

        if not has_playback:
            print("No playback controls found — capturing static graph only")
            img_data = page.screenshot(clip={"x": graph_box["x"], "y": graph_box["y"],
                                              "width": graph_box["w"], "height": graph_box["h"]})
            frame = Image.open(io.BytesIO(img_data))
            # Static: repeat frame to make a short GIF
            for _ in range(fps * 3):
                frames.append(frame.copy())
        else:
            # Get total event count
            total_steps = page.evaluate("""
                () => {
                    const s = document.getElementById('playbackScrubber');
                    return s ? parseInt(s.max || 0) : 0;
                }
            """)
            print(f"Playback events: {total_steps}")
            max_steps = steps if steps else total_steps

            def capture_frame():
                img_data = page.screenshot(clip={"x": graph_box["x"], "y": graph_box["y"],
                                                  "width": graph_box["w"], "height": graph_box["h"]})
                return Image.open(io.BytesIO(img_data))

            # Go to start
            page.evaluate("() => { const s = document.getElementById('playbackScrubber'); if(s){ s.value=0; s.dispatchEvent(new Event('input')); } }")
            time.sleep(0.3)

            # Hold first frame
            first_frame = capture_frame()
            hold_count = max(1, int(pause_start * fps))
            print(f"Holding first frame for {pause_start}s ({hold_count} frames)")
            for _ in range(hold_count):
                frames.append(first_frame.copy())

            # Step through playback
            step_size = max(1, total_steps // max_steps) if max_steps < total_steps else 1
            captured = 0
            for i in range(0, total_steps + 1, step_size):
                page.evaluate(f"""
                    () => {{
                        const s = document.getElementById('playbackScrubber');
                        if (s) {{ s.value = {i}; s.dispatchEvent(new Event('input')); }}
                        // Also try direct render function
                        if (window.SwarmDepsIntegrityUI && window.SwarmDepsIntegrityUI._pbRenderAt) {{
                            window.SwarmDepsIntegrityUI._pbRenderAt({i});
                        }}
                    }}
                """)
                time.sleep(0.12)  # let graph re-render
                frames.append(capture_frame())
                captured += 1
                if captured % 10 == 0:
                    print(f"  Captured {captured} frames...")
                if steps and captured >= steps:
                    break

            # Hold last frame
            last_frame = frames[-1]
            hold_count = max(1, int(pause_end * fps))
            print(f"Holding last frame for {pause_end}s ({hold_count} frames)")
            for _ in range(hold_count):
                frames.append(last_frame.copy())

        browser.close()

    if not frames:
        print("ERROR: No frames captured")
        sys.exit(1)

    print(f"Saving {len(frames)} frames → {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    # Convert to palette mode for smaller GIF
    palette_frames = []
    for f in frames:
        palette_frames.append(f.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT))

    palette_frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=palette_frames[1:],
        duration=frame_delay_ms,
        loop=0,  # loop forever
        optimize=True,
    )

    size_kb = output.stat().st_size // 1024
    print(f"Done! {output} ({size_kb} KB, {len(frames)} frames @ {fps}fps)")


def main():
    parser = argparse.ArgumentParser(description="Capture dep graph playback as animated GIF")
    parser.add_argument("--project", required=True, help="Project name")
    parser.add_argument("--output", help="Output GIF path (default: <project>.gif)")
    parser.add_argument("--api", default="http://localhost:5001", help="Dashboard URL")
    parser.add_argument("--fps", type=int, default=6, help="Frames per second")
    parser.add_argument("--width", type=int, default=1400, help="Viewport width")
    parser.add_argument("--height", type=int, default=820, help="Viewport height")
    parser.add_argument("--steps", type=int, default=None, help="Max steps to capture")
    parser.add_argument("--pause-start", type=float, default=1.5, help="Seconds to hold first frame")
    parser.add_argument("--pause-end", type=float, default=3.0, help="Seconds to hold last frame")
    parser.add_argument("--history", type=int, default=10, help="Playback history limit")
    parser.add_argument("--no-headless", action="store_true", help="Show browser window")
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
        headless=not args.no_headless,
    )


if __name__ == "__main__":
    main()
