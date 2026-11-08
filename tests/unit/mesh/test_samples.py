"""M-8 §2 규격 샘플(tests/fixtures/mesh/*.k) 재파싱 + 현재 코드 출력과 바이트 동일(회귀).

Impl-Solver `.k` 리더가 아직 없어 자체 고정 폭 파서(kfile.read_k)로 검사한다(M-8 대체 경로). 샘플은 Solver 와 공유.
샘플이 의도적으로 바뀌면 `python tests/fixtures/mesh/make_k_samples.py`로 다시 만든다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from matobserver.mesh import elements, kfile

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "mesh"
SAMPLES = {"hex8": "specimen_hex8.k", "tet4": "specimen_tet4.k", "tet10": "specimen_tet10.k"}


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_k_samples", FIX / "make_k_samples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.mark.parametrize("topology", list(SAMPLES))
def test_specimen_samples_parse(topology):
    kf = kfile.read_k((FIX / SAMPLES[topology]).read_bytes(), "specimen")
    assert kf.header_fields()["topology"] == topology
    conns = np.array([c for _p, c in kf.solids.values()])
    assert {kfile.solid_topology(tuple(c)) for c in conns} == {topology}
    nodes = {k: np.array(v) * 1e-3 for k, v in kf.nodes.items()}
    X = np.array([[nodes[n] for n in c] for c in conns])
    k = {"hex8": 8, "tet4": 4, "tet10": 10}[topology]
    vol = elements.volumes(topology, X[:, :k])
    assert (vol > 0).all()
    assert vol.sum() * 4 == pytest.approx(0.08 * 0.01 * 0.004, rel=1e-9)  # quarter
    assert set(kf.node_sets) == {901, 902, 4_900_001} and kf.part_sets == {900: (1,)}


def test_jig_sample_parse():
    kf = kfile.read_k((FIX / "jig_quarter.k").read_bytes(), "jig")
    assert set(kf.parts) == {1001, 1002} and set(kf.rigid_mats) == {1001, 1002}
    assert min(kf.nodes) >= 5_000_000


def test_samples_match_current_writer(tmp_path):
    gen = _load_generator()
    out = gen.generate(tmp_path)
    for key, path in out.items():
        name = SAMPLES.get(key, "jig_quarter.k")
        assert path.read_bytes() == (FIX / name).read_bytes(), f"{name} differs: regenerate samples if intended"



@pytest.mark.parametrize("name", [*SAMPLES.values(), "jig_quarter.k"])
def test_m8_solver_reader_cross_parse(name):
    """M-8: Impl-Solver `.k` 리더(matobserver.deck.kfile.read_mesh)로 재파싱 — 자체 파서와 내용 일치."""
    try:
        from matobserver.deck.kfile import read_mesh
    except (ImportError, AttributeError):
        pytest.skip("Impl-Solver .k reader not available")
    path = FIX / name
    md = read_mesh(path)
    kf = kfile.read_k(path.read_bytes(), "jig" if name.startswith("jig") else "specimen")
    assert md.nodes == kf.nodes
    assert md.solids == kf.solids and md.shells == kf.shells
    assert md.node_sets == {k: v for k, (_t, v) in kf.node_sets.items()}
    assert md.part_sets == kf.part_sets
    if md.solids:
        assert md.solid_kind() == kf.header_fields()["topology"]
