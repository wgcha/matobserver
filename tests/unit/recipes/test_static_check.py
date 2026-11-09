"""B-7(a): 레시피 정적 검사 코드 (recipe.md §7.1) — 코드마다 최소 1개 변형 + 정상 레시피는 이슈 없음."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from matobserver.core.registry import Registry
from matobserver.recipes.static_check import check_folder

from be_support import PLUGINS_DIR, RECIPE_DIR, FIX

REPO = FIX.parents[2]


@pytest.fixture(scope="module")
def registry():
    return Registry.from_paths([PLUGINS_DIR])


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "recipe"
    shutil.copytree(RECIPE_DIR, d)
    return d


def edit(folder: Path, rel: str, fn):
    p = folder / rel
    p.write_text(fn(p.read_text(encoding="utf-8")), encoding="utf-8")


def sub(old, new, count=1):
    def f(t):
        assert old in t, f"{old!r} 없음"
        return t.replace(old, new, count)

    return f


def append(text):
    return lambda t: t + "\n" + text + "\n"


def codes(res, severity="error"):
    return [i.code for i in res.issues if i.severity == severity]


# ---- 변형: 코드 -> (폴더에 적용하는 함수) ----
def m_bundle_invalid(d):
    (d / "manifest.yaml").unlink()


def m_manifest_invalid(d):
    edit(d, "manifest.yaml", sub("title: ", "titel: "))


def m_manifest_bad_type(d):
    edit(d, "manifest.yaml", sub("version: 1\n", "version: one\n"))


def m_test_type_unknown(d):
    edit(d, "manifest.yaml", sub("test_type: bending", "test_type: tension"))


def m_unit_system(d):
    edit(d, "manifest.yaml", sub("unit_system: mm-N-s-tonne", "unit_system: SI"))


def m_solver_unknown(d):
    edit(d, "manifest.yaml", sub("name: lsdyna", "name: nosolver"))


def m_generator_unknown(d):
    edit(d, "manifest.yaml", sub("generator: jig.bending3p_rollers", "generator: jig.nothing"))


def m_path_unsafe(d):
    try:
        os.symlink("/etc/hostname", d / "deck" / "link.k")
    except OSError:
        pytest.skip("symlink 권한 없음 (Windows)")


def m_path_unsafe_manifest(d):
    edit(d, "manifest.yaml", sub("main: deck/main.k", "main: ../outside/main.k"))


def m_file_missing(d):
    (d / "deck" / "contact.k").unlink()


def m_include_unresolved(d):
    edit(d, "deck/main.k", sub("*INCLUDE\ncontact.k", "*INCLUDE\nnot_there.k"))


def m_reserved_include_missing(d):
    edit(d, "deck/main.k", sub("*INCLUDE\nmo_jig.k\n", ""))


def m_params_not_first(d):
    edit(d, "deck/main.k", lambda t: t.replace("*INCLUDE\nmo_params.k\n", "", 1).replace("*INCLUDE\nmo_specimen.k", "*INCLUDE\nmo_specimen.k\n*INCLUDE\nmo_params.k", 1))


def m_reserved_file_present(d):
    (d / "deck" / "mo_extra.k").write_text("*KEYWORD\n*END\n", encoding="utf-8")


def m_reserved_param_redefined(d):
    edit(d, "deck/contact.k", append("*PARAMETER\nR span, 0.5\n"))


def m_reserved_param_redefined_engine(d):
    edit(d, "deck/contact.k", append("*PARAMETER\nR mo_tend, 1.0\n"))


def m_param_undeclared(d):
    edit(d, "deck/contact.k", sub("&fric     &fric", "&zzz      &fric"))


def m_param_name_invalid(d):
    edit(d, "manifest.yaml", sub("name: r_ind,", "name: Bad-Name,"))


def m_param_default_outside_range(d):
    edit(d, "manifest.yaml", sub("default: 0.064, range: [0.010, 0.300]", "default: 0.5, range: [0.010, 0.300]"))


def m_param_range_reversed(d):
    edit(d, "manifest.yaml", sub("range: [0.010, 0.300]", "range: [0.300, 0.010]"))


def m_output_keyword(d):
    edit(d, "deck/contact.k", append("*DATABASE_RCFORC\n  1.0e-3\n*END"))


def m_slot_id_conflict(d):
    edit(d, "deck/contact.k", append("*MAT_ELASTIC\n       101  7.8e-9\n"))


def m_section_invalid(d):
    edit(d, "manifest.yaml", sub("elform: -2", "elform: 99"))


def m_id_range_conflict(d):
    edit(d, "deck/contact.k", append("*NODE\n      10       0.0       0.0       0.0\n"))


def m_response_id_missing(d):
    edit(d, "manifest.yaml", sub("contact_id: 1", "contact_id: 77"))


def m_symmetry_invalid(d):
    edit(d, "manifest.yaml", sub("force_scale: 4.0", "force_scale: 2.0"))


def m_compliance_invalid(d):
    edit(d, "manifest.yaml", sub("value_m_per_n: 1.8e-7", "value_m_per_n: -1.0"))


def m_reference_card_invalid(d):
    edit(d, "verification/reference_card.yaml", sub("youngs_modulus: 2.0e9", "youngs_modulus: -2.0e9"))


def m_reference_card_slot_missing(d):
    (d / "verification" / "reference_card.yaml").write_text("other:\n  elastic: {youngs_modulus: 2.0e9, poisson_ratio: 0.38, density: 1200.0}\n",
                                                           encoding="utf-8")


def m_kind_mismatch(d):
    edit(d, "manifest.yaml", sub("kind: parametric", "kind: fixed"))


def m_implicit_dtmax(d):
    edit(d, "deck/controls_implicit.k", sub("0.0 &mo_dtmax", "0.0 1.0e-3"))


MUTATIONS = {
    "RECIPE_BUNDLE_INVALID": [m_bundle_invalid],
    "RECIPE_MANIFEST_INVALID": [m_manifest_invalid, m_manifest_bad_type],
    "RECIPE_TEST_TYPE_UNKNOWN": [m_test_type_unknown],
    "RECIPE_UNIT_SYSTEM_UNSUPPORTED": [m_unit_system],
    "RECIPE_SOLVER_UNKNOWN": [m_solver_unknown],
    "RECIPE_GENERATOR_UNKNOWN": [m_generator_unknown],
    "RECIPE_PATH_UNSAFE": [m_path_unsafe, m_path_unsafe_manifest],
    "RECIPE_FILE_MISSING": [m_file_missing],
    "RECIPE_INCLUDE_UNRESOLVED": [m_include_unresolved],
    "RECIPE_RESERVED_INCLUDE_MISSING": [m_reserved_include_missing, m_params_not_first],
    "RECIPE_RESERVED_FILE_PRESENT": [m_reserved_file_present],
    "RECIPE_RESERVED_PARAM_REDEFINED": [m_reserved_param_redefined, m_reserved_param_redefined_engine],
    "RECIPE_PARAM_UNDECLARED": [m_param_undeclared],
    "RECIPE_PARAM_NAME_INVALID": [m_param_name_invalid, m_param_default_outside_range, m_param_range_reversed],
    "RECIPE_OUTPUT_KEYWORD_PRESENT": [m_output_keyword],
    "RECIPE_SLOT_ID_CONFLICT": [m_slot_id_conflict],
    "RECIPE_SECTION_INVALID": [m_section_invalid],
    "RECIPE_ID_RANGE_CONFLICT": [m_id_range_conflict],
    "RECIPE_RESPONSE_ID_MISSING": [m_response_id_missing],
    "RECIPE_SYMMETRY_INVALID": [m_symmetry_invalid],
    "RECIPE_COMPLIANCE_INVALID": [m_compliance_invalid],
    "RECIPE_REFERENCE_CARD_INVALID": [m_reference_card_invalid, m_reference_card_slot_missing],
    "RECIPE_KIND_MISMATCH": [m_kind_mismatch],
    "RECIPE_IMPLICIT_DTMAX_MISSING": [m_implicit_dtmax],
}
CASES = [(code, fn) for code, fns in MUTATIONS.items() for fn in fns]


def test_reference_recipe_has_no_issues(folder, registry):
    res = check_folder(folder, registry)
    assert res.issues == [] and res.manifest["recipe_id"] == "bending3p-param"
    assert "manifest.yaml" in [e.path for e in res.entries]


@pytest.mark.parametrize("code,mutate", CASES, ids=[f"{c}:{f.__name__}" for c, f in CASES])
def test_static_check_code(code, mutate, folder, registry):
    mutate(folder)
    res = check_folder(folder, registry)
    assert code in codes(res), f"{code} 기대, 실제 {[(i.code, i.message) for i in res.issues]}"
    for i in res.errors:
        assert i.message


def test_every_code_in_recipe_md_is_covered():
    text = (REPO / "docs" / "contracts" / "recipe.md").read_text(encoding="utf-8")
    sec = text.split("### 7.1", 1)[1].split("### 7.2", 1)[0]
    documented = set(re.findall(r"`(RECIPE_[A-Z_]+)`", sec))
    assert documented <= set(MUTATIONS), f"시험이 없는 코드: {sorted(documented - set(MUTATIONS))}"


def test_output_interval_hardcoded_is_a_warning_not_an_error(folder, registry):
    edit(folder, "deck/contact.k", append("*DATABASE_SECFORC\n  1.0e-3\n*END"))
    res = check_folder(folder, registry)
    assert res.errors == [] and "RECIPE_OUTPUT_INTERVAL_HARDCODED" in codes(res, "warning")


def test_dtout_reference_is_not_hardcoded(folder, registry):
    edit(folder, "deck/contact.k", append("*DATABASE_SECFORC\n  &mo_dtout\n*END"))
    assert check_folder(folder, registry).issues == []


def test_issues_are_collected_not_first_only(folder, registry):
    m_symmetry_invalid(folder)
    m_compliance_invalid(folder)
    m_output_keyword(folder)
    got = codes(check_folder(folder, registry))
    assert {"RECIPE_SYMMETRY_INVALID", "RECIPE_COMPLIANCE_INVALID", "RECIPE_OUTPUT_KEYWORD_PRESENT"} <= set(got)


def test_check_without_registry_skips_registry_codes(folder):
    m_solver_unknown(folder)
    assert "RECIPE_SOLVER_UNKNOWN" not in codes(check_folder(folder, None))


def test_fixed_recipe_requires_no_jig_and_forbids_generator(folder, registry):
    edit(folder, "manifest.yaml", sub("kind: parametric", "kind: fixed"))
    assert "RECIPE_KIND_MISMATCH" in codes(check_folder(folder, registry))
