"""오류·경고 코드 집합이 api.md §7, result.md §4.10, recipe.md §7.1·§7.2 정의와 일치한다."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from matobserver.core import errors as E

CONTRACTS = Path(__file__).resolve().parents[3] / "docs" / "contracts"
pytestmark = pytest.mark.skipif(not CONTRACTS.exists(), reason="docs/contracts not available")

CODE_RE = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z]{3,})`")


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


def _table_codes(section: str, column: int) -> set[str]:
    """마크다운 표의 `column`번째(0부터) 셀에서 백틱 코드를 모은다. 빈 첫 셀 행(분류 열 생략)도 처리."""
    out: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("|") or set(line.replace("|", "").strip()) <= {"-", " "}:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) > column:
            out.update(CODE_RE.findall(cells[column]))
    return out


@pytest.fixture(scope="module")
def api_text() -> str:
    return (CONTRACTS / "api.md").read_text(encoding="utf-8")


# ---- (c) api.md §7 ----
def test_http_top_level_codes_match_api_7_2(api_text):
    sec = _section(api_text, "### 7.2 HTTP 상태와 최상위 코드", "### 7.3")
    assert _table_codes(sec, 1) == set(E.HTTP_ERROR_CODES)
    # HTTP 상태도 표와 같다
    for line in sec.splitlines():
        m = re.match(r"^\| (\d{3}) \| ([^|]+) \|", line)
        if m:
            for code in CODE_RE.findall(m.group(2)):
                assert E.HTTP_STATUS[code] == int(m.group(1)), code


def test_job_error_codes_match_api_7_3(api_text):
    sec = _section(api_text, "### 7.3 잡 실행 오류", "### 7.4")
    assert _table_codes(sec, 0) == set(E.JOB_ERROR_CODES)


def test_validation_codes_match_api_7_4(api_text):
    sec = _section(api_text, "### 7.4 400 검증 사유 코드", "## 8. 레시피 API")
    # 표: | 분류 | 코드 | 조건 |  (분류가 비어 있는 행은 첫 셀이 빈 문자열)
    doc_codes = _table_codes(sec, 1)
    assert doc_codes == set(E.VALIDATION_CODES)


def test_upload_error_code_defined_in_api_4_1(api_text):
    assert "`FILE_TYPE_UNSUPPORTED`" in api_text
    assert E.UPLOAD_ERROR_CODES == {"FILE_TYPE_UNSUPPORTED"}


# ---- result.md §4.10 ----
def test_warning_codes_match_result_4_10():
    text = (CONTRACTS / "result.md").read_text(encoding="utf-8")
    sec = _section(text, "### 4.10 `Warning`", "### 4.11")
    # 1차 경고 코드 문장에서 `{code, ...}` 설명 이후의 코드만 모은다
    sentence = sec.split("1차 경고 코드:", 1)[1]
    assert set(CODE_RE.findall(sentence)) == set(E.WARNING_CODES)


# ---- recipe.md §7.1 / §7.2 ----
def test_recipe_static_codes_match_recipe_7_1():
    text = (CONTRACTS / "recipe.md").read_text(encoding="utf-8")
    sec = _section(text, "### 7.1 정적 검사", "### 7.2 dry-run")
    doc = set()
    for line in sec.splitlines():
        if line.startswith("| `RECIPE_"):
            doc.update(CODE_RE.findall(line.split("|")[1]))
    assert doc == set(E.RECIPE_STATIC_CODES)


def test_verify_codes_match_recipe_7_2():
    text = (CONTRACTS / "recipe.md").read_text(encoding="utf-8")
    sec = _section(text, "### 7.2 dry-run 검증", "### 7.3")
    doc = _table_codes(sec, 0)
    # 표의 코드 열 + 표 본문에 쓰인 보조 코드 VERIFY_FALLBACK_USED
    assert doc == set(E.VERIFY_CODES) - {"VERIFY_FALLBACK_USED"}
    assert "VERIFY_FALLBACK_USED" in sec


# ---- 상수 자체의 일관성 ----
def test_constant_values_equal_their_names():
    consts = {n: v for n, v in vars(E).items() if n.isupper() and isinstance(v, str)}
    assert consts
    for name, value in consts.items():
        assert name == value, name


def test_code_groups_and_helpers():
    assert not (E.WARNING_CODES & E.JOB_ERROR_CODES)
    assert E.ALL_ERROR_CODES >= E.VALIDATION_CODES | E.JOB_ERROR_CODES | E.HTTP_ERROR_CODES
    assert E.VERIFY_FAIL_CODES.isdisjoint(E.VERIFY_WARN_CODES)
    # 레지스트리 type -> 400 사유 코드는 모두 정의된 코드
    for code in E.UNKNOWN_PLUGIN_CODE.values():
        assert code is None or code in E.VALIDATION_CODES
    assert E.UNKNOWN_PLUGIN_CODE["optimizer"] == "OPTIMIZER_UNKNOWN"


def test_engine_failure_requires_job_error_code():
    with pytest.raises(ValueError):
        E.EngineFailure("NOT_A_CODE")
    f = E.EngineFailure(E.MESH_FAILED, "bad mesh")
    assert f.code == "MESH_FAILED" and "bad mesh" in str(f)
