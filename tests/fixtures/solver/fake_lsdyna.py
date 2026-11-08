"""가짜 LS-DYNA 배치 스크립트 (시험 전용, Impl-Solver). 사용: `python fake_lsdyna.py -i <main.k> -n <ncpu>`.

실제 솔버 대신 d3hsp/messag/binout 샘플을 쓴다. 동작은 환경변수 `FAKE_LSDYNA_BEHAVIOR`(JSON 파일 경로)로 고른다:

    {"implicit": "normal"|"error"|"diverge"|"hang"|"license"|"license_then_normal",
     "explicit": ...,                      # 위와 같음 (기본 normal)
     "license_failures": 2,               # license_then_normal 의 실패 횟수
     "contact_id": 1, "penetration_mm": null, "first_force_N": 0.0,
     "stiffness_scale": 1.0,              # 보 이론 강성 배율 (단위 오류 모사)
     "node_scale": {"<nid>": 0.9}}        # 히스토리 노드별 z 변위 배율 (기본 1.0)

물리: 3점굽힘 보 이론 F = 48EI/L³·δ (σy가 있으면 F_y = 2σy·b·h²/(3L) 이후 기울기 10%), 대칭 배율로 나눈 힘을
압자 접촉 master(z 음수)/slave(z 양수)에 쓴다. 1요소 덱(제목 "matobserver single element uniaxial")은 단축 J2 응답.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from binout_writer import write_binout  # noqa: E402

from matobserver.deck import kfile  # noqa: E402
from matobserver.deck.materials import read_materials  # noqa: E402

NORMAL = " N o r m a l    t e r m i n a t i o n"
ERROR = " E r r o r    t e r m i n a t i o n"
HEADER = "     |  Version : smp d R13.1.1                      |\n"


def behavior() -> dict:
    p = os.environ.get("FAKE_LSDYNA_BEHAVIOR")
    if p and Path(p).is_file():
        return json.loads(Path(p).read_text())
    return {}


def write_logs(d: Path, body: str, term: str | None) -> None:
    text = HEADER + body + ("\n" + term + "\n" if term else "\n")
    (d / "d3hsp").write_text(text)
    (d / "messag").write_text(text)


def mode_of(d: Path) -> str:
    c = d / "mo_controls.k"
    if c.is_file() and "*CONTROL_IMPLICIT" in c.read_text().upper():
        return "implicit"
    return "explicit"


def bump_counter(d: Path, name: str) -> int:
    p = d / f"fake_{name}.count"
    n = int(p.read_text()) if p.is_file() else 0
    p.write_text(str(n + 1))
    return n + 1


def uniaxial(d: Path, main: Path) -> None:
    blocks = kfile.read_k(main)
    mats = read_materials((d / "mo_mat_se.k").read_text())
    mat = mats[1].material
    E = mat.elastic.youngs_modulus / 1e6  # MPa
    tab = mat.table
    dt0 = float(kfile.split_fields(next(b for b in blocks if b.name == "CONTROL_IMPLICIT_GENERAL").lines[0])[1])
    curve = next(b for b in blocks if b.name == "DEFINE_CURVE")
    pts = [kfile.split_fields(ln, 20) for ln in curve.lines[1:]]
    t_end, u_end = float(pts[-1][0]), float(pts[-1][1])
    times = np.arange(0.0, t_end + 0.5 * dt0, dt0)
    u = u_end * times / t_end
    eps = np.log1p(u)
    sig = np.zeros_like(eps)
    if tab is None:
        sig = E * eps
    else:
        ep_t = np.array([r.plastic_strain for r in tab])
        s_t = np.array([r.true_stress for r in tab]) / 1e6

        def h(ep):  # 마지막 구간 기울기로 외삽
            if ep <= ep_t[-1]:
                return float(np.interp(ep, ep_t, s_t))
            k = (s_t[-1] - s_t[-2]) / (ep_t[-1] - ep_t[-2])
            return float(s_t[-1] + k * (ep - ep_t[-1]))

        for i, e in enumerate(eps):
            if E * e <= s_t[0]:
                sig[i] = E * e
                continue
            lo, hi = 0.0, e
            for _ in range(200):
                mid = 0.5 * (lo + hi)
                if E * (e - mid) > h(mid):
                    lo = mid
                else:
                    hi = mid
            sig[i] = E * (e - 0.5 * (lo + hi))
    write_binout(d / "binout", {
        "elout/solid": {"metadata": {"ids": [1]}, "time": times, "series": {"sig_xx": sig}},
        "nodout": {"metadata": {"ids": [7]}, "time": times, "series": {"x_displacement": u}},
    })
    write_logs(d, "single element\n", NORMAL)


def bending(d: Path, beh: dict, mode: str) -> None:
    params = kfile.read_parameters(kfile.read_k(d / "mo_params.k"))
    tend, dend, dtout = params["mo_tend"], params["mo_dend"], params["mo_dtout"]
    span = params.get("span", 64.0)
    mesh = kfile.read_mesh(d / "mo_specimen.k")
    xyz = np.array(list(mesh.nodes.values()))
    b = 2.0 * (xyz[:, 1].max() - xyz[:, 1].min())  # quarter: 폭 절반만 메시
    h = xyz[:, 2].max() - xyz[:, 2].min()
    mats = {}
    for p in sorted(d.glob("mo_mat_*.k")):
        mats.update(read_materials(p.read_text()))
    m = next(iter(mats.values())).material
    E = m.elastic.youngs_modulus / 1e6
    k = 4.0 * E * b * h ** 3 / span ** 3 * float(beh.get("stiffness_scale", 1.0))
    fy = None
    if m.table is not None:
        fy = 2.0 * (m.table[0].true_stress / 1e6) * b * h * h / (3.0 * span)
    n = int(round(tend / dtout))
    t = np.linspace(0.0, tend, n + 1)
    delta = dend * t / tend
    F = k * delta
    if fy is not None:
        dy = fy / k
        F = np.where(delta <= dy, F, fy + 0.1 * k * (delta - dy))
    F = F + np.where(t > 0, float(beh.get("first_force_N", 0.0)), 0.0) * (t == t[1])
    scale = 4.0  # quarter
    fq = F / scale
    cid = int(beh.get("contact_id", 1))
    hist = []
    out = kfile.read_k(d / "mo_output.k")
    for blk in out:
        if blk.name == "DATABASE_HISTORY_NODE":
            for ln in blk.lines:
                hist += [int(x) for x in kfile.split_fields(ln) if x]
    ns = beh.get("node_scale", {})
    disp = np.array([[-delta[i] * float(ns.get(str(nid), 1.0)) for nid in hist] for i in range(len(t))])
    ie = np.concatenate([[0.0], np.cumsum(0.5 * (F[1:] + F[:-1]) * np.diff(delta))]) / scale
    ke = (0.001 if mode == "explicit" else 0.0) * ie
    write_binout(d / "binout", {
        "rcforc": {"metadata": {"ids": [cid, cid], "side": [0, 1]}, "time": t,
                   "series": {"x_force": np.zeros((len(t), 2)), "y_force": np.zeros((len(t), 2)),
                              "z_force": np.stack([fq, -fq], axis=1)}},
        "nodout": {"metadata": {"ids": hist}, "time": t,
                   "series": {"x_displacement": np.zeros_like(disp), "y_displacement": np.zeros_like(disp),
                              "z_displacement": disp}},
        "glstat": {"time": t, "series": {"kinetic_energy": ke, "internal_energy": ie}},
    })
    body = f"analysis {mode}\n"
    if beh.get("penetration_mm") is not None:
        body += f" *** Warning: initial penetration of node 12 = {float(beh['penetration_mm']):.4e}\n"
    write_logs(d, body, NORMAL)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", required=True)
    ap.add_argument("-n", default="1")
    a = ap.parse_args()
    main_file = Path(a.i)
    d = main_file.parent
    beh = behavior()
    if "single element uniaxial" in main_file.read_text():
        uniaxial(d, main_file)
        return 0
    mode = mode_of(d)
    action = beh.get(mode, "normal")
    if action == "hang":
        (d / "d3hsp").write_text(HEADER + "running...\n")
        time.sleep(600)
        return 0
    if action == "error":
        write_logs(d, "*** Error 10234 (KEY+234) bad deck\n", ERROR)
        return 1
    if action == "diverge":
        write_logs(d, " *** Error: nonlinear solver failed to converge\n", ERROR)
        return 1
    if action == "license" or (action == "license_then_normal"
                               and bump_counter(d, "license") <= int(beh.get("license_failures", 1))):
        print("***  LICENSE ERROR: Unable to checkout license for LS-DYNA (FlexNet)")
        return 3
    bending(d, beh, mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
