"""`matobserver doctor` D01–D11 (deploy.md §2.2). 각 점검은 `Check` 하나를 돌려주고, 예외를 밖으로 내지 않는다."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import CONTRACT_SCHEMA_VERSION, __version__
from ..core.config import CONFIG_KEYS, Config, resolve_secret, safe_load_yaml
from ..core.errors import ConfigError, RegistryError
from .service import OpsEnv, server_url
from .runtime import service_state
from . import dbinfo

OK, WARN, FAIL, SKIP, INFO = "OK", "WARN", "FAIL", "SKIP", "INFO"

REQUIRED_KEYS = ("paths.work_dir", "recipes.store_dir", "jobstore.sqlite_path", "solver.lsdyna.script",
                 "auth.service_tokens", "auth.admin_tokens")
SCRIPT_EXTS = (".bat", ".cmd", ".ps1", ".exe")
LEGACY_ALIASES = ("engine.elastic_stage.optimizer", "engine.plastic_stage.optimizer")

NAMES = {"D01": "config", "D02": "python_package", "D03": "batch_script", "D04": "lsdyna_version", "D05": "folders",
         "D06": "database", "D07": "registry", "D08": "gmsh", "D09": "matnexus", "D10": "port", "D11": "token_files"}


@dataclass
class Check:
    id: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return NAMES[self.id]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "status": self.status, "detail": self.detail, "data": self.data}


# ------------------------------------------------------------------------------ 설정 로드 (D01 의 일부)
def _leaf_keys(node: Any, prefix: str = "") -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if p in CONFIG_KEYS:
                out.add(p)
            else:
                out |= _leaf_keys(v, p)
    return out


def file_keys(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    try:
        return _leaf_keys(safe_load_yaml(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def d01(cfg: Config, unknown: list[str], config_path: Path | None) -> Check:
    missing: list[str] = []
    for k in REQUIRED_KEYS:
        v = cfg.get(k)
        if v is None or v == "" or (isinstance(v, list) and not v):
            missing.append(k)
    secret_errors: list[str] = []
    for k in ("matnexus.token",):
        try:
            cfg.secret(k)
        except ConfigError as e:
            if str(cfg.get(k)).startswith("file:") or cfg.get("matnexus.base_url"):
                secret_errors.append(str(e))
    if missing or secret_errors:
        return Check("D01", FAIL, "; ".join((["필수 키 누락: " + ", ".join(missing)] if missing else []) + secret_errors),
                     {"missing": missing, "secret_errors": secret_errors})
    warns: list[str] = []
    if unknown:
        warns.append("알 수 없는 키: " + ", ".join(unknown))
    legacy = [k for k in LEGACY_ALIASES if cfg.get(k) is not None]
    if legacy:
        warns.append("1.0 별칭 키 사용: " + ", ".join(legacy))
    if warns:
        return Check("D01", WARN, "; ".join(warns), {"unknown": unknown, "legacy": legacy})
    present = file_keys(config_path)
    new = sorted(k for k in CONFIG_KEYS if present and k not in present and k not in LEGACY_ALIASES)
    if new:
        return Check("D01", INFO, f"설정 파일에 없는 키 {len(new)}개는 코드 기본값 적용", {"defaulted": new})
    return Check("D01", OK, "설정 로드, 필수 키 확인")


def d02(cfg: Config, config_path: Path | None) -> Check:
    if sys.version_info < (3, 11):
        return Check("D02", FAIL, f"Python {sys.version.split()[0]} < 3.11")
    data: dict[str, Any] = {"python": sys.version.split()[0], "package_version": __version__}
    manifest = config_path.resolve().parent.parent / "install.json" if config_path else None
    if manifest is not None and manifest.is_file():
        try:
            installed = json.loads(manifest.read_text(encoding="utf-8")).get("package_version")
        except (OSError, ValueError):
            installed = None
        data["install_json_version"] = installed
        if installed and installed != __version__:
            return Check("D02", WARN, f"install.json {installed} != 패키지 {__version__}", data)
    return Check("D02", OK, f"Python {data['python']}, matobserver {__version__}", data)


def d03(cfg: Config) -> Check:
    script = cfg.get("solver.lsdyna.script")
    if not script:
        return Check("D03", FAIL, "solver.lsdyna.script 미설정")
    p = Path(script).expanduser()
    if not p.is_file():
        return Check("D03", FAIL, "배치 스크립트 파일이 없습니다", {"path": str(p)})
    if not os.access(p, os.R_OK):
        return Check("D03", FAIL, "배치 스크립트를 읽을 수 없습니다", {"path": str(p)})
    if p.suffix.lower() not in SCRIPT_EXTS and not (os.name == "posix" and os.access(p, os.X_OK)):
        return Check("D03", FAIL, f"실행 가능한 형식이 아닙니다 (허용: {', '.join(SCRIPT_EXTS)})", {"path": str(p)})
    return Check("D03", OK, "배치 스크립트 확인", {"path": str(p)})


def _windows_file_version(path: str) -> str | None:  # pragma: no cover - Windows
    import ctypes
    from ctypes import wintypes

    ver = ctypes.windll.version  # type: ignore[attr-defined]
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return None
    lp, ln = ctypes.c_void_p(), wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(lp), ctypes.byref(ln)):
        return None

    class FixedInfo(ctypes.Structure):
        _fields_ = [(n, wintypes.DWORD) for n in ("sig", "sv", "fvms", "fvls", "pvms", "pvls", "ffm", "ff", "fos", "ft", "fst", "dms", "dls")]

    fi = ctypes.cast(lp, ctypes.POINTER(FixedInfo)).contents
    return f"{fi.fvms >> 16}.{fi.fvms & 0xFFFF}.{fi.fvls >> 16}.{fi.fvls & 0xFFFF}"


def d04(cfg: Config, env: OpsEnv) -> Check:
    cmd, exe = cfg.get("solver.lsdyna.version_command"), cfg.get("solver.lsdyna.executable")
    note = "첫 해석 d3hsp 에서 기록"
    if not cmd and not exe:
        return Check("D04", WARN, f"executable·version_command 미설정 ({note})")
    pattern = re.compile(cfg.get("solver.lsdyna.version_pattern"))
    if cmd:
        try:
            r = env.run([str(a) for a in cmd], capture_output=True, text=True, timeout=float(cfg.get("doctor.command_timeout_s")), check=False)
            text = f"{r.stdout or ''}\n{r.stderr or ''}"
        except (OSError, subprocess.TimeoutExpired) as e:
            return Check("D04", WARN, f"version_command 실행 실패: {type(e).__name__} ({note})")
        m = pattern.search(text)
        if m:
            return Check("D04", OK, m.group(1) if m.groups() else m.group(0), {"source": "version_command", "version": m.group(1) if m.groups() else m.group(0)})
        return Check("D04", WARN, f"버전 문자열 추출 실패 ({note})")
    if sys.platform == "win32":  # pragma: no cover - Windows
        try:
            v = _windows_file_version(str(exe))
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return Check("D04", OK, v, {"source": "executable", "version": v})
    return Check("D04", WARN, f"실행 파일 버전 정보를 읽지 못했습니다 ({note})")


def _probe_dir(d: Path) -> str | None:
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".doctor-", dir=d)
        os.close(fd)
        os.unlink(name)
    except OSError as e:
        return f"{d}: {e.strerror or e}"
    return None


def d05(cfg: Config) -> Check:
    from .runtime import log_dir, pid_dir

    dirs = {"paths.work_dir": cfg.path("paths.work_dir"), "recipes.store_dir": cfg.path("recipes.store_dir"),
            "jobstore": cfg.path("jobstore.sqlite_path").parent, "logging.dir": log_dir(cfg), "runtime.pid_dir": pid_dir(cfg)}
    errors = [e for e in (_probe_dir(d) for d in dirs.values()) if e]
    if errors:
        return Check("D05", FAIL, "쓰기 불가: " + "; ".join(errors))
    need = float(cfg.get("doctor.min_free_bytes"))
    free = min(shutil.disk_usage(d).free for d in dirs.values())
    if free < need:
        return Check("D05", WARN, f"여유 공간 {free / 2**30:.1f} GiB < {need / 2**30:.1f} GiB", {"free_bytes": free})
    return Check("D05", OK, f"쓰기 확인, 여유 {free / 2**30:.1f} GiB", {"free_bytes": free})


def d06(cfg: Config) -> Check:
    from ..jobs.db import SCHEMA_VERSION

    db = cfg.path("jobstore.sqlite_path")
    if not db.is_file():
        return Check("D06", OK, "DB 아직 없음 (`matobserver db migrate` 또는 첫 실행이 만듭니다)")
    import sqlite3

    try:
        info = dbinfo.inspect(db)
    except sqlite3.Error as e:
        return Check("D06", FAIL, f"DB 열기 실패: {e}")
    data = {"user_version": info["version"], "expected": SCHEMA_VERSION}
    if info["integrity"] != "ok":
        return Check("D06", FAIL, f"integrity_check: {info['integrity']}", data)
    if info["version"] > SCHEMA_VERSION:
        return Check("D06", FAIL, f"DB 스키마 {info['version']} 가 코드({SCHEMA_VERSION})보다 새롭습니다", data)
    if info["version"] < SCHEMA_VERSION:
        return Check("D06", WARN, f"마이그레이션 필요: `matobserver db migrate` ({info['version']} -> {SCHEMA_VERSION})", data)
    return Check("D06", OK, f"integrity ok, user_version {info['version']}", data)


def d07(cfg: Config) -> Check:
    from ..core.registry import Registry

    try:
        reg = Registry.from_config(cfg)  # enabled 플러그인 import·인터페이스 검사 포함
    except (RegistryError, ConfigError) as e:
        return Check("D07", FAIL, f"레지스트리 로드 실패: {e}")
    except Exception as e:  # noqa: BLE001
        return Check("D07", FAIL, f"레지스트리 오류: {type(e).__name__}: {e}")
    disabled = [f"{e.type}:{e.name}" for e in reg.entries() if not e.enabled]
    if disabled:
        return Check("D07", WARN, "disabled 항목: " + ", ".join(disabled), {"disabled": disabled})
    return Check("D07", OK, f"플러그인 {len(reg.entries())}개 확인")


def d08() -> Check:
    try:
        import gmsh
    except Exception as e:  # noqa: BLE001
        return Check("D08", FAIL, f"gmsh import 실패: {type(e).__name__}: {e}")
    return Check("D08", OK, f"gmsh {getattr(gmsh, '__version__', '?')}", {"version": getattr(gmsh, "__version__", None)})


def d09(cfg: Config, env: OpsEnv) -> Check:
    base = cfg.get("matnexus.base_url")
    if not base:
        return Check("D09", SKIP, "matnexus.base_url 미설정")
    headers: dict[str, str] = {}
    try:
        tok = cfg.secret("matnexus.token", env.environ)
    except ConfigError:
        tok = None
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    url = str(base).rstrip("/") + str(cfg.get("matnexus.probe_path"))
    timeout = float(cfg.get("matnexus.timeout_s"))
    code, _, elapsed = env.http_get(url, headers=headers, timeout=timeout)
    if code is None:
        return Check("D09", FAIL, "MatNexus 연결 실패")
    if code in (401, 403):
        return Check("D09", FAIL, f"MatNexus 인증 실패 (HTTP {code})")
    if code != 200:
        return Check("D09", FAIL, f"MatNexus 응답 HTTP {code}")
    if elapsed > timeout / 2:
        return Check("D09", WARN, f"응답 지연 {elapsed:.1f}s", {"elapsed_s": elapsed})
    return Check("D09", OK, f"HTTP 200 ({elapsed:.2f}s)", {"elapsed_s": elapsed})


def can_bind(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "posix":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def d10(cfg: Config, env: OpsEnv, bind: Callable[[str, int], bool] | None = None) -> Check:
    bind = bind or can_bind
    host, port = str(cfg.get("api.host")), int(cfg.get("api.port"))
    if service_state(cfg, "server")["state"] == "running":
        code, body, _ = env.http_get(f"{server_url(cfg)}/v1/health", timeout=2.0)
        if code == 200 and isinstance(body, dict) and body.get("status") == "ok":
            return Check("D10", OK, f"포트 {port} 를 matobserver 서버가 사용 중")
        return Check("D10", FAIL, f"서버 PID 는 있으나 포트 {port} 가 헬스 응답을 하지 않습니다")
    if bind(host, port):
        return Check("D10", OK, f"{host}:{port} 바인드 가능")
    return Check("D10", FAIL, f"포트 {port} 를 다른 프로세스가 사용 중")


def _token_files(cfg: Config, config_path: Path | None) -> list[Path]:
    out: list[Path] = []
    v = cfg.get("matnexus.token")
    if isinstance(v, str) and v.startswith("file:"):
        out.append(Path(v[5:].strip()).expanduser())
    if config_path is not None:
        sec = config_path.resolve().parent.parent / "secrets"
        if sec.is_dir():
            out += sorted(sec.glob("*.token"))
    seen: dict[str, Path] = {}
    for p in out:
        seen[str(p)] = p
    return [p for p in seen.values() if p.is_file()]


_BROAD = re.compile(r"(Everyone|BUILTIN\\Users|Authenticated Users|NT AUTHORITY\\Authenticated Users)", re.I)


def d11(cfg: Config, config_path: Path | None, env: OpsEnv) -> Check:
    files = _token_files(cfg, config_path)
    if not files:
        return Check("D11", SKIP, "점검할 토큰 파일 없음")
    loose: list[str] = []
    for p in files:
        if env.is_windows:
            try:
                r = env.run(["icacls", str(p)], capture_output=True, text=True, check=False)
                if _BROAD.search(r.stdout or ""):
                    loose.append(str(p))
            except OSError:
                return Check("D11", WARN, "icacls 실행 실패")
        else:
            if p.stat().st_mode & 0o077:
                loose.append(str(p))
    if loose:
        return Check("D11", WARN, "다른 사용자가 읽을 수 있는 토큰 파일: " + ", ".join(loose), {"files": loose})
    return Check("D11", OK, f"토큰 파일 {len(files)}개 현재 사용자 전용", {"files": [str(p) for p in files]})


# ------------------------------------------------------------------------------ 실행
def run_doctor(cfg: Config | None, unknown: list[str], config_path: Path | None, env: OpsEnv,
               load_error: str | None = None, bind: Callable[[str, int], bool] | None = None) -> tuple[int, dict[str, Any]]:
    if cfg is None:
        checks = [Check("D01", FAIL, load_error or "설정 로드 실패")] + [Check(f"D{i:02d}", SKIP, "설정을 읽지 못해 건너뜀") for i in range(2, 12)]
    else:
        checks = [d01(cfg, unknown, config_path), d02(cfg, config_path), d03(cfg), d04(cfg, env), d05(cfg), d06(cfg), d07(cfg),
                  d08(), d09(cfg, env), d10(cfg, env, bind), d11(cfg, config_path, env)]
    summary = {k.lower(): sum(1 for c in checks if c.status == k) for k in (OK, WARN, FAIL, SKIP)}
    report = {"version": __version__, "contract_schema_version": CONTRACT_SCHEMA_VERSION,
              "config_path": None if config_path is None else str(config_path), "summary": summary,
              "checks": [c.to_json() for c in checks]}
    if cfg is None:
        return 3, report
    return (2 if summary["fail"] else 1 if summary["warn"] else 0), report
