"""Backend 테스트 픽스처 생성기 (소유: Impl-Backend). `python tests/fixtures/backend/make_fixtures.py` 로 JSON 을 다시 쓴다.

MatNexus 응답은 /root/matnexus/backend/openapi.json 의 TestRunDetailOut·CurvePointsOut·SpecimenSizesOut 형식을 따른
기록 대역이다. 시험 곡선은 3점 굽힘 탄소성 거동의 합성 곡선(SI: N, m, s)이다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "shared"

RUN_ID = "6f1c2a3b-0000-4000-8000-000000000001"
RUN_ID_2 = "6f1c2a3b-0000-4000-8000-000000000002"
RUN_ID_BAD = "6f1c2a3b-0000-4000-8000-0000000000ff"
SPECIMEN_ID = "5a5a5a5a-0000-4000-8000-000000000001"
MATERIAL_ID = "3c3c3c3c-0000-4000-8000-000000000001"


def curve(n: int = 80, stiffness: float = 6.8e5, delta_max: float = 3.0e-3) -> tuple[list[float], list[float], list[float]]:
    """(time, displacement, force). 짧은 토우 구간 + 선형 탄성 + 완만한 경화."""
    dy = 4.4e-4
    fy = stiffness * dy
    t, d, f = [], [], []
    for i in range(n):
        x = delta_max * i / (n - 1)
        if x < dy:
            y = stiffness * x
        else:
            y = fy + 0.35 * fy * math.sqrt((x - dy) / dy) * 0.5
        t.append(round(20.0 * i / (n - 1), 9))
        d.append(round(x, 12))
        f.append(round(y, 9))
    return t, d, f


def write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main() -> None:
    t, d, f = curve()
    write(HERE / "matnexus" / "run_flexural.json", {
        "id": RUN_ID, "code": "TR-0001", "status": "parsed", "parse_error": None, "test_type_key": "flexural",
        "channels": ["time", "displacement", "force"], "conditions": {"temperature": 296.15, "test_speed": 1.5e-4},
        "instrument": "UTM-01", "specimen_id": SPECIMEN_ID, "material_id": MATERIAL_ID, "material_name": "DP600",
        "source_sha256": "ab" * 32, "curves": [], "created_at": "2026-11-01T00:00:00Z",
    })
    write(HERE / "matnexus" / "run_unparsed.json", {
        "id": RUN_ID_BAD, "code": "TR-0099", "status": "failed", "parse_error": "unexpected header", "test_type_key": "flexural",
        "channels": [], "conditions": {}, "instrument": None, "specimen_id": None, "material_id": None, "source_sha256": None,
    })
    write(HERE / "matnexus" / "curve_displacement_force.json", {
        "x": "displacement", "y": "force", "row_count": len(d), "returned": len(d), "points": [[a, b] for a, b in zip(d, f)]})
    write(HERE / "matnexus" / "curve_time_force.json", {
        "x": "time", "y": "force", "row_count": len(t), "returned": len(t), "points": [[a, b] for a, b in zip(t, f)]})
    sizes = [
        {"key": "width", "dimension": "length", "label": "폭", "measured": 0.0101, "nominal": 0.010, "inherited": False, "is_required": True},
        {"key": "thickness", "dimension": "length", "label": "두께", "measured": None, "nominal": 0.004, "inherited": False, "is_required": True},
        {"key": "span", "dimension": "length", "label": "지점 간 거리", "measured": None, "nominal": 0.064, "inherited": False, "is_required": True},
        {"key": "total_length", "dimension": "length", "label": "전장", "measured": 0.080, "nominal": 0.080, "inherited": False, "is_required": False},
        {"key": "load_span", "dimension": "length", "label": "하중 지점 간 거리", "measured": None, "nominal": None, "inherited": False, "is_required": False},
    ]
    write(HERE / "matnexus" / "dimensions_flexural.json", {
        "standard": "ISO 178", "cross_section": "rect", "cross_section_label": "직사각형", "area": 4.0e-5, "area_problem": None,
        "fields": sizes, "warnings": []})
    write(HERE / "matnexus" / "dimensions_no_span.json", {
        "standard": "ISO 178", "cross_section": "rect", "cross_section_label": "직사각형", "area": 4.0e-5, "area_problem": None,
        "fields": [s for s in sizes if s["key"] != "span"], "warnings": []})
    write(SHARED / "flexural_inline_run.json", {
        "run_key": "spec01", "test_type_key": "flexural",
        "channels": {"time": {"unit": "s", "values": t}, "displacement": {"unit": "m", "values": d}, "force": {"unit": "N", "values": f}},
        "specimen_dimensions": {"width": 0.010, "thickness": 0.004, "span": 0.064, "total_length": 0.080},
        "conditions": {"temperature": 296.15, "test_speed": 1.5e-4}, "instrument": "UTM-01", "specimen_ref": "S-01"})


if __name__ == "__main__":
    main()
