from swarm.closure.project_seeds import (
    get_representative_project_seed,
    list_representative_project_seeds,
)


def test_representative_project_seeds_returns_list():
    seeds = list_representative_project_seeds()
    assert isinstance(seeds, list)


def test_get_representative_project_seed_raises_for_unknown():
    try:
        get_representative_project_seed("nonexistent-project")
        assert False, "Expected KeyError"
    except KeyError:
        pass
