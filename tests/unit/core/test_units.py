"""(d) 단위 변환: 변환표 값과 SI ↔ 파일 왕복."""

from __future__ import annotations

import numpy as np
import pytest

from matobserver.core import units
from matobserver.core.errors import UnitError

# module-boundaries.md §3: length 1e3, stress 1e-6, density 1e-12, velocity 1e3, force 1, time 1, compliance 1e3
TABLE = {"length": 1e3, "stress": 1e-6, "density": 1e-12, "velocity": 1e3, "force": 1.0, "time": 1.0,
         "compliance": 1e3}


def test_factor_table_matches_contract():
    assert units.FACTORS == TABLE
    assert units.UNIT_SYSTEM == "mm-N-s-tonne"


@pytest.mark.parametrize("quantity", sorted(TABLE))
@pytest.mark.parametrize("value", [0.0, 1.0, 2.31e9, 7850.0, 1e-12, 123.456, -3.5])
def test_round_trip_scalar(quantity, value):
    back = units.to_si(quantity, units.to_file(quantity, value))
    assert back == pytest.approx(value, rel=1e-14, abs=0.0)


@pytest.mark.parametrize("quantity", sorted(TABLE))
def test_round_trip_array(quantity):
    rng = np.random.default_rng(0)
    a = rng.uniform(1e-9, 1e12, size=200)
    back = units.to_si(quantity, units.to_file(quantity, a))
    np.testing.assert_allclose(back, a, rtol=1e-14, atol=0.0)
    assert isinstance(units.to_file(quantity, a), np.ndarray)


def test_known_physical_values():
    assert units.m_to_mm(0.0125) == pytest.approx(12.5)
    assert units.mm_to_m(12.5) == pytest.approx(0.0125)
    assert units.pa_to_mpa(2.1e11) == pytest.approx(2.1e5)          # 2.1e11 Pa = 210 GPa = 2.1e5 MPa
    assert units.mpa_to_pa(2.1e5) == pytest.approx(2.1e11)
    assert units.kg_m3_to_tonne_mm3(7850.0) == pytest.approx(7.85e-9)  # 7850 kg/m^3 = 7.85e-9 tonne/mm^3
    assert units.tonne_mm3_to_kg_m3(7.85e-9) == pytest.approx(7850.0)
    assert units.to_file("velocity", 0.5) == pytest.approx(500.0)      # 0.5 m/s = 500 mm/s
    assert units.to_file("compliance", 1e-8) == pytest.approx(1e-5)   # m/N -> mm/N
    assert units.to_file("force", 123.0) == 123.0
    assert units.to_file("time", 1.0) == 1.0


def test_unit_labels_cover_all_quantities():
    assert set(units.UNITS) == set(units.FACTORS)
    assert units.UNITS["stress"] == ("Pa", "MPa")


def test_unknown_quantity_raises():
    with pytest.raises(UnitError):
        units.to_file("temperature", 1.0)
    with pytest.raises(ValueError):  # UnitError 는 ValueError 하위
        units.factor("mass")
