"""Representative project closure seeds for rollout and manual application.

These are intentionally narrow starter contracts. They are not auto-applied to
projects; operators can review and POST them through the closure API when ready.

If no seed exists for a project, ``propose_project_closure`` falls through to
auto-generated profile-based proposals (TypeScript / Godot / Python).

To add a seed for your own project, add an entry to ``_RAW_PROJECT_SEEDS``
following the pattern below, then POST it via the closure API:

    POST /api/closure/projects/<project-name>/spec
    Body: the normalized closure_spec dict

Example seed (replace values for your project):

    "my-godot-game": {
        "project": "my-godot-game",
        "profile": "godot",
        "closure_spec": {
            "mode": "stabilize",
            "boot": {
                "ready_check": {"type": "process"},
            },
            "verification": {
                "smoke_checks": [
                    {
                        "id": "vertical-slice",
                        "type": "command",
                        "command": "godot --headless --path . -s tests/test_vertical_slice.gd",
                    }
                ],
            },
            "critical_flows": [
                {
                    "id": "vertical-slice",
                    "description": "Reach the first meaningful game loop from the main menu.",
                }
            ],
            "gates": {"critical_flow_count": 1, "max_open_regressions": 0},
        },
        "assumptions": [
            "A headless Godot CLI is available on PATH.",
            "The vertical slice test is the representative closure flow.",
        ],
    },
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from swarm.closure.specs import normalize_project_spec


_RAW_PROJECT_SEEDS: dict[str, dict[str, Any]] = {}


def list_representative_project_seeds() -> list[dict[str, Any]]:
    return [get_representative_project_seed(name) for name in _RAW_PROJECT_SEEDS]


def get_representative_project_seed(project_name: str) -> dict[str, Any]:
    raw = _RAW_PROJECT_SEEDS[project_name]
    seed = deepcopy(raw)
    seed["closure_spec"] = normalize_project_spec(seed["closure_spec"], seed["profile"])
    return seed
