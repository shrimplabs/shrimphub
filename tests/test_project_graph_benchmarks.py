import json
from pathlib import Path


def test_project_graph_benchmark_fixture_schema():
    fixture_path = Path(__file__).parent / "fixtures" / "project_graph_benchmarks.json"
    data = json.loads(fixture_path.read_text())

    assert isinstance(data, list)
    assert len(data) >= 3

    for item in data:
        assert item["id"]
        assert item["difficulty"] in {"vague", "medium", "detailed"}
        assert isinstance(item["prompt"], str) and item["prompt"].strip()
        props = item["expected_properties"]
        assert props["min_tasks"] >= 1
        assert props["max_tasks"] >= props["min_tasks"]
        assert isinstance(props["reject_star"], bool)
        assert isinstance(props["reject_chain"], bool)
        assert props["require_parallel_branches"] >= 0
        assert props["require_convergences"] >= 0
        if "semantic_expectations" in props:
            assert all(isinstance(x, str) and x for x in props["semantic_expectations"])
