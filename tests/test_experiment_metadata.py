from pathlib import Path

from swarm.experiment_metadata import stamp_experiment_metadata


def test_stamp_experiment_metadata_applies_fixed_project_pipeline():
    config = {
        "project_pipelines": {
            "game-a": {
                "*": ["plan", "work", "validate"],
                "_experiment": {
                    "experiment_id": "exp-1",
                    "experiment_arm": "confirmatory",
                    "experiment_variant": "variant-a",
                    "pipeline_mode": "fixed",
                    "pipeline": ["plan", "work", "validate"],
                    "source_project": "source-game",
                },
            }
        }
    }

    meta = stamp_experiment_metadata("game-a", {"is_integration_task": True}, config=config)

    assert meta["is_integration_task"] is True
    assert meta["experiment_id"] == "exp-1"
    assert meta["experiment_variant"] == "variant-a"
    assert meta["source_project"] == "source-game"
    assert meta["pipeline"] == ["plan", "work", "validate"]
    assert meta["pipeline_variant"] == ["plan", "work", "validate"]
    assert meta["phase_order"] == ["plan", "work", "validate"]
    assert meta["is_valid_order"] is True


def test_stamp_experiment_metadata_respects_explicit_pipeline():
    config = {
        "project_pipelines": {
            "game-a": {
                "_experiment": {
                    "experiment_variant": "variant-a",
                    "pipeline_mode": "fixed",
                    "pipeline": ["plan", "work", "validate"],
                }
            }
        }
    }

    meta = stamp_experiment_metadata("game-a", {"pipeline": ["work"], "experiment_variant": "custom"}, config=config)

    assert meta == {"pipeline": ["work"], "experiment_variant": "custom"}


def test_stamp_experiment_metadata_repairs_partial_pipeline_metadata():
    config = {
        "project_pipelines": {
            "game-a": {
                "_experiment": {
                    "experiment_id": "exp-1",
                    "experiment_arm": "confirmatory",
                    "experiment_variant": "variant-a",
                    "pipeline_mode": "fixed",
                    "pipeline": ["plan", "work", "validate"],
                }
            }
        }
    }

    meta = stamp_experiment_metadata(
        "game-a",
        {"pipeline_variant": ["plan", "scout", "work", "validate"]},
        config=config,
    )

    assert meta["experiment_variant"] == "variant-a"
    assert meta["pipeline"] == ["plan", "work", "validate"]
    assert meta["pipeline_variant"] == ["plan", "work", "validate"]


def test_stamp_experiment_metadata_reads_config_file(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "project_pipelines": {
            "game-b": {
              "_experiment": {
                "experiment_id": "exp-file",
                "experiment_arm": "exploratory",
                "experiment_variant": "variant-b",
                "pipeline_mode": "fixed",
                "pipeline": ["plan", "scout", "synthesize", "work", "validate"]
              }
            }
          }
        }
        """
    )
    monkeypatch.chdir(tmp_path)

    meta = stamp_experiment_metadata("game-b", {})

    assert meta["experiment_id"] == "exp-file"
    assert meta["experiment_variant"] == "variant-b"
    assert meta["pipeline"] == ["plan", "scout", "synthesize", "work", "validate"]
