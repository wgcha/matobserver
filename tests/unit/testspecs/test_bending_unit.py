"""bending TestSpec 단위: 응답(처짐계 alt_axes), engine_params(§10.4), 초기값 식(§10.5)."""

from __future__ import annotations

import numpy as np
import pytest

import mo_optim_doubles as D
from matobserver.core import types as T
from matobserver.testspecs.bending import BendingTestSpec


def test_responses_axes():
    man = D.make_manifest()
    ts = BendingTestSpec({})
    r = ts.responses(man, "displacement")[0]
    assert r.axis == "displacement" and r.extractor["x"].name == "nodout_disp"
    assert r.extractor["y"].args["scale"] == "symmetry"
    rd = ts.responses(man, "deflection")[0]
    assert rd.axis == "deflection" and rd.extractor["x"].name == "nodout_rel_disp"
    man2 = dict(man, responses=[{k: v for k, v in man["responses"][0].items() if k != "alt_axes"}])
    with pytest.raises(ValueError):
        ts.responses(man2, "deflection")
    assert ts.responses({}, "displacement")[0].extractor["y"].name == "rcforc_resultant"


def test_engine_params():
    ts = BendingTestSpec({"t_end_s": 1.0, "loading_velocity_m_s": 0.5, "overtravel_ratio": 0.05})
    mesh = T.MeshResult(None, 0, 0, {}, 0, 0, ((0, 0, 0), (1, 1, 1)), {}, {}, 0.004, 0.0, "quarter",
                        T.MeshQuality(1, 1, 1), 0.001, ())
    ep = ts.engine_params({"X_u": 0.006}, "implicit", mesh)
    assert ep == {"mo_dend": pytest.approx(0.0063), "mo_tend": 1.0, "mo_htop": 0.004, "mo_hbot": 0.0}
    assert ts.engine_params({"x_max": 0.006}, "explicit", mesh)["mo_tend"] == pytest.approx(0.0063 / 0.5)


def test_initial_hints_from_aligned_meta():
    ts = BendingTestSpec({})
    b, h, L, k0 = 0.01, 0.004, 0.064, 1500.0
    x = np.linspace(0, 0.006, 100)
    y = np.minimum(k0 * x, 5.0 + 0 * x + k0 * 0.002)
    d = T.TestData("r", "displacement", x, y, "crosshead", None, {}, {}, {"k0": k0, "x_elastic_end": 0.002, "X_u": 0.006})
    g = T.GeometrySpec("box", {"width": b, "thickness": h, "total_length": 0.08})
    hi = ts.initial_hints([d], g, {"span": L})
    assert hi["E0"] == pytest.approx(k0 * L**3 / (4 * b * h**3))
    assert hi["sigma_y0"] == pytest.approx(3 * np.interp(0.002, x, y) * L / (2 * b * h**2))
    assert hi["eps_p_max0"] == pytest.approx(max(6 * h * 0.006 / L**2 - hi["sigma_y0"] / hi["E0"], 0.01))
