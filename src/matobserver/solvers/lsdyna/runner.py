"""사내 배치 스크립트 래핑 실행 (plugins.md §4.2 `run`, module-boundaries.md §6 `solver.lsdyna`).

* 명령 = `solver.lsdyna.batch_command`(인자 템플릿 리스트) 치환. 치환 키: `{script}` `{main}`(절대 경로)
  `{main_name}`(파일명) `{workdir}` `{ncpu}` `{mode}`(smp|mpp) `{precision}`(single|double) `{analysis}`(implicit|explicit).
  경로·인자는 코드에 쓰지 않는다.
* 작업 디렉터리 = 메인 덱 폴더. stdout/stderr → `mo_run.log`.
* 시간 상한: 시도당 `timeout_s`. 초과 시 SIGTERM(프로세스 그룹) → `kill_grace_s` 후 SIGKILL → TIMEOUT.
* 취소: `poll_interval_s` 간격으로 `CancelToken.is_set()` 확인 → 같은 종료 절차 → CANCELLED.
* 라이선스 오류: `license_retry.interval_s` 간격 재시도, 누적 대기가 `max_wait_s`를 넘으면 LICENSE_TIMEOUT.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...core.errors import SOLVER_UNAVAILABLE, MatObserverError
from ...core.types import CancelToken, Issue, RunState
from .termination import LOG_FILES, RUN_LOG, judge, read_logs


class SolverUnavailable(MatObserverError):
    """배치 스크립트 미설정·실행 불가 (api.md §7.3 SOLVER_UNAVAILABLE)."""

    code = SOLVER_UNAVAILABLE


@dataclass(frozen=True)
class RunnerSettings:
    batch_command: tuple[str, ...]
    script: str | None
    parallel: str
    precision: str
    termination_patterns: Mapping[str, Sequence[str]]
    license_patterns: tuple[str, ...]
    divergence_patterns: tuple[str, ...]
    version_pattern: str
    kill_grace_s: float
    poll_interval_s: float
    license_interval_s: float
    license_max_wait_s: float
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AttemptResult:
    state: RunState
    elapsed_s: float
    message: str
    version: str | None
    license_waits: int
    warnings: tuple[Issue, ...]


_STALE_PREFIXES = ("d3hsp", "messag", "mes0", "binout", "d3plot", "glstat", "rcforc", "nodout", "matsum", RUN_LOG)


def _clear_stale(rundir: Path) -> None:
    """이전 시도의 판정 파일을 지운다(작업 폴더 직속 파일만) — 낡은 'Normal termination'을 읽지 않게."""
    for p in rundir.iterdir():
        if p.is_file() and p.name.startswith(_STALE_PREFIXES):
            p.unlink()


def build_command(s: RunnerSettings, main_file: Path, ncpu: int, analysis: str) -> list[str]:
    values = {
        "script": s.script,
        "main": str(Path(main_file).resolve()),
        "main_name": Path(main_file).name,
        "workdir": str(Path(main_file).resolve().parent),
        "ncpu": str(int(ncpu)),
        "mode": s.parallel,
        "precision": s.precision,
        "analysis": analysis,
    }
    if any("{script}" in a for a in s.batch_command) and not s.script:
        raise SolverUnavailable("solver.lsdyna.script is not configured")
    try:
        return [a.format(**values) for a in s.batch_command]
    except KeyError as e:
        raise SolverUnavailable(f"solver.lsdyna.batch_command has unknown placeholder {e}") from None


#: Windows CREATE_NEW_PROCESS_GROUP (subprocess 상수는 Windows 에만 있다)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def popen_kwargs(os_name: str = os.name) -> dict[str, Any]:
    """프로세스 트리 단위로 끝낼 수 있게 띄우는 옵션. POSIX: 새 세션(=프로세스 그룹), Windows: 새 프로세스 그룹."""
    if os_name == "nt":
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def taskkill_command(pid: int) -> list[str]:
    """Windows 프로세스 트리 강제 종료 (배치 스크립트 → LS-DYNA 자식까지)."""
    return ["taskkill", "/T", "/F", "/PID", str(int(pid))]


def _terminate(proc: subprocess.Popen[Any], grace_s: float, os_name: str = os.name) -> None:
    """배치 스크립트와 그 자식(LS-DYNA) 트리를 끝낸다 (시간 초과·취소 공통).

    POSIX: 그룹에 SIGTERM → grace 대기 → 그룹에 SIGKILL(남은 자식까지). Windows: `taskkill /T /F /PID`
    (실패하면 `Popen.kill()` 폴백 — 이 경우 자식은 남을 수 있다)."""
    if os_name == "nt":
        if proc.poll() is None:
            try:
                r = subprocess.run(taskkill_command(proc.pid), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=max(grace_s, 1.0) + 10.0)
                ok = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if not ok and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=max(grace_s, 0.0))
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # TERM 을 무시한 자식까지
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _attempt(s: RunnerSettings, cmd: list[str], rundir: Path, timeout_s: float,
             cancel: CancelToken) -> tuple[str, float, RunState | None]:
    """한 번 실행. 반환 (종료 사유 'exit'|'timeout'|'cancel', 경과, 강제 상태)."""
    t0 = time.monotonic()
    env = dict(os.environ)
    env.update(s.env)
    with open(rundir / RUN_LOG, "wb") as log:
        try:
            proc = subprocess.Popen(cmd, cwd=rundir, stdout=log, stderr=subprocess.STDOUT, env=env, **popen_kwargs())
        except OSError as e:
            raise SolverUnavailable(f"cannot start batch command {cmd[0]!r}: {e}") from e
        poll = max(min(s.poll_interval_s, 1.0), 0.01)
        while True:
            try:
                proc.wait(timeout=poll)
                return "exit", time.monotonic() - t0, None
            except subprocess.TimeoutExpired:
                pass
            if cancel.is_set():
                _terminate(proc, s.kill_grace_s)
                return "cancel", time.monotonic() - t0, RunState.CANCELLED
            if time.monotonic() - t0 > timeout_s:
                _terminate(proc, s.kill_grace_s)
                return "timeout", time.monotonic() - t0, RunState.TIMEOUT


def _sleep_cancellable(seconds: float, poll: float, cancel: CancelToken) -> bool:
    """대기. 취소되면 True."""
    end = time.monotonic() + seconds
    while True:
        if cancel.is_set():
            return True
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(poll, left))


def run_batch(s: RunnerSettings, main_file: Path, ncpu: int, timeout_s: float, cancel: CancelToken,
              analysis: str) -> AttemptResult:
    rundir = Path(main_file).parent
    cmd = build_command(s, main_file, ncpu, analysis)
    waited = 0.0
    waits = 0
    total = 0.0
    while True:
        _clear_stale(rundir)
        reason, elapsed, forced = _attempt(s, cmd, rundir, timeout_s, cancel)
        total += elapsed
        logs = read_logs(rundir)
        info = judge(logs, termination_patterns=s.termination_patterns, license_patterns=s.license_patterns,
                     divergence_patterns=s.divergence_patterns, version_pattern=s.version_pattern)
        if forced is not None:
            msg = "cancelled" if forced is RunState.CANCELLED else f"timeout after {timeout_s:g} s"
            return AttemptResult(forced, total, msg, info.version, waits, ())
        if info.verdict == "license":
            if waited + s.license_interval_s > s.license_max_wait_s:
                return AttemptResult(RunState.LICENSE_TIMEOUT, total, info.message, info.version, waits, ())
            waits += 1
            if _sleep_cancellable(s.license_interval_s, max(s.poll_interval_s, 0.01), cancel):
                return AttemptResult(RunState.CANCELLED, total, "cancelled during license wait", info.version, waits, ())
            waited += s.license_interval_s
            continue
        state = {"normal": RunState.NORMAL, "diverged": RunState.DIVERGED}.get(info.verdict, RunState.ERROR)
        msg = info.message or ("no termination message in d3hsp/messag" if state is RunState.ERROR else "")
        return AttemptResult(state, total, msg, info.version, waits, ())


__all__ = ["RunnerSettings", "AttemptResult", "SolverUnavailable", "build_command", "run_batch", "LOG_FILES"]
