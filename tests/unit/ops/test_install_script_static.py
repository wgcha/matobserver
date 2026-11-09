"""install.ps1 정적 점검 (Linux 에서 가능한 범위): deploy.md §1.2 매개변수·§1.7 종료 코드 일치, 인코딩, PowerShell 구문 (pwsh 가 있을 때).

실제 Windows 실행은 CI(`windows-install` 잡, scripts/windows/tests/smoke.ps1)가 검증한다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "windows" / "install.ps1"
SMOKE = REPO / "scripts" / "windows" / "tests" / "smoke.ps1"
DEPLOY = REPO / "docs" / "contracts" / "deploy.md"


def test_scripts_exist_with_utf8_bom_for_windows_powershell_5_1():
    for p in (SCRIPT, SMOKE):
        assert p.is_file()
        assert p.read_bytes().startswith(b"\xef\xbb\xbf"), f"{p.name}: Windows PowerShell 5.1 이 한글을 읽으려면 UTF-8 BOM 필요"


def test_parameters_match_contract():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    block = text[text.index("param("):text.index("Set-StrictMode")]
    params = set(re.findall(r"\$([A-Za-z]+)\s*(?:=|,|\))", block))
    expected = {"InstallRoot", "DataRoot", "Python", "Tag", "Repo", "GitHubTokenFile", "Wheel", "WheelDir", "LsDynaBatchScript", "Ncpu",
                "MatNexusUrl", "MatNexusTokenFile", "Port", "NonInteractive", "NoSchedule", "AtStartup", "Update", "AllowMajor",
                "Uninstall", "PurgeData", "Force"}
    assert params == expected
    assert "[int]$Port = 8040" in block  # deploy.md §1.2 기본 포트


@pytest.mark.skipif(not DEPLOY.exists(), reason="docs/ not available")
def test_exit_codes_used_are_documented():
    doc = DEPLOY.read_text(encoding="utf-8")
    sec = doc[doc.index("### 1.7 종료 코드"):doc.index("### 1.8 로그")]
    documented = {int(m) for m in re.findall(r"^\| (\d+) \|", sec, re.M)}
    used = {int(m) for m in re.findall(r"Stop-Install\s+(\d+)\b", SCRIPT.read_text(encoding="utf-8-sig"))}
    assert used <= documented, f"문서에 없는 종료 코드: {used - documented}"
    assert {10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23} <= used, f"쓰이지 않는 종료 코드: {documented - used}"


def test_never_logs_secrets():
    text = SCRIPT.read_text(encoding="utf-8-sig")
    assert "Write-Host $svcTok" not in text and "Write-Host $admTok" not in text
    assert "sha256" in text.lower()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh 없음")
@pytest.mark.parametrize("path", [SCRIPT, SMOKE], ids=lambda p: p.name)
def test_powershell_syntax(path):
    code = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:PS_PARSE_TARGET,[ref]$t,[ref]$e);"
            "if($e.Count){$e|%{$_.Message};exit 1}")
    r = subprocess.run(["pwsh", "-NoProfile", "-Command", code], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PS_PARSE_TARGET": str(path)})
    assert r.returncode == 0, r.stdout + r.stderr
