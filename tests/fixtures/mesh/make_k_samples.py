"""§2 규격 샘플 메시(.k) 생성 — Impl-Solver S-6(tet10 샘플 덱 실제 실행)·M-8 공유용.

    python tests/fixtures/mesh/make_k_samples.py

3점굽힘 1/4 대칭: 시편 80×10×4 mm, 스팬 64 mm, 압자·지지 R 5 mm, target 1 mm, 두께 방향 4요소.
PID 1, 시편 파트 세트 900, 대칭 세트 sym_x0=901·sym_y0=902, 참조점 세트 4900001(midspan_bottom).
지그: 압자 PID/MID 1001, 지지 PID/MID 1002, 강재 E 2.1e11 Pa·ν 0.3·ρ 7850 kg/m³.

| 파일 | 내용 |
| --- | --- |
| specimen_hex8.k / specimen_tet4.k / specimen_tet10.k | 시편 (mo_specimen.k 형식) |
| jig_quarter.k | 지그 (mo_jig.k 형식) |
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from matobserver.core import types as T
from matobserver.mesh.box import BoxMesher
from matobserver.mesh.jig import Bending3pRollersJig

HERE = Path(__file__).resolve().parent
SPEC = T.GeometrySpec(kind="box", dimensions={"width": 0.010, "thickness": 0.004, "total_length": 0.080},
                      region_part_ids={"specimen": 1},
                      reference_points=(T.ReferencePointRequest("midspan_bottom", {"x": 0.0, "y": 0.0, "z": "min"}),))


def opts(topology: str) -> T.MeshOptions:
    return T.MeshOptions(topology=topology, target_size_m=0.001, min_elems_through_thickness=4, max_elements=200_000,
                         symmetry="quarter", part_set_id=900, node_set_ids={"sym_x0": 901, "sym_y0": 902})


def jig_spec(specimen: T.MeshResult) -> T.JigSpec:
    return T.JigSpec(generator="jig.bending3p_rollers",
                     params_si={"span": 0.064, "indenter_radius": 0.005, "support_radius": 0.005},
                     part_ids={"indenter": 1001, "support": 1002}, mids={"indenter": 1001, "support": 1002},
                     rigid={"E": 2.1e11, "poisson": 0.3, "density": 7850.0}, specimen=specimen, symmetry="quarter")


def generate(dest: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        hex_result = None
        for topo in ("hex8", "tet4", "tet10"):
            r = BoxMesher({}).mesh(SPEC, opts(topo), tmp / topo)
            p = dest / f"specimen_{topo}.k"
            shutil.copyfile(r.mesh_path, p)
            out[topo] = p
            if topo == "hex8":
                hex_result = r
        j = Bending3pRollersJig({}).mesh(jig_spec(hex_result), None, tmp / "jig")
        p = dest / "jig_quarter.k"
        shutil.copyfile(j.mesh_path, p)
        out["jig"] = p
    return out


if __name__ == "__main__":
    for k, v in generate(HERE).items():
        print(k, v.name, v.stat().st_size)
