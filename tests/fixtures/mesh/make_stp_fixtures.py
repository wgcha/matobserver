"""STP 테스트 픽스처 생성기 (gmsh OCC). 좌표 mm 로 만들고 STEP 단위 MM 으로 쓴다.

    python tests/fixtures/mesh/make_stp_fixtures.py

| 파일 | 형상 | 용도 |
| --- | --- | --- |
| block.stp | 40×10×4 mm 육면체, 1 바디 | M-4 단일 바디 |
| fillet_block.stp | 40×10×4 mm, x 방향 4 모서리 R1 필렛 | M-4 곡면 체적 |
| two_body.stp | 40×10×2 mm 두 장 z 적층(계면 공유) | M-4 바디→PID |
| thin.stp | 20×10×0.005 mm 판 | M-5 얇은 부위 → STP_CHECK_FAILED |
| corrupt.stp | STEP 머리만 있고 잘린 파일 | M-5 손상 → STP_UNREADABLE |

생성 파일은 저장소에 커밋해 둔다(STEP 머리의 시각 문자열 때문에 재생성 시 바이트가 달라질 수 있음).
"""

from __future__ import annotations

import math
from pathlib import Path

import gmsh

HERE = Path(__file__).resolve().parent


def _write(name: str, build) -> None:
    gmsh.initialize([], readConfigFiles=False, interruptible=False)
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.add(name)
    build(gmsh.model.occ)
    gmsh.model.occ.synchronize()
    gmsh.write(str(HERE / name))
    gmsh.finalize()


def block(occ) -> None:
    occ.addBox(-20, -5, 0, 40, 10, 4)


def fillet_block(occ) -> None:
    v = occ.addBox(-20, -5, 0, 40, 10, 4)
    occ.synchronize()
    edges = []
    for _d, c in gmsh.model.getBoundary([(3, v)], combined=False, oriented=False, recursive=True):
        pass
    for _d, c in gmsh.model.getEntities(1):
        x0, y0, z0, x1, y1, z1 = occ.getBoundingBox(1, c)
        if abs((x1 - x0) - 40) < 1e-6:  # x 방향 모서리
            edges.append(c)
    occ.fillet([v], edges, [1.0], removeVolume=True)


def two_body(occ) -> None:
    occ.addBox(-20, -5, 0, 40, 10, 2)
    occ.addBox(-20, -5, 2, 40, 10, 2)


def thin(occ) -> None:
    occ.addBox(-10, -5, 0, 20, 10, 0.005)


def main() -> None:
    for name, fn in (("block.stp", block), ("fillet_block.stp", fillet_block), ("two_body.stp", two_body),
                     ("thin.stp", thin)):
        _write(name, fn)
    text = (HERE / "block.stp").read_text()
    (HERE / "corrupt.stp").write_text(text[: len(text) // 3] + "\n#999=GARBAGE((((;\n")
    print("ok", math.pi)


if __name__ == "__main__":
    main()
