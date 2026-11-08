"""덱 스캐너·카드 블록·매니페스트 스키마 단위 시험 (정적 검사의 구성 요소)."""

from __future__ import annotations

import pytest

from matobserver.recipes import kdeck
from matobserver.recipes.cards import CardBlocksError, neutral_from_card_blocks
from matobserver.recipes.manifest import parse_manifest_text, validate_manifest

from be_support import RECIPE_DIR


# ---------------------------------------------------------------- kdeck
def test_parse_cards_skips_comments_and_blank_lines():
    cards = kdeck.parse_cards("*KEYWORD\n$ 주석\n\n*INCLUDE\nmo_params.k\n*TITLE\n제목\n*END\n")
    assert [c.keyword for c in cards] == ["KEYWORD", "INCLUDE", "TITLE", "END"]
    assert kdeck.include_target(cards[1]) == "mo_params.k" and cards[1].data == [(5, "mo_params.k")]


def test_include_path_variants_are_not_includes():
    (c,) = kdeck.parse_cards("*INCLUDE_PATH\n/some/dir\n")
    assert kdeck.include_target(c) is None
    (q,) = kdeck.parse_cards("*INCLUDE\n'quoted file.k'\n")
    assert kdeck.include_target(q) == "quoted file.k"


def test_parameter_defs_and_refs():
    cards = kdeck.parse_cards("*PARAMETER\nR fric, 0.1\nI nn 5\n*CONTACT\n 1 &fric &mo_dtmax\n")
    assert [n for _, n in kdeck.parameter_defs(cards[0])] == ["fric", "nn"]
    assert [n for _, n in kdeck.param_refs(cards[1])] == ["fric", "mo_dtmax"]


@pytest.mark.parametrize("line,expected", [
    ("         1indenter", 1), ("       900", 900), ("  12, name", 12), ("abc", None), ("", None), ("-5", -5), ("     1.5", None),
])
def test_first_int_reads_fixed_and_free_format(line, expected):
    assert kdeck.first_int(line) == expected


def test_card_id_skips_title_line_for_title_cards():
    (c,) = kdeck.parse_cards("*MAT_ELASTIC_TITLE\nmy mat\n       101   7.8e-9\n")
    assert kdeck.card_id(c) == 101
    (d,) = kdeck.parse_cards("*DEFINE_CURVE\n       910         0\n")
    assert kdeck.card_id(d) == 910


def test_part_and_element_ids():
    (p,) = kdeck.parse_cards("*PART\nname\n   1   101   101\n")
    assert kdeck.part_ids(p) == (1, 101, 101)
    (e,) = kdeck.parse_cards("*ELEMENT_SOLID\n 1 1\n 1 2 3 4 5 6 7 8\n 2 1\n 2 3 4 5 6 7 8 9\n")
    assert list(kdeck.element_ids(e)) == [1, 2]


# ---------------------------------------------------------------- cards
def test_card_blocks_to_neutral_material_and_back():
    blocks = {"elastic": {"youngs_modulus": 7e10, "poisson_ratio": 0.33, "density": 2700.0},
              "table": [{"plastic_strain": 0.0, "true_stress": 3e8}, {"plastic_strain": 0.1, "true_stress": 4e8}], "rate_table": None}
    nm = neutral_from_card_blocks(blocks)
    assert nm.elastic.youngs_modulus == 7e10 and nm.to_card_blocks()["elastic"]["poisson_ratio"] == 0.33
    assert len(nm.to_card_blocks()["table"]) == 2


@pytest.mark.parametrize("blocks", [
    {"elastic": {"poisson_ratio": 0.3}},
    {"elastic": {"youngs_modulus": "x", "poisson_ratio": 0.3, "density": 1.0}},
    {"elastic": {"youngs_modulus": 1e9, "poisson_ratio": 0.3, "density": 1.0}, "table": [{"plastic_strain": 0.0}]},
    "nope",
])
def test_bad_card_blocks(blocks):
    with pytest.raises((CardBlocksError, KeyError, TypeError)):
        neutral_from_card_blocks(blocks)


# ---------------------------------------------------------------- manifest
def manifest_text():
    return (RECIPE_DIR / "manifest.yaml").read_text(encoding="utf-8")


def test_reference_manifest_validates():
    m, errs = parse_manifest_text(manifest_text())
    assert errs == [] and m["recipe_id"] == "bending3p-param" and m["version"] == 1


@pytest.mark.parametrize("old,new,field", [
    ("version: 1\n", "version: 0\n", "version"),
    ("version: 1\n", "version: 1.5\n", "version"),
    ("kind: parametric", "kind: weird", "kind"),
    ("default: 0.064,", "default: nan,", "default"),
    ("default: 0.064,", "default: true,", "default"),
])
def test_manifest_rejects_bad_values(old, new, field):
    m, errs = parse_manifest_text(manifest_text().replace(old, new, 1))
    assert errs and any(field in e["field"] or field in e["message"] for e in errs), errs


def test_manifest_yaml_errors_and_non_mapping():
    assert parse_manifest_text("a: [unclosed")[1]
    assert parse_manifest_text("- 1\n- 2\n")[1]
    assert parse_manifest_text("")[1]


def test_manifest_allows_unknown_extra_keys_for_forward_compat():
    m, errs = parse_manifest_text(manifest_text() + "\nfuture_key: {a: 1}\n")
    assert errs == [] and m["future_key"] == {"a": 1}
    assert validate_manifest(m) == []


@pytest.mark.parametrize("old,new", [
    ("recipe_id: bending3p-param", "recipe_id: BAD_ID"),
    ("recipe_id: bending3p-param", "recipe_id: ab"),
    ("recipe_id: bending3p-param", "recipe_id: ../../evil"),
    ("recipe_id: bending3p-param", "recipe_id: \"a/b/c\""),
    ("schema_version: \"1.0\"", "schema_version: \"2.0\""),
])
def test_value_rules_are_enforced_by_registration(env, old, new):
    """값 규칙(패턴·MAJOR)은 등록 단계에서 400 RECIPE_MANIFEST_INVALID 로 막힌다 - 저장 경로에 쓰이기 전에."""
    from be_support import error_codes, folder_zip

    def mutate(files):
        files["manifest.yaml"] = files["manifest.yaml"].decode().replace(old, new, 1).encode()

    r = env.add_recipe(folder_zip(RECIPE_DIR, mutate))
    assert r.status_code == 400 and "RECIPE_MANIFEST_INVALID" in error_codes(r)
    store = env.config.path("recipes.store_dir")
    assert not [p for p in store.rglob("*") if p.is_file()]
    assert not (store.parent / "evil").exists() and not (store.parent.parent / "evil").exists()
