"""d3hsp/messag 판독: 종료 판정, 버전, 라이선스 오류, 수렴 실패, 초기 관통 (plugins.md §4.2, recipe.md §7.2).

문구·정규식은 설정(`solver.lsdyna.termination_patterns`, `license_error_patterns`, `version_pattern`)과
플러그인 옵션(`divergence_patterns`, `penetration_pattern`)에서만 온다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence

from ...deck.kfile import normalize_newlines

LOG_FILES = ("d3hsp", "messag", "mes0000")
RUN_LOG = "mo_run.log"  # 배치 스크립트 stdout/stderr (어댑터가 작업 폴더에 쓴다)

Verdict = Literal["normal", "error", "diverged", "license", "unknown"]


@dataclass(frozen=True)
class TerminationInfo:
    verdict: Verdict
    message: str  # 판정 근거 문구(없으면 "")
    version: str | None


def read_logs(rundir: Path, names: Sequence[str] = LOG_FILES + (RUN_LOG,)) -> dict[str, str]:
    out = {}
    for n in names:
        p = Path(rundir) / n
        if p.is_file():
            out[n] = normalize_newlines(p.read_bytes().decode("utf-8", errors="replace"))
    return out


LICENSE_TAIL_LINES = 60


def _tail(text: str, n: int = LICENSE_TAIL_LINES) -> str:
    return "\n".join(normalize_newlines(text).split("\n")[-n:])


def _search(patterns: Iterable[str], text: str) -> str | None:
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return None


def judge(logs: Mapping[str, str], *, termination_patterns: Mapping[str, Sequence[str]],
          license_patterns: Sequence[str], divergence_patterns: Sequence[str], version_pattern: str) -> TerminationInfo:
    """우선순위: Error termination(라이선스 → license, 수렴 실패 → diverged, 그 외 error) > Normal termination >
    문구 없음(라이선스 문구 → license, 그 외 unknown)."""
    logs = {k: normalize_newlines(v) for k, v in logs.items()}  # Windows LS-DYNA 로그(CRLF) 허용
    solver_text = "\n".join(logs.get(n, "") for n in LOG_FILES)
    # 라이선스 문구는 각 로그의 끝부분에서만 찾는다(d3hsp 머리말의 'Licensed to' 등 오검출 방지)
    tail_text = "\n".join(_tail(logs.get(n, "")) for n in LOG_FILES + (RUN_LOG,))
    version = None
    for n in ("d3hsp", "messag", "mes0000", RUN_LOG):
        if n in logs:
            m = re.search(version_pattern, logs[n])
            if m:
                version = (m.group(1) if m.groups() else m.group(0)).strip()
                break
    err = _search(termination_patterns.get("error", ()), solver_text)
    lic = _search(license_patterns, tail_text)
    div = _search(divergence_patterns, solver_text)
    if err is not None:
        if lic is not None:
            return TerminationInfo("license", lic, version)
        if div is not None:
            return TerminationInfo("diverged", f"{err} ({div})", version)
        return TerminationInfo("error", err, version)
    normal = _search(termination_patterns.get("normal", ()), solver_text)
    if normal is not None:
        return TerminationInfo("normal", normal, version)
    if lic is not None:
        return TerminationInfo("license", lic, version)
    if div is not None:
        return TerminationInfo("diverged", div, version)
    return TerminationInfo("unknown", "", version)


def initial_penetration_mm(d3hsp_text: str, pattern: str) -> float | None:
    """d3hsp 초기 관통 보고의 최대값(파일 단위 mm). 보고가 없으면 None. `pattern`의 첫 그룹이 수치."""
    vals = []
    d3hsp_text = normalize_newlines(d3hsp_text)
    for m in re.finditer(pattern, d3hsp_text, flags=re.IGNORECASE):
        try:
            vals.append(abs(float(m.group(1))))
        except (ValueError, IndexError):
            continue
    return max(vals) if vals else None
