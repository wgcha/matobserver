"""픽스처 재생성 스크립트 (Impl-Solver). `python tests/fixtures/solver/make_fixtures.py` — 결정적 출력.

* reference_binout/binout + reference_curve.csv (S-5): 합성 binout(실제 LS-DYNA 디렉터리 모양)과 해석적 기대 곡선.
  rcforc 시간축(41점)과 nodout 시간축(61점)을 다르게 둬 공통 시간축 보간을 검사한다. 변위는 시간에 선형이라
  보간이 정확하다. 기대값: x = 0.8·t [mm] → m, y = −(−F_q)·4 [N] (master z, sign −1, quarter 배율 4).
* logs/*.txt (S-4): 종료 판정 로그 샘플.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from binout_writer import write_binout  # noqa: E402

CID = 7
INDENTER = 5000001
SUPPORT = 5000031
MIDSPAN = 1


def f_quarter(t: np.ndarray) -> np.ndarray:
    """quarter 모델 압자 반력 크기 [N] (비선형, 단조)."""
    return 25.0 * t + 3.0 * t ** 2 - 0.2 * t ** 3


def make_reference_binout() -> None:
    out = HERE / "reference_binout"
    out.mkdir(exist_ok=True)
    ty = np.linspace(0.0, 5.0, 41)
    tx = np.linspace(0.0, 5.0, 61)
    fq = f_quarter(ty)
    ids = [3, 3, CID, CID]  # 다른 접촉(3) 섞음
    side = [0, 1, 0, 1]
    z = np.stack([0.5 * fq, -0.5 * fq, fq, -fq], axis=1)
    rc = {"metadata": {"ids": ids, "side": side}, "time": ty,
          "series": {"x_force": 0.01 * z, "y_force": -0.02 * z, "z_force": z}}
    nodes = [MIDSPAN, INDENTER, SUPPORT]
    uz = np.stack([-0.7 * tx, -0.8 * tx, -0.05 * tx], axis=1)  # mm
    nd = {"metadata": {"ids": nodes}, "time": tx,
          "series": {"x_displacement": 0.0 * uz, "y_displacement": 0.0 * uz, "z_displacement": uz}}
    ke = 0.002 * ty ** 2
    ie = 1.0 + ty ** 2
    gl = {"time": ty, "series": {"kinetic_energy": ke, "internal_energy": ie}}
    write_binout(out / "binout", {"rcforc": rc, "nodout": nd, "glstat": gl})
    x_m = 0.8 * ty * 1e-3
    y_n = 4.0 * fq
    rel_m = (0.7 - 0.05) * ty * 1e-3  # midspan − support, sign −1
    with open(out / "reference_curve.csv", "w") as f:
        f.write("# S-5 expected: t[s], x_crosshead[m], force[N], x_deflection[m]\n")
        for row in zip(ty, x_m, y_n, rel_m):
            f.write(",".join(repr(float(v)) for v in row) + "\n")


LOGS = {
    "normal_implicit.txt": " Version : smp d R13.1.1\n ...\n N o r m a l    t e r m i n a t i o n   11/08/26 10:00:00\n",
    "normal_mpp.txt": "     |  Version : mpp s R12.0.0                      |\n Normal termination\n",
    "error.txt": " Version : smp d R13.1.1\n *** Error 10111 (KEY+111)\n E r r o r    t e r m i n a t i o n   11/08/26\n",
    "license.txt": " Version : smp d R13.1.1\n ***  LICENSE ERROR : Unable to checkout license (FlexNet)\n E r r o r    t e r m i n a t i o n\n",
    "license_no_term.txt": "lmgrd is not running: License server machine is down or not responding.\n",
    "diverged.txt": " Version : smp d R13.1.1\n *** Error 70031: nonlinear solver failed to converge\n E r r o r    t e r m i n a t i o n\n",
    "truncated.txt": " Version : smp d R13.1.1\n cycle 1000 t 0.4\n",
    "penetration.txt": (" Version : smp d R13.1.1\n *** Warning 40513 (SOL+513)\n"
                        "   initial penetration of node 12 = 1.2000E-03\n"
                        "   initially penetrating node 14 penetration = 3.5000E-03\n"
                        " N o r m a l    t e r m i n a t i o n\n"),
}


def make_logs() -> None:
    d = HERE / "logs"
    d.mkdir(exist_ok=True)
    for name, text in LOGS.items():
        (d / name).write_text(text)


if __name__ == "__main__":
    make_reference_binout()
    make_logs()
