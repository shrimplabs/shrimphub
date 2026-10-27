#!/usr/bin/env python3
"""Analyze run 7 pipeline experiment data and render lightweight SVG charts."""

from __future__ import annotations

import csv
import html
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_ID = "void-patrol-pipeline-ab-run7-20260613"
EVENTS_PATH = ROOT / "data" / "experiments" / EXPERIMENT_ID / "events.jsonl"
DB_PATH = ROOT / "data" / "swarm.db"
OUT_DIR = ROOT / "data" / "experiment_exports" / "run7-analysis-20260614"

VARIANT_ORDER = ["variant-f", "variant-c", "variant-e", "variant-g", "variant-adaptive"]
VARIANT_LABELS = {
    "variant-f": "F flat",
    "variant-c": "C scout-work",
    "variant-e": "E scout-plan",
    "variant-g": "G work-scout-plan",
    "variant-adaptive": "Adaptive",
}
VARIANT_COLORS = {
    "variant-f": "#3366cc",
    "variant-c": "#109618",
    "variant-e": "#dc3912",
    "variant-g": "#ff9900",
    "variant-adaptive": "#990099",
}
STATUS_COLORS = {
    "completed": "#2e7d32",
    "failed": "#c62828",
    "cancelled": "#ef6c00",
    "pending": "#9e9e9e",
    "in_progress": "#1565c0",
}
PHASE_COLORS = {
    "plan": "#6a1b9a",
    "scout": "#00897b",
    "work": "#1565c0",
    "repair_work": "#64b5f6",
    "validate": "#455a64",
    "repair_validate": "#90a4ae",
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _load_tasks() -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              t.id, t.project, t.type, t.status, t.attempts, t.max_attempts,
              t.created, t.started, t.completed, t.dependencies, t.metadata,
              p.closure_status, p.last_verification_status, p.open_regression_count
            FROM tasks t
            LEFT JOIN projects p ON p.name = t.project
            WHERE t.project LIKE 'void-patrol-%run7'
            ORDER BY t.project, t.created, t.id
            """
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            meta = json.loads(item.get("metadata") or "{}")
        except Exception:
            meta = {}
        item["metadata"] = meta
        item["variant"] = meta.get("experiment_variant") or _variant_from_project(item["project"])
        item["source_task_id"] = meta.get("source_task_id") or ""
        item["seeded_tail"] = bool(meta.get("seeded_experiment_tail"))
        item["infrastructure_failure"] = bool(meta.get("infrastructure_failure"))
        out.append(item)
    return out


def _variant_from_project(project: str) -> str:
    if "control-run7" in project:
        return "variant-f"
    if "variant-c-run7" in project:
        return "variant-c"
    if "variant-e-run7" in project:
        return "variant-e"
    if "variant-g-run7" in project:
        return "variant-g"
    if "adaptive-run7" in project:
        return "variant-adaptive"
    return project


def _variant_sort_key(variant: str) -> tuple[int, str]:
    try:
        return (VARIANT_ORDER.index(variant), variant)
    except ValueError:
        return (999, variant)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _mean(values: list[float]) -> float:
    values = [v for v in values if v is not None and not math.isnan(v)]
    return mean(values) if values else 0.0


def _median(values: list[float]) -> float:
    values = [v for v in values if v is not None and not math.isnan(v)]
    return median(values) if values else 0.0


def _effective_loops(row: dict[str, Any]) -> float:
    """Use structured total loops when present, otherwise legacy work loops."""
    total = _num(row.get("total_phase_loops"))
    return total if total > 0 else _num(row.get("work_loops"))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                keys.append(key)
                seen.add(key)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _svg_header(width: int, height: int, title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:Arial,Helvetica,sans-serif;fill:#212121}",
        ".title{font-size:20px;font-weight:700}",
        ".axis{stroke:#424242;stroke-width:1}",
        ".grid{stroke:#e0e0e0;stroke-width:1}",
        ".label{font-size:12px}",
        ".small{font-size:11px;fill:#555}",
        "</style>",
        f'<rect width="100%" height="100%" fill="#fff"/>',
        f'<text class="title" x="24" y="32">{html.escape(title)}</text>',
    ]


def _svg_footer() -> list[str]:
    return ["</svg>"]


def _save_svg(path: Path, parts: list[str]) -> None:
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def _bar_chart(path: Path, title: str, data: dict[str, float], *, ylabel: str = "", color_by_variant: bool = True) -> None:
    width, height = 920, 520
    margin = {"left": 86, "right": 36, "top": 68, "bottom": 110}
    plot_w = width - margin["left"] - margin["right"]
    plot_h = height - margin["top"] - margin["bottom"]
    max_v = max(data.values(), default=1)
    max_v = max(max_v, 1)
    parts = _svg_header(width, height, title)
    # grid
    for i in range(6):
        y = margin["top"] + plot_h - (plot_h * i / 5)
        val = max_v * i / 5
        parts.append(f'<line class="grid" x1="{margin["left"]}" y1="{y:.1f}" x2="{width-margin["right"]}" y2="{y:.1f}"/>')
        parts.append(f'<text class="small" x="{margin["left"]-8}" y="{y+4:.1f}" text-anchor="end">{val:.1f}</text>')
    parts.append(f'<line class="axis" x1="{margin["left"]}" y1="{margin["top"]}" x2="{margin["left"]}" y2="{margin["top"]+plot_h}"/>')
    parts.append(f'<line class="axis" x1="{margin["left"]}" y1="{margin["top"]+plot_h}" x2="{width-margin["right"]}" y2="{margin["top"]+plot_h}"/>')
    if ylabel:
        parts.append(f'<text class="small" x="24" y="{margin["top"]+plot_h/2:.1f}" transform="rotate(-90 24 {margin["top"]+plot_h/2:.1f})">{html.escape(ylabel)}</text>')
    labels = list(data)
    bar_gap = 18
    bar_w = (plot_w - bar_gap * (len(labels) + 1)) / max(len(labels), 1)
    for i, label in enumerate(labels):
        val = data[label]
        x = margin["left"] + bar_gap + i * (bar_w + bar_gap)
        h = plot_h * val / max_v
        y = margin["top"] + plot_h - h
        color = VARIANT_COLORS.get(label, "#607d8b") if color_by_variant else "#607d8b"
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="2" fill="{color}"/>')
        parts.append(f'<text class="small" x="{x+bar_w/2:.1f}" y="{y-6:.1f}" text-anchor="middle">{val:.2f}</text>')
        parts.append(
            f'<text class="label" x="{x+bar_w/2:.1f}" y="{margin["top"]+plot_h+20}" text-anchor="end" transform="rotate(-35 {x+bar_w/2:.1f} {margin["top"]+plot_h+20})">'
            f'{html.escape(VARIANT_LABELS.get(label, label))}</text>'
        )
    _save_svg(path, parts + _svg_footer())


def _stacked_bar_chart(path: Path, title: str, data: dict[str, dict[str, float]], stacks: list[str], colors: dict[str, str]) -> None:
    width, height = 980, 560
    margin = {"left": 86, "right": 170, "top": 68, "bottom": 110}
    plot_w = width - margin["left"] - margin["right"]
    plot_h = height - margin["top"] - margin["bottom"]
    totals = {k: sum(v.get(s, 0) for s in stacks) for k, v in data.items()}
    max_v = max(totals.values(), default=1)
    max_v = max(max_v, 1)
    parts = _svg_header(width, height, title)
    for i in range(6):
        y = margin["top"] + plot_h - (plot_h * i / 5)
        val = max_v * i / 5
        parts.append(f'<line class="grid" x1="{margin["left"]}" y1="{y:.1f}" x2="{width-margin["right"]}" y2="{y:.1f}"/>')
        parts.append(f'<text class="small" x="{margin["left"]-8}" y="{y+4:.1f}" text-anchor="end">{val:.0f}</text>')
    labels = list(data)
    bar_gap = 20
    bar_w = (plot_w - bar_gap * (len(labels) + 1)) / max(len(labels), 1)
    for i, label in enumerate(labels):
        x = margin["left"] + bar_gap + i * (bar_w + bar_gap)
        y_cursor = margin["top"] + plot_h
        for stack in stacks:
            val = data[label].get(stack, 0)
            h = plot_h * val / max_v
            y_cursor -= h
            parts.append(f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="{colors.get(stack, "#999")}"/>')
            if h > 16:
                parts.append(f'<text class="small" x="{x+bar_w/2:.1f}" y="{y_cursor+h/2+4:.1f}" text-anchor="middle" fill="#fff">{val:.0f}</text>')
        parts.append(
            f'<text class="label" x="{x+bar_w/2:.1f}" y="{margin["top"]+plot_h+20}" text-anchor="end" transform="rotate(-35 {x+bar_w/2:.1f} {margin["top"]+plot_h+20})">'
            f'{html.escape(VARIANT_LABELS.get(label, label))}</text>'
        )
    lx, ly = width - margin["right"] + 16, margin["top"] + 8
    for j, stack in enumerate(stacks):
        parts.append(f'<rect x="{lx}" y="{ly+j*24}" width="14" height="14" fill="{colors.get(stack, "#999")}"/>')
        parts.append(f'<text class="small" x="{lx+22}" y="{ly+j*24+12}">{html.escape(stack)}</text>')
    _save_svg(path, parts + _svg_footer())


def _grouped_bar_chart(path: Path, title: str, data: dict[str, dict[str, float]], series: list[str], colors: dict[str, str]) -> None:
    width, height = 1040, 580
    margin = {"left": 86, "right": 180, "top": 68, "bottom": 120}
    plot_w = width - margin["left"] - margin["right"]
    plot_h = height - margin["top"] - margin["bottom"]
    max_v = max((v.get(s, 0) for v in data.values() for s in series), default=1)
    max_v = max(max_v, 1)
    parts = _svg_header(width, height, title)
    for i in range(6):
        y = margin["top"] + plot_h - (plot_h * i / 5)
        val = max_v * i / 5
        parts.append(f'<line class="grid" x1="{margin["left"]}" y1="{y:.1f}" x2="{width-margin["right"]}" y2="{y:.1f}"/>')
        parts.append(f'<text class="small" x="{margin["left"]-8}" y="{y+4:.1f}" text-anchor="end">{val:.1f}</text>')
    labels = list(data)
    group_gap = 22
    group_w = (plot_w - group_gap * (len(labels) + 1)) / max(len(labels), 1)
    bar_w = group_w / max(len(series), 1)
    for i, label in enumerate(labels):
        gx = margin["left"] + group_gap + i * (group_w + group_gap)
        for j, s in enumerate(series):
            val = data[label].get(s, 0)
            h = plot_h * val / max_v
            x = gx + j * bar_w
            y = margin["top"] + plot_h - h
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(bar_w-2, 1):.1f}" height="{h:.1f}" fill="{colors.get(s, "#777")}"/>')
        parts.append(
            f'<text class="label" x="{gx+group_w/2:.1f}" y="{margin["top"]+plot_h+22}" text-anchor="end" transform="rotate(-35 {gx+group_w/2:.1f} {margin["top"]+plot_h+22})">'
            f'{html.escape(VARIANT_LABELS.get(label, label))}</text>'
        )
    lx, ly = width - margin["right"] + 16, margin["top"] + 8
    for j, s in enumerate(series):
        parts.append(f'<rect x="{lx}" y="{ly+j*24}" width="14" height="14" fill="{colors.get(s, "#777")}"/>')
        parts.append(f'<text class="small" x="{lx+22}" y="{ly+j*24+12}">{html.escape(s)}</text>')
    _save_svg(path, parts + _svg_footer())


def _scatter_chart(path: Path, title: str, rows: list[dict[str, Any]]) -> None:
    width, height = 980, 620
    margin = {"left": 86, "right": 170, "top": 68, "bottom": 78}
    plot_w = width - margin["left"] - margin["right"]
    plot_h = height - margin["top"] - margin["bottom"]
    xs = [_effective_loops(r) for r in rows]
    ys = [_num(r.get("diff_insertions")) + _num(r.get("diff_deletions")) for r in rows]
    max_x = max(xs, default=1)
    max_y = max(ys, default=1)
    max_x = max(max_x, 1)
    max_y = max(max_y, 1)
    parts = _svg_header(width, height, title)
    for i in range(6):
        x = margin["left"] + plot_w * i / 5
        y = margin["top"] + plot_h - plot_h * i / 5
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{margin["top"]}" x2="{x:.1f}" y2="{margin["top"]+plot_h}"/>')
        parts.append(f'<line class="grid" x1="{margin["left"]}" y1="{y:.1f}" x2="{margin["left"]+plot_w}" y2="{y:.1f}"/>')
        parts.append(f'<text class="small" x="{x:.1f}" y="{margin["top"]+plot_h+18}" text-anchor="middle">{max_x*i/5:.0f}</text>')
        parts.append(f'<text class="small" x="{margin["left"]-8}" y="{y+4:.1f}" text-anchor="end">{max_y*i/5:.0f}</text>')
    parts.append(f'<text class="small" x="{margin["left"]+plot_w/2:.1f}" y="{height-24}" text-anchor="middle">effective loops (phase total, or legacy work loops)</text>')
    parts.append(f'<text class="small" x="24" y="{margin["top"]+plot_h/2:.1f}" transform="rotate(-90 24 {margin["top"]+plot_h/2:.1f})">diff churn (insertions + deletions)</text>')
    for r in rows:
        xval = _effective_loops(r)
        yval = _num(r.get("diff_insertions")) + _num(r.get("diff_deletions"))
        x = margin["left"] + plot_w * xval / max_x
        y = margin["top"] + plot_h - plot_h * yval / max_y
        variant = r.get("experiment_variant", "")
        color = VARIANT_COLORS.get(variant, "#777")
        task_type = r.get("task_type", "")
        radius = 5 if task_type == "feature" else 4
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{color}" fill-opacity="0.72"><title>{html.escape(str(r.get("task_id")))} {task_type}: loops={xval:.0f}, diff={yval:.0f}</title></circle>')
    lx, ly = width - margin["right"] + 16, margin["top"] + 8
    for j, variant in enumerate(VARIANT_ORDER):
        parts.append(f'<circle cx="{lx+7}" cy="{ly+j*24+7}" r="6" fill="{VARIANT_COLORS[variant]}"/>')
        parts.append(f'<text class="small" x="{lx+22}" y="{ly+j*24+12}">{html.escape(VARIANT_LABELS[variant])}</text>')
    _save_svg(path, parts + _svg_footer())


def _timeline_chart(path: Path, title: str, rows: list[dict[str, Any]]) -> None:
    points: dict[str, list[datetime]] = defaultdict(list)
    for row in rows:
        if row.get("status") != "completed":
            continue
        dt = _parse_dt(row.get("completed_at") or row.get("timestamp"))
        if dt:
            points[row.get("experiment_variant", "")].append(dt)
    all_times = [dt for vals in points.values() for dt in vals]
    if not all_times:
        _save_svg(path, _svg_header(800, 400, title) + ["<text x='40' y='80'>No completion timestamps</text>"] + _svg_footer())
        return
    start, end = min(all_times), max(all_times)
    span = max((end - start).total_seconds(), 1)
    width, height = 1040, 560
    margin = {"left": 86, "right": 170, "top": 68, "bottom": 88}
    plot_w = width - margin["left"] - margin["right"]
    plot_h = height - margin["top"] - margin["bottom"]
    max_count = max((len(v) for v in points.values()), default=1)
    parts = _svg_header(width, height, title)
    for i in range(6):
        y = margin["top"] + plot_h - plot_h * i / 5
        parts.append(f'<line class="grid" x1="{margin["left"]}" y1="{y:.1f}" x2="{margin["left"]+plot_w}" y2="{y:.1f}"/>')
        parts.append(f'<text class="small" x="{margin["left"]-8}" y="{y+4:.1f}" text-anchor="end">{max_count*i/5:.0f}</text>')
    for variant in VARIANT_ORDER:
        vals = sorted(points.get(variant, []))
        if not vals:
            continue
        coords = []
        for i, dt in enumerate(vals, 1):
            x = margin["left"] + plot_w * ((dt - start).total_seconds() / span)
            y = margin["top"] + plot_h - plot_h * (i / max_count)
            coords.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline points="{" ".join(coords)}" fill="none" stroke="{VARIANT_COLORS[variant]}" stroke-width="3"/>')
        for coord in coords:
            x, y = coord.split(",")
            parts.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{VARIANT_COLORS[variant]}"/>')
    parts.append(f'<text class="small" x="{margin["left"]}" y="{height-42}">{start.strftime("%Y-%m-%d %H:%M UTC")}</text>')
    parts.append(f'<text class="small" x="{margin["left"]+plot_w}" y="{height-42}" text-anchor="end">{end.strftime("%Y-%m-%d %H:%M UTC")}</text>')
    lx, ly = width - margin["right"] + 16, margin["top"] + 8
    for j, variant in enumerate(VARIANT_ORDER):
        parts.append(f'<line x1="{lx}" y1="{ly+j*24+7}" x2="{lx+16}" y2="{ly+j*24+7}" stroke="{VARIANT_COLORS[variant]}" stroke-width="3"/>')
        parts.append(f'<text class="small" x="{lx+22}" y="{ly+j*24+12}">{html.escape(VARIANT_LABELS[variant])}</text>')
    _save_svg(path, parts + _svg_footer())


def _summaries(events: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    final_counts: dict[str, Counter] = defaultdict(Counter)
    final_type_counts: dict[str, Counter] = defaultdict(Counter)
    for task in tasks:
        variant = task["variant"]
        final_counts[variant][task["status"]] += 1
        final_type_counts[variant][f'{task["type"]}:{task["status"]}'] += 1

    completed_events = [r for r in events if r.get("status") == "completed"]
    non_infra_events = [r for r in events if not r.get("infrastructure_failure")]
    summary_rows: list[dict[str, Any]] = []
    for variant in sorted({t["variant"] for t in tasks} | {e.get("experiment_variant") for e in events}, key=_variant_sort_key):
        ev = [r for r in events if r.get("experiment_variant") == variant]
        comp = [r for r in completed_events if r.get("experiment_variant") == variant]
        final = [t for t in tasks if t["variant"] == variant]
        source_features = [
            t for t in final
            if t["type"] == "feature"
            and t["source_task_id"].startswith("void-patrol-t")
            and "genesis" not in t["source_task_id"]
        ]
        feature_events = [
            r for r in comp
            if r.get("task_type") == "feature"
            and str(r.get("source_task_id", "")).startswith("void-patrol-t")
        ]
        row = {
            "variant": variant,
            "label": VARIANT_LABELS.get(variant, variant),
            "final_tasks": len(final),
            "completed_final_tasks": final_counts[variant]["completed"],
            "failed_final_tasks": final_counts[variant]["failed"],
            "cancelled_final_tasks": final_counts[variant]["cancelled"],
            "terminal_completion_rate": final_counts[variant]["completed"] / len(final) if final else 0,
            "source_features_completed": sum(1 for t in source_features if t["status"] == "completed"),
            "source_features_total": len(source_features),
            "source_feature_attempts_sum": sum(int(t.get("attempts") or 0) for t in source_features),
            "bug_tasks_final": sum(1 for t in final if t["type"] == "bug"),
            "research_tasks_final": sum(1 for t in final if t["type"] == "research"),
            "tail_tasks_final": sum(1 for t in final if t["type"] in {"art_pass", "polish", "harness_qa", "qa"}),
            "event_rows": len(ev),
            "completed_event_rows": len(comp),
            "infra_event_rows": sum(1 for r in ev if r.get("infrastructure_failure")),
            "validation_pass_rate_completed": _mean([1.0 if r.get("validation_passed") else 0.0 for r in comp]),
            "pipeline_validation_pass_rate_completed": _mean([1.0 if r.get("pipeline_validation_passed") else 0.0 for r in comp]),
            "avg_effective_loops_completed": _mean([_effective_loops(r) for r in comp]),
            "median_effective_loops_completed": _median([_effective_loops(r) for r in comp]),
            "avg_total_phase_loops_completed": _mean([_num(r.get("total_phase_loops")) for r in comp]),
            "median_total_phase_loops_completed": _median([_num(r.get("total_phase_loops")) for r in comp]),
            "avg_work_loops_completed": _mean([_num(r.get("work_loops")) for r in comp]),
            "avg_repair_attempts_completed": _mean([_num(r.get("repair_attempts")) for r in comp]),
            "avg_feature_effective_loops": _mean([_effective_loops(r) for r in feature_events]),
            "avg_feature_total_phase_loops": _mean([_num(r.get("total_phase_loops")) for r in feature_events]),
            "avg_feature_work_loops": _mean([_num(r.get("work_loops")) for r in feature_events]),
            "avg_diff_churn_completed": _mean([_num(r.get("diff_insertions")) + _num(r.get("diff_deletions")) for r in comp]),
        }
        summary_rows.append(row)

    return {
        "summary_rows": summary_rows,
        "final_counts": {k: dict(v) for k, v in final_counts.items()},
        "final_type_counts": {k: dict(v) for k, v in final_type_counts.items()},
        "event_status_counts": {k: dict(Counter(r.get("status") for r in events if r.get("experiment_variant") == k)) for k in VARIANT_ORDER},
        "completed_events": len(completed_events),
        "non_infra_events": len(non_infra_events),
    }


def _build_chart_inputs(events: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    variants = [v for v in VARIANT_ORDER if any(t["variant"] == v for t in tasks)]
    final_status = {
        v: dict(Counter(t["status"] for t in tasks if t["variant"] == v))
        for v in variants
    }
    completed = [r for r in events if r.get("status") == "completed"]

    validation_rate = {}
    avg_loops = {}
    recovery_churn = {}
    phase_comp: dict[str, dict[str, float]] = {}
    type_loops: dict[str, dict[str, float]] = {}
    for v in variants:
        rows = [r for r in completed if r.get("experiment_variant") == v]
        validation_rate[v] = _mean([1.0 if r.get("validation_passed") else 0.0 for r in rows])
        avg_loops[v] = _mean([_effective_loops(r) for r in rows])
        source_feature_total = sum(
            1 for t in tasks
            if t["variant"] == v and t["type"] == "feature" and t["source_task_id"].startswith("void-patrol-t")
        ) or 1
        recovery_churn[v] = sum(1 for t in tasks if t["variant"] == v and t["type"] in {"bug", "research"}) / source_feature_total
        feature_rows = [
            r for r in rows
            if r.get("task_type") == "feature"
            and str(r.get("source_task_id", "")).startswith("void-patrol-t")
        ]
        phase_comp[v] = {}
        for phase in ["plan", "scout", "work", "repair_work", "validate", "repair_validate"]:
            phase_comp[v][phase] = _mean([_num((r.get("phase_loops") or {}).get(phase)) for r in feature_rows])
        type_loops[v] = {}
        for task_type in ["feature", "bug", "research", "art_pass", "polish", "harness_qa"]:
            type_rows = [r for r in rows if r.get("task_type") == task_type]
            type_loops[v][task_type] = _mean([_effective_loops(r) for r in type_rows])

    scatter_rows = [
        r for r in completed
        if r.get("task_type") in {"feature", "art_pass", "polish", "harness_qa", "bug"}
        and not r.get("infrastructure_failure")
    ]

    return {
        "variants": variants,
        "final_status": final_status,
        "validation_rate": validation_rate,
        "avg_loops": avg_loops,
        "recovery_churn": recovery_churn,
        "phase_comp": phase_comp,
        "type_loops": type_loops,
        "scatter_rows": scatter_rows,
    }


def _write_markdown(out_dir: Path, summary: dict[str, Any], charts: list[str]) -> None:
    rows = summary["summary_rows"]
    lines = [
        "# Run 7 Quantitative Analysis",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"Experiment id: `{EXPERIMENT_ID}`",
        "",
        "## Headline Metrics",
        "",
        "| Variant | Final completion | Source features | Validation pass | Avg total phase loops | Bug+research per source feature | Infra rows |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        source_ratio = f'{int(row["source_features_completed"])}/{int(row["source_features_total"])}'
        recovery_ratio = (row["bug_tasks_final"] + row["research_tasks_final"]) / (row["source_features_total"] or 1)
        lines.append(
            f'| {row["label"]} | {row["terminal_completion_rate"]:.1%} | {source_ratio} | '
            f'{row["validation_pass_rate_completed"]:.1%} | {row["avg_total_phase_loops_completed"]:.1f} | '
            f'{recovery_ratio:.2f} | {int(row["infra_event_rows"])} |'
        )
    lines += [
        "",
        "## Charts",
        "",
    ]
    for chart in charts:
        lines.append(f"![{chart}](charts/{chart})")
        lines.append("")
    lines += [
        "## Interpretation Notes",
        "",
        "- Completion and closure were green for every run 7 project, so artifact quality needs separate qualitative review.",
        "- Wall-clock speed is not treated as primary evidence because runs shared a fixed agent/provider capacity pool.",
        "- Infrastructure failures should be censored or labelled rather than interpreted as project-code failures.",
        "- `variant-f` has no structured phase loops for main work, so phase-loop charts understate its legacy loop budget compared with pipeline variants.",
        "- Tail tasks matter for game quality; charts split by task type where possible to avoid mixing feature implementation with art/polish/QA passes.",
        "",
        "## Files",
        "",
        "- `summary_by_variant.csv`",
        "- `event_rows.csv`",
        "- `task_rows.csv`",
        "- `summary.json`",
    ]
    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    charts_dir = OUT_DIR / "charts"
    charts_dir.mkdir(exist_ok=True)

    events = _load_jsonl(EVENTS_PATH)
    tasks = _load_tasks()
    summary = _summaries(events, tasks)
    chart_inputs = _build_chart_inputs(events, tasks)

    # Flatten JSON-ish cells for CSV consumers.
    event_rows = []
    for row in events:
        flat = dict(row)
        for key in ["pipeline_variant", "phase_order", "phases_completed", "phase_timings", "phase_loops"]:
            flat[key] = json.dumps(flat.get(key, {}), separators=(",", ":"))
        event_rows.append(flat)
    task_rows = []
    for row in tasks:
        flat = dict(row)
        flat["metadata"] = json.dumps(flat.get("metadata", {}), separators=(",", ":"))
        task_rows.append(flat)

    _write_csv(OUT_DIR / "summary_by_variant.csv", summary["summary_rows"])
    _write_csv(OUT_DIR / "event_rows.csv", event_rows)
    _write_csv(OUT_DIR / "task_rows.csv", task_rows)
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    charts: list[str] = []
    _stacked_bar_chart(
        charts_dir / "terminal_task_outcomes_by_variant.svg",
        "Run 7 Terminal Task Outcomes by Variant",
        chart_inputs["final_status"],
        ["completed", "failed", "cancelled", "pending"],
        STATUS_COLORS,
    )
    charts.append("terminal_task_outcomes_by_variant.svg")

    _bar_chart(
        charts_dir / "validation_pass_rate_completed_events.svg",
        "Run 7 Validation Pass Rate on Completed Events",
        chart_inputs["validation_rate"],
        ylabel="pass rate",
    )
    charts.append("validation_pass_rate_completed_events.svg")

    _bar_chart(
        charts_dir / "avg_total_phase_loops_completed.svg",
        "Run 7 Average Total Phase Loops on Completed Events",
        chart_inputs["avg_loops"],
        ylabel="loops",
    )
    charts.append("avg_total_phase_loops_completed.svg")

    _stacked_bar_chart(
        charts_dir / "feature_phase_loop_composition.svg",
        "Run 7 Feature Task Phase Loop Composition",
        chart_inputs["phase_comp"],
        ["plan", "scout", "work", "repair_work", "validate", "repair_validate"],
        PHASE_COLORS,
    )
    charts.append("feature_phase_loop_composition.svg")

    _grouped_bar_chart(
        charts_dir / "avg_total_loops_by_task_type.svg",
        "Run 7 Average Total Phase Loops by Task Type",
        chart_inputs["type_loops"],
        ["feature", "bug", "research", "art_pass", "polish", "harness_qa"],
        {
            "feature": "#1565c0",
            "bug": "#c62828",
            "research": "#6d4c41",
            "art_pass": "#8e24aa",
            "polish": "#00897b",
            "harness_qa": "#f9a825",
        },
    )
    charts.append("avg_total_loops_by_task_type.svg")

    _bar_chart(
        charts_dir / "bug_research_churn_per_source_feature.svg",
        "Run 7 Bug + Research Tasks per Original Source Feature",
        chart_inputs["recovery_churn"],
        ylabel="tasks/source feature",
    )
    charts.append("bug_research_churn_per_source_feature.svg")

    _scatter_chart(
        charts_dir / "loops_vs_diff_churn_scatter.svg",
        "Run 7 Total Loops vs Diff Churn",
        chart_inputs["scatter_rows"],
    )
    charts.append("loops_vs_diff_churn_scatter.svg")

    _timeline_chart(
        charts_dir / "cumulative_completions_timeline.svg",
        "Run 7 Cumulative Completed Events Over Time",
        events,
    )
    charts.append("cumulative_completions_timeline.svg")

    _write_markdown(OUT_DIR, summary, charts)
    print(OUT_DIR)


if __name__ == "__main__":
    main()
