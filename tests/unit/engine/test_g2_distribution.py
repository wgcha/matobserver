"""G2 1.2 기준 (module-boundaries.md §5.3 O-4·O-5): Verifier 조건, 노이즈 0% 개별 + 0.5% 시드 1–13 분포.

노이즈 0%: O-4 E ≤1%, O-5 J ≤0.01·테이블 ≤3%. 노이즈 0.5%(가산 σ = 0.005·max|F|): E 오차 중앙값 ≤0.5%·최대 ≤1.5%,
테이블 노드 오차 중앙값 ≤3%·최대 ≤5%(coeff·table 각각), J/J(참값) ≤1.2 전 시드. 분포 시험은 `slow` 마커(기본 실행 포함,
`-m "not slow"`로 뺄 수 있음 — 그때도 노이즈 0% Verifier 조건 시험은 남는다).
"""

from __future__ import annotations

import numpy as np
import pytest

import g2_seeds as G


@pytest.mark.parametrize("model", ["mat001", "mat024_coeff", "mat024_table"])
def test_verifier_condition_noise_free(tmp_path, model):
    r = G.run_one(model, 0.0, 0, root=tmp_path)
    if model == "mat001":
        assert abs(r["E_err"]) <= 0.01
        return
    assert r["J"] <= 0.01 and r["tab_err"] <= 0.03, r
    assert abs(r["res"]["slots"][0]["valid_range"]["max_plastic_strain"] / r["ep_true"] - 1) <= 0.07


@pytest.mark.slow
def test_o4_distribution_mat001():
    E = np.abs([G.run_one("mat001", 0.005, s)["E_err"] for s in G.SEEDS])
    assert np.median(E) <= 0.005 and E.max() <= 0.015, E


@pytest.mark.slow
@pytest.mark.parametrize("model", ["mat024_coeff", "mat024_table"])
def test_o5_distribution(model):
    rows = [G.run_one(model, 0.005, s) for s in G.SEEDS]
    E = np.abs([r["E_err"] for r in rows])
    tab = np.array([r["tab_err"] for r in rows])
    ratio = np.array([r["J"] / r["J_true"] for r in rows])
    assert np.median(E) <= 0.005 and E.max() <= 0.015, E
    assert np.median(tab) <= 0.03 and tab.max() <= 0.05, tab
    assert np.all(ratio <= 1.2), ratio
