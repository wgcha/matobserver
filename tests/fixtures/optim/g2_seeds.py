"""G2 1.2 분포 기준 실행기 (module-boundaries.md §5.3 O-4·O-5, 노이즈 모델·시드 1–13).

Verifier 조건: E 2.5 GPa, Swift K 90 MPa / ε0 0.01 / n 0.15, b 10 · h 4 · L 64 mm, δmax 6 mm, 크로스헤드 C 1.8e-7 m/N.
노이즈: 가산 가우스 σ = level·max|F| (F[0] = 0 유지), seed = 시드 번호.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

import mo_optim_doubles as D
from matobserver.core.config import Config
from matobserver.engine import run_calibration
from matobserver.engine.context import preprocess
from matobserver.engine.evaluate import Evaluator
from matobserver.solvers.surrogate.beam import Section, Uniaxial, three_point_bending

W, H, L, TL = 0.010, 0.004, 0.064, 0.080
VE, VSW, VDMAX, C = 2.5e9, (9.0e7, 0.01, 0.15), 0.006, 1.8e-7
SEEDS = tuple(range(1, 14))


def swift(ep):
    K, e0, n = VSW
    return K * (e0 + np.asarray(ep)) ** n


def curve(noise: float, seed: int, plastic: bool = True):
    ep = np.concatenate([[0.0], np.geomspace(1e-6, 0.3, 400)])
    sec = Section.from_boxes([("body", -W / 2, W / 2, 0.0, H)])
    d = np.linspace(0.0, VDMAX, 300)
    mat = Uniaxial.from_card(VE, list(zip(ep, swift(ep))) if plastic else None)
    r = three_point_bending(sec, [mat], L, d)
    F = r.force.copy()
    if noise > 0:
        F = F + np.random.default_rng(seed).normal(0.0, noise * np.max(np.abs(F)), F.shape)
        F[0] = 0.0
    return d + C * F, F, r.eps_p_max["body"]


def run_one(model: str, noise: float, seed: int, *, root: Path | None = None, cfg=None, params=None):
    root = Path(root or tempfile.mkdtemp())
    x, F, ep_true = curve(noise, seed, plastic=model != "mat001")
    t = D.make_test("t1", [D.make_run("r", x, F, width=W, thickness=H, span=L)], D.make_manifest(),
                    {"kind": "box", "dimensions": {"total_length": TL}}, store_dir=root)
    slot = {"slot": "body", "role": "identify", "material_model": model, "reference": D.reference()}
    if params:
        slot["params"] = params
    svc = D.FakeServices(root, D.make_registry(), Config(cfg or {}))
    res = run_calibration(D.make_spec([t], [slot], optimizer=("", {}), elastic_optimizer=("", {})), svc)
    out = {"res": res, "ep_true": ep_true, "E_err": res["slots"][0]["card_blocks"]["elastic"]["youngs_modulus"] / VE - 1}
    if model != "mat001":
        K, e0, n = VSW
        truth = D.make_spec([t], [{"slot": "body", "role": "identify", "material_model": "mat024_coeff",
                                   "reference": D.reference()}])
        ev = Evaluator(preprocess(truth, svc), svc, "surrogate_beam", {}, 60)
        out["J_true"] = ev.evaluate({"body.E": VE, "body.K": K, "body.eps0": e0, "body.n": n}, "plastic",
                                    elastic_only=False).J
        s = res["slots"][0]
        tb = s["card_blocks"]["table"]
        ep = np.array([r["plastic_strain"] for r in tb])
        sg = np.array([r["true_stress"] for r in tb])
        m = ep <= s["valid_range"]["max_plastic_strain"] * (1 + 1e-9)
        out["tab_err"] = float(np.max(np.abs(sg[m] / swift(ep[m]) - 1)))
        out["J"] = res["metadata"]["residuals"]["J"]
        eb = res["metadata"]["termination"]["e_by_stage"][0]
        out["E1_err"] = eb["elastic"] / VE - 1
        out["final_stage"] = res["metadata"]["termination"]["final_stage"]
    return out
