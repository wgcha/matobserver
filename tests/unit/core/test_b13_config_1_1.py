"""B-13: 설정 기본값 1.1 — 새 키, 변경된 기본값, 1.0 별칭 호환."""

from __future__ import annotations

from matobserver.core.config import Config, default_values


def test_new_defaults():
    c = Config({})
    assert c.get("alignment.elastic_dev_tol") == 0.01
    assert c.get("material.mat024_table.first_node_strain") == 0.002
    assert c.get("engine.stages.elastic.optimizer") == "lsq"
    assert c.get("engine.stages.plastic.optimizer") == "lsq"


def test_legacy_alias_fills_new_key():
    c = Config({"engine.plastic_stage.optimizer": "bo", "engine.elastic_stage.optimizer": "cmaes"})
    assert c.get("engine.stages.plastic.optimizer") == "bo"
    assert c.get("engine.stages.elastic.optimizer") == "cmaes"


def test_new_key_wins_over_alias():
    c = Config({"engine.plastic_stage.optimizer": "bo", "engine.stages.plastic.optimizer": "cmaes"})
    assert c.get("engine.stages.plastic.optimizer") == "cmaes"


def test_legacy_alias_default_is_none():
    assert default_values()["engine.plastic_stage.optimizer"] is None
