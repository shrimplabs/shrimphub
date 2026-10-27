"""swarm.tools.observe_common -- shared observe-layer logic for QA agents,
the scenario replayer, and playthrough bots (roadmap #10: converge the
observe layer used by every consumer of StateServer's a11y tree and
screenshots into one place, instead of duplicating it).

This module holds pure functions only -- no TCP/StateServer client (that's
swarm.tools.scenario_qa.StateServerClient, already the canonical client) and
no module-level mutable state. Callers pass in whatever window geometry or
a11y tree data they already have.
"""

from __future__ import annotations

import re


def screen_to_viewport(x: float, y: float, window: dict) -> tuple[float, float]:
    """Convert OS screen coordinates to game viewport coordinates.

    `window` is a dict with keys x, y (screen offset) and pixel_ratio (or
    viewport_w/w to derive it) -- the same shape as qa_tools.py's _qa_window.
    """
    wx, wy = float(window.get("x", 0)), float(window.get("y", 0))
    ratio = _window_pixel_ratio(window)
    return (float(x) - wx) * ratio, (float(y) - wy) * ratio


def viewport_to_screen(vx: float, vy: float, window: dict) -> tuple[int, int]:
    """Convert game viewport coordinates to OS screen coordinates."""
    wx, wy = float(window.get("x", 0)), float(window.get("y", 0))
    ratio = _window_pixel_ratio(window)
    if ratio <= 0:
        ratio = 1.0
    return int(round(wx + (float(vx) / ratio))), int(round(wy + (float(vy) / ratio)))


def _window_pixel_ratio(window: dict) -> float:
    ratio = window.get("pixel_ratio")
    if isinstance(ratio, (int, float)) and ratio > 0:
        return float(ratio)
    vw = float(window.get("viewport_w", window.get("w", 0)) or 0)
    ww = float(window.get("w", 0) or 0)
    if vw > 0 and ww > 0:
        return vw / ww
    return 1.0


def resolve_image_coords_from_model(
    raw_x: int, raw_y: int, img_w: int, img_h: int, provider_name: str, raw_text: str = ""
) -> tuple[int, int, str]:
    """Map model-reported coords to image pixels.

    Returns: (img_x, img_y, mode) where mode is one of:
      - "normalized-1000"
      - "raw-pixels"
      - "fallback-clamped"
    """
    if img_w <= 0 or img_h <= 0:
        return max(0, raw_x), max(0, raw_y), "fallback-clamped"

    norm_candidate = None
    if 0 <= raw_x <= 1000 and 0 <= raw_y <= 1000:
        norm_candidate = (
            int(raw_x / 1000 * img_w),
            int(raw_y / 1000 * img_h),
        )

    raw_candidate = None
    if 0 <= raw_x < img_w and 0 <= raw_y < img_h:
        raw_candidate = (raw_x, raw_y)

    pname = (provider_name or "").lower()
    is_minimax = pname.startswith("minimax")

    # Minimax vision tools return 0..1000 normalized coords in practice.
    if is_minimax:
        if norm_candidate is not None:
            return norm_candidate[0], norm_candidate[1], "normalized-1000"
        if raw_candidate is not None:
            return raw_candidate[0], raw_candidate[1], "raw-pixels"
    else:
        # Local/other providers can vary. Prefer raw pixel coords when valid,
        # but fall back to normalized interpretation when raw is out of bounds.
        if raw_candidate is not None and norm_candidate is not None:
            text = (raw_text or "").lower()
            if ("normalized" in text) or ("0-1000" in text):
                return norm_candidate[0], norm_candidate[1], "normalized-1000"
            return raw_candidate[0], raw_candidate[1], "raw-pixels"
        if raw_candidate is not None:
            return raw_candidate[0], raw_candidate[1], "raw-pixels"
        if norm_candidate is not None:
            return norm_candidate[0], norm_candidate[1], "normalized-1000"

    # Last resort: clamp raw numbers into image bounds.
    return max(0, min(raw_x, img_w - 1)), max(0, min(raw_y, img_h - 1)), "fallback-clamped"


def match_a11y_element(
    a11y_tree: list,
    description: str,
    mode: str = "scored",
    require_visible: bool = False,
) -> dict | None:
    """Find the best-matching element in a flat a11y_tree list.

    mode="scored": weighted matching (exact match, substring, role bonus,
      token overlap, button-role penalty) -- ported from qa_tools.py's
      click_element grounding tier. Used when an LLM agent describes an
      element in free text and there's a vision/color-detection fallback if
      this misses.
    mode="substring": first-match-wins case-insensitive substring search --
      ported from playthrough_kit.py's click_label. Deliberately simpler and
      more permissive than "scored": deterministic playthrough bots have no
      LLM fallback, so this mode must not be silently tightened.

    Returns the matched element dict, or None if nothing matched.
    """
    if not isinstance(a11y_tree, list):
        return None

    if mode == "substring":
        needle = description.lower()
        for entry in a11y_tree:
            if require_visible and not entry.get("visible", True):
                continue
            label = str(entry.get("label", ""))
            if needle in label.lower():
                return entry
        return None

    if mode != "scored":
        raise ValueError(f"match_a11y_element: unknown mode {mode!r}")

    desc_lower = description.lower()
    desc_tokens = {tok for tok in re.findall(r"[a-z0-9]+", desc_lower) if len(tok) >= 3}
    best_elem = None
    best_score = 0
    for elem in a11y_tree:
        if require_visible and not elem.get("visible", True):
            continue
        label = elem.get("label", "") or elem.get("name", "") or ""
        qa_label = elem.get("qa_label", "") or ""
        role = (elem.get("role", "") or "").lower()
        if not label and not qa_label:
            continue
        label_lower = label.lower()
        qa_lower = qa_label.lower()
        score = 0
        if role == "button":
            score += 2
        if desc_lower == label_lower or desc_lower == qa_lower:
            score += 10
        if label_lower and (desc_lower in label_lower or label_lower in desc_lower):
            score += 6
        if qa_lower and (desc_lower in qa_lower or qa_lower in desc_lower):
            score += 6
        elem_tokens = {tok for tok in re.findall(r"[a-z0-9]+", f"{label_lower} {qa_lower}") if len(tok) >= 3}
        shared = len(desc_tokens & elem_tokens)
        score += shared * 2
        # Avoid matching the entire menu/container when the request is for a button.
        if "button" in desc_lower and role != "button":
            score -= 4
        if score > best_score:
            best_score = score
            best_elem = elem

    return best_elem if best_score > 0 else None
