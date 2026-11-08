"""LS-DYNA SolverAdapter (plugins.md §4.2, 레지스트리 이름 `lsdyna`).

옵션(`Cls(options)`, plugins.md §2 규칙 3):

| 키 | 의미 |
| --- | --- |
| `config` | `core.config.Config` 인스턴스(없으면 `load_config()`) |
| `"solver.…"`/`"verify.…"` 점 표기 키 | 그 설정 키 덮어쓰기(시험·요청 단위) |
| `divergence_patterns` | implicit 수렴 실패 문구(정규식) → `DIVERGED` |
| `penetration_pattern` | d3hsp 초기 관통 보고 정규식(첫 그룹 = mm) |
| `rcforc_side_codes` | binout rcforc `side` 값 ↔ slave/master |
| `long_format` | 재료 카드 long 형식 `auto`, `never`, `always` |
| `single_element_stress_var`, `single_element_steps` | 1요소 덱 결과 변수·증분 수 |
| `env` | 배치 실행 환경변수 추가(사내 라이선스 서버 등) |

상태는 객체에 두지 않는다(`PreparedRun` 핸들). 예외: 마지막으로 읽은 솔버 버전 문자열(`solver_version()`).
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any, ClassVar, Literal, Mapping

from ...core import units
from ...core.config import Config, load_config
from ...core.errors import FALLBACK_EXPLICIT_USED, MatObserverError
from ...core.types import (
    CancelToken,
    CurveData,
    Extracted,
    Issue,
    NeutralMaterial,
    PreparedRun,
    RunOverrides,
    RunState,
    RunStatus,
    SimulationCase,
    SlotBinding,
    SolverCapabilities,
)
from ...core.units import UNIT_SYSTEM
from ...deck.assemble import assemble
from ...deck.materials import SUPPORTED_EXTENSIONS, card_text, check_extensions
from .binout import open_binout
from .extract import SUPPORTED_EXTRACTORS, ExtractSettings, extract_case
from .runner import RunnerSettings, run_batch
from .termination import initial_penetration_mm
from . import single_element as se

FALLBACK_DIR = "explicit"
CARD_FORMAT = "lsdyna_k"  # (1.1) SolverCapabilities.card_format

DEFAULT_DIVERGENCE_PATTERNS = (
    r"(?i)nonlinear solver failed",
    r"(?i)convergence failure",
    r"(?i)failed to converge",
    r"(?i)did not converge",
    r"(?i)too many reformations",
)
DEFAULT_PENETRATION_PATTERN = r"initial(?:ly)?\s+penetrat\w*[^\n=]*=\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"

Keep = Literal["all", "results", "minimal"]

#: cleanup 분류 (plugins.md §4.2 keep 표)
_DECK_SUFFIXES = (".k", ".key", ".dyn")
_LOG_NAMES = ("d3hsp", "messag", "mo_run.log")


class SolverRunFailed(MatObserverError):
    """single_element_uniaxial 실행이 정상 종료하지 않음 (state·message를 담는다)."""

    def __init__(self, state: RunState, message: str) -> None:
        super().__init__(f"single-element run {state.value}: {message}")
        self.state = state


def _is_result_kept(name: str, keep: Keep) -> bool:
    if keep == "all":
        return True
    if name.endswith(_DECK_SUFFIXES) or name.startswith("binout") or name in _LOG_NAMES or name.startswith("mes0"):
        return True
    if name.startswith("d3plot"):
        return keep == "results"
    return False


def keep_level(cfg: Config, *, best: bool) -> Keep:
    """plugins.md §4.2 `solver.keep_d3plot` 매핑표 (엔진이 cleanup keep을 고를 때 쓰는 도우미)."""
    if cfg.get("solver.keep_all_files"):
        return "all"
    mode = cfg.get("solver.keep_d3plot")
    if mode == "all":
        return "results"
    if mode == "none":
        return "minimal"
    return "results" if best else "minimal"


class LsDynaAdapter:
    name: ClassVar[str] = "lsdyna"
    version: ClassVar[str] = "1.0.0"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        opts = dict(options or {})
        cfg = opts.pop("config", None)
        if cfg is None:
            cfg = load_config()
        dotted = {k: v for k, v in opts.items() if "." in k}
        if dotted:
            cfg = cfg.with_overrides(dotted)
        self.config: Config = cfg
        self.options = {k: v for k, v in opts.items() if "." not in k}
        self._last_version: str | None = None

    # ---- 설정 ----
    def runner_settings(self) -> RunnerSettings:
        c = self.config
        o = self.options
        return RunnerSettings(
            batch_command=tuple(str(a) for a in c.get("solver.lsdyna.batch_command")),
            script=c.get("solver.lsdyna.script"),
            parallel=str(c.get("solver.lsdyna.mode")),
            precision=str(c.get("solver.lsdyna.precision")),
            termination_patterns=c.get("solver.lsdyna.termination_patterns"),
            license_patterns=tuple(c.get("solver.lsdyna.license_error_patterns")),
            divergence_patterns=tuple(o.get("divergence_patterns", DEFAULT_DIVERGENCE_PATTERNS)),
            version_pattern=str(c.get("solver.lsdyna.version_pattern")),
            kill_grace_s=float(c.get("solver.kill_grace_s")),
            poll_interval_s=float(c.get("solver.poll_interval_s")),
            license_interval_s=float(c.get("solver.license_retry.interval_s")),
            license_max_wait_s=float(c.get("solver.license_retry.max_wait_s")),
            env={str(k): str(v) for k, v in (o.get("env") or {}).items()},
        )

    def extract_settings(self) -> ExtractSettings:
        codes = self.options.get("rcforc_side_codes")
        return ExtractSettings(rcforc_side_codes=dict(codes)) if codes else ExtractSettings()

    @property
    def penetration_pattern(self) -> str:
        return str(self.options.get("penetration_pattern", DEFAULT_PENETRATION_PATTERN))

    # ---- Protocol ----
    def capabilities(self) -> SolverCapabilities:
        kw: dict[str, Any] = dict(
            analysis_modes=frozenset({"implicit", "explicit"}),
            extractors=SUPPORTED_EXTRACTORS,
            material_keywords=frozenset({"MAT_001", "MAT_024"}),
            extension_keys=frozenset(SUPPORTED_EXTENSIONS),
            unit_system=UNIT_SYSTEM,
            supports_overrides=True,
        )
        if "card_format" in {f.name for f in dataclasses.fields(SolverCapabilities)}:  # (1.1) core 반영 후
            kw["card_format"] = CARD_FORMAT
        return SolverCapabilities(**kw)

    def render_card(self, neutral: NeutralMaterial, slot: SlotBinding, options: Mapping[str, Any] | None = None) -> str:
        """(1.1) 결과 카드 렌더링 (plugins.md §4.2, 계약 S13). 결과 NeutralMaterial(유효 범위 절단·외삽 반영 후)을
        그대로 쓴다 — 행을 더하거나 자르지 않는다. 덱 조립과 같은 writer(`deck.materials`), mid·lcid는 슬롯 바인딩 값."""
        o = dict(options or {})
        return card_text(neutral, int(slot.mid), slot.lcid, slot=slot.slot, title=o.get("title"),
                         header_comments=tuple(o.get("header_comments") or ()),
                         long_format=self.options.get("long_format", "auto"))

    def prepare(self, case: SimulationCase, materials: Mapping[str, NeutralMaterial], workdir: Path,
                mode: Literal["implicit", "explicit"] | None = None,
                overrides: RunOverrides | None = None) -> PreparedRun:
        for m in materials.values():
            check_extensions(m)
        if overrides is not None:
            if mode == "implicit":
                raise ValueError("RunOverrides require mode='explicit'")
            mode = "explicit"
        mode = mode or case.analysis_primary
        long_format = self.options.get("long_format", "auto")
        deck = assemble(case, materials, Path(workdir), mode, overrides, long_format=long_format)
        if overrides is None and mode == "implicit" and case.analysis_fallback == "explicit":
            assemble(case, materials, Path(workdir) / FALLBACK_DIR, "explicit", None, long_format=long_format)
        return PreparedRun(workdir=Path(workdir), case=case, main_file=deck.main_file, mode=mode)

    def _fallback_main(self, prepared: PreparedRun) -> Path:
        rel = prepared.main_file.relative_to(prepared.workdir)
        return prepared.workdir / FALLBACK_DIR / rel

    def run(self, prepared: PreparedRun, ncpu: int, timeout_s: float, cancel: CancelToken) -> RunStatus:
        s = self.runner_settings()
        r = run_batch(s, prepared.main_file, ncpu, timeout_s, cancel, prepared.mode)
        self._last_version = r.version or self._last_version
        fb_main = self._fallback_main(prepared)
        if (prepared.mode == "implicit" and r.state in (RunState.ERROR, RunState.DIVERGED)
                and prepared.case.analysis_fallback == "explicit" and fb_main.is_file()):
            r2 = run_batch(s, fb_main, ncpu, timeout_s, cancel, "explicit")
            self._last_version = r2.version or self._last_version
            warn = Issue(code=FALLBACK_EXPLICIT_USED, severity="warning",
                         message=f"implicit {r.state.value} ({r.message}); explicit fallback {r2.state.value}",
                         context={"implicit_state": r.state.value, "implicit_message": r.message})
            return RunStatus(state=r2.state, mode_used="explicit", fallback_used=True,
                             elapsed_s=r.elapsed_s + r2.elapsed_s, termination_message=r2.message,
                             solver_version=r2.version or r.version, license_waits=r.license_waits + r2.license_waits,
                             warnings=(warn,) + r.warnings + r2.warnings)
        return RunStatus(state=r.state, mode_used=prepared.mode, fallback_used=False, elapsed_s=r.elapsed_s,
                         termination_message=r.message, solver_version=r.version, license_waits=r.license_waits,
                         warnings=r.warnings)

    def result_dir(self, prepared: PreparedRun, status: RunStatus) -> Path:
        return self._fallback_main(prepared).parent if status.fallback_used else prepared.main_file.parent

    def extract(self, prepared: PreparedRun, status: RunStatus) -> Extracted:
        rundir = self.result_dir(prepared, status)
        return extract_case(open_binout(rundir), prepared.case, rundir, status.mode_used, self.extract_settings())

    def initial_penetration_m(self, prepared: PreparedRun, status: RunStatus) -> float | None:
        """d3hsp 초기 관통 보고 최대값 [m] (recipe.md §7.2 VERIFY_PENETRATION). 보고가 없으면 None."""
        d3hsp = self.result_dir(prepared, status) / "d3hsp"
        if not d3hsp.is_file():
            return None
        mm = initial_penetration_mm(d3hsp.read_text(encoding="utf-8", errors="replace"), self.penetration_pattern)
        return None if mm is None else units.mm_to_m(mm)

    def solver_version(self) -> str | None:
        return self._last_version

    def cleanup(self, prepared: PreparedRun, keep: Keep) -> None:
        if keep not in ("all", "results", "minimal"):
            raise ValueError(f"keep must be all|results|minimal, got {keep!r}")
        if keep == "all" or self.config.get("solver.keep_all_files"):
            return
        root = Path(prepared.workdir).resolve()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            d = Path(dirpath)
            for n in filenames:
                p = d / n
                if p.is_symlink():
                    continue  # 링크는 따라가지도 지우지도 않는다
                try:
                    p.resolve().relative_to(root)
                except ValueError:
                    continue
                if not _is_result_kept(n, keep):
                    p.unlink()

    def single_element_uniaxial(self, material: NeutralMaterial, strain_max: float, workdir: Path,
                                timeout_s: float) -> CurveData:
        check_extensions(material)
        steps = int(self.options.get("single_element_steps", 100))
        main = se.write_deck(material, strain_max, Path(workdir), steps=steps)

        class _Never:
            def is_set(self) -> bool:
                return False

        r = run_batch(self.runner_settings(), main, 1, timeout_s, _Never(), "implicit")
        self._last_version = r.version or self._last_version
        if r.state is not RunState.NORMAL:
            raise SolverRunFailed(r.state, r.message)
        src = open_binout(main.parent)
        if src is None:
            raise SolverRunFailed(RunState.ERROR, "no binout from single-element run")
        return se.read_curve(src, material, str(self.options.get("single_element_stress_var", "sig_xx")))
