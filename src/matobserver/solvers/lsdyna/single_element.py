"""1요소 단축 인장 덱 (plugins.md §4.2 `single_element_uniaxial`, 계약 S11, result.md §4.9 왕복 검사).

1×1×1 mm hex 1개, 1/8 대칭, x=1 면에 변위 지정(implicit 정적, 고정 증분). 결과:
x = 유효소성변형률 εp = ln(1+u/L) − σ/E (단축 응력 상태의 탄성 변형률 = σ/E), y = 진응력 σ_xx [Pa].
σ_xx는 binout `elout/solid`(`stress_var`, 기본 `sig_xx`), u는 `nodout` 당김 면 노드의 `x_displacement`.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from ...core import units
from ...core.types import CurveData, NeutralMaterial, SlotBinding
from ...deck import kfile
from ...deck.materials import slot_file_text
from .binout import ResultSource, series

SLOT = "se"
PID, SECID, MID, LCID, LCID_PULL = 1, 1, 1, 100, 200
PULL_NODE = 7
MAT_FILE = f"mo_mat_{SLOT}.k"
MAIN_FILE = "main.k"

_NODES = [(1, 0, 0, 0), (2, 1, 0, 0), (3, 1, 1, 0), (4, 0, 1, 0), (5, 0, 0, 1), (6, 1, 0, 1), (7, 1, 1, 1), (8, 0, 1, 1)]
_SETS = [(1, (1, 4, 5, 8), "x = 0"), (2, (1, 2, 5, 6), "y = 0"), (3, (1, 2, 3, 4), "z = 0"), (4, (2, 3, 6, 7), "x = 1 (pulled)")]


def end_displacement_mm(material: NeutralMaterial, strain_max: float, margin: float = 1.05) -> float:
    """εp = strain_max 에 도달하는 당김 변위 [mm] (여유 margin 배)."""
    e = float(material.elastic.youngs_modulus)
    if material.table:
        eps = [r.plastic_strain for r in material.table]
        sig = [r.true_stress for r in material.table]
        s_end = float(np.interp(strain_max, eps, sig, right=sig[-1]))
        eps_total = strain_max + s_end / e
    else:
        eps_total = strain_max
    return (math.exp(margin * max(eps_total, 1e-6)) - 1.0) * 1.0


def binding() -> SlotBinding:
    return SlotBinding(slot=SLOT, part_ids=(PID,), mid=MID, secid=SECID,
                       section={"keyword": "SECTION_SOLID", "elform": 2}, lcid=LCID)


def write_deck(material: NeutralMaterial, strain_max: float, workdir: Path, steps: int = 100) -> Path:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / MAT_FILE).write_text(slot_file_text(binding(), material), encoding="ascii")
    t_end = 1.0
    dt = t_end / steps
    u_end = end_displacement_mm(material, strain_max)
    c = kfile.card
    lines = [
        "*KEYWORD",
        "$ matobserver single-element uniaxial tension (mm-N-s-tonne), implicit static",
        "*TITLE",
        "matobserver single element uniaxial",
        "*CONTROL_TERMINATION",
        kfile.comment("endtim"),
        c([t_end]),
        "*CONTROL_IMPLICIT_GENERAL",
        kfile.comment("imflag", "dt0"),
        c([1, dt]),
        "*DATABASE_ELOUT",
        kfile.comment("dt", "binary"),
        c([dt, 2]),
        "*DATABASE_NODOUT",
        c([dt, 2]),
        "*DATABASE_HISTORY_SOLID",
        c([1]),
        "*DATABASE_HISTORY_NODE",
        c([PULL_NODE]),
        "*INCLUDE",
        MAT_FILE,
        "*NODE",
    ]
    for nid, x, y, z in _NODES:
        lines.append(f"{nid:>8d}{float(x):>16.9e}{float(y):>16.9e}{float(z):>16.9e}{0:>8d}{0:>8d}")
    lines += ["*ELEMENT_SOLID", f"{1:>8d}{PID:>8d}", "".join(f"{n:>8d}" for n in range(1, 9))]
    for sid, nodes, title in _SETS:
        lines += ["*SET_NODE_LIST_TITLE", title, c([sid]), c(list(nodes))]
    lines += ["*BOUNDARY_SPC_SET", kfile.comment("nsid", "cid", "dofx", "dofy", "dofz"),
              c([1, 0, 1, 0, 0]), c([2, 0, 0, 1, 0]), c([3, 0, 0, 0, 1])]
    lines += ["*BOUNDARY_PRESCRIBED_MOTION_SET", kfile.comment("nsid", "dof", "vad", "lcid", "sf"),
              c([4, 1, 2, LCID_PULL, 1.0])]
    lines += ["*DEFINE_CURVE", c([LCID_PULL]), f"{0.0:>20.12E}{0.0:>20.12E}", f"{t_end:>20.12E}{u_end:>20.12E}", "*END"]
    main = workdir / MAIN_FILE
    main.write_text("\n".join(lines) + "\n", encoding="ascii")
    return main


def read_curve(src: ResultSource, material: NeutralMaterial, stress_var: str = "sig_xx") -> CurveData:
    sig_mpa = series(src, "elout/solid", stress_var, 1)[:, 0]
    ids = np.asarray(src.read("nodout", "ids"), dtype=int).reshape(-1)
    col = int(np.flatnonzero(ids == PULL_NODE)[0])
    u_mm = series(src, "nodout", "x_displacement", len(ids))[:, col]
    n = min(len(sig_mpa), len(u_mm))
    sig = units.mpa_to_pa(sig_mpa[:n])
    eps_p = np.log1p(u_mm[:n] / 1.0) - sig / float(material.elastic.youngs_modulus)
    eps_p = np.maximum(eps_p, 0.0)
    keep = u_mm[:n] > 0
    return CurveData(x=eps_p[keep], y=sig[keep], x_quantity="eff_plastic_strain", y_quantity="true_stress")
