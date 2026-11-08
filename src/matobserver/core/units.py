"""SI ↔ 파일 단위계(mm·N·s·tonne) 변환표 (module-boundaries.md §3).

Python 객체는 SI 고정, LS-DYNA .k 파일은 mm·N·s·tonne 고정이며 변환은 파일 writer·reader 안에서만 한다.
설정으로 바꾸지 않는다. 계수는 "SI 값 × factor = 파일 값".

| 물리량 | SI → 파일 | factor |
| length | m → mm | 1e3 |
| stress | Pa → MPa | 1e-6 |
| density | kg/m^3 → tonne/mm^3 | 1e-12 |
| velocity | m/s → mm/s | 1e3 |
| force | N → N | 1 |
| time | s → s | 1 |
| compliance | m/N → mm/N | 1e3 |
"""

from __future__ import annotations

from typing import Final, TypeVar

from .errors import UnitError

UNIT_SYSTEM: Final = "mm-N-s-tonne"  # SolverCapabilities.unit_system, 매니페스트 unit_system

#: 물리량 → SI→파일 배율 (module-boundaries.md §3 변환표)
FACTORS: Final[dict[str, float]] = {
    "length": 1e3,
    "stress": 1e-6,
    "density": 1e-12,
    "velocity": 1e3,
    "force": 1.0,
    "time": 1.0,
    "compliance": 1e3,
}

#: 물리량 → (SI 단위, 파일 단위)
UNITS: Final[dict[str, tuple[str, str]]] = {
    "length": ("m", "mm"),
    "stress": ("Pa", "MPa"),
    "density": ("kg/m^3", "tonne/mm^3"),
    "velocity": ("m/s", "mm/s"),
    "force": ("N", "N"),
    "time": ("s", "s"),
    "compliance": ("m/N", "mm/N"),
}

T = TypeVar("T")  # float 또는 numpy 배열


def factor(quantity: str) -> float:
    try:
        return FACTORS[quantity]
    except KeyError:
        raise UnitError(f"unknown quantity {quantity!r}; known: {sorted(FACTORS)}") from None


def to_file(quantity: str, value_si: T) -> T:
    """SI 값 → 파일(mm·N·s·tonne) 값. float 또는 numpy 배열."""
    return value_si * factor(quantity)  # type: ignore[operator]


def to_si(quantity: str, value_file: T) -> T:
    """파일 값 → SI 값. 1.0 배율은 값을 그대로 돌려주고, 그 외는 나눗셈으로 환산해 왕복 오차를 줄인다."""
    f = factor(quantity)
    if f == 1.0:
        return value_file
    return value_file / f  # type: ignore[operator]


# 자주 쓰는 이름 (파일 writer·reader 가독성용). 모두 위 변환표를 그대로 쓴다.
def m_to_mm(v: T) -> T:
    return to_file("length", v)


def mm_to_m(v: T) -> T:
    return to_si("length", v)


def pa_to_mpa(v: T) -> T:
    return to_file("stress", v)


def mpa_to_pa(v: T) -> T:
    return to_si("stress", v)


def kg_m3_to_tonne_mm3(v: T) -> T:
    return to_file("density", v)


def tonne_mm3_to_kg_m3(v: T) -> T:
    return to_si("density", v)
