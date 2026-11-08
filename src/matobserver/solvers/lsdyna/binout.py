"""binout 리더 추상화 (module-boundaries.md §1: binout 읽기 라이브러리 자유 — 1차 lasso-python).

`ResultSource`는 블록·변수 이름으로 배열을 준다(LSDA 경로 `/<block>/metadata/<var>`, `/<block>/dNNNNNN/<var>`).
시계열 변수는 (n_time, n_col) 2차원으로 정규화한다. 다른 리더(사내 l2a 변환 CSV 등)는 같은 Protocol로 교체한다.
"""

from __future__ import annotations

import glob
from pathlib import Path
from typing import Mapping, Protocol, Sequence

import numpy as np


class BinoutUnavailable(RuntimeError):
    """binout 파일 없음·읽기 실패·리더 라이브러리 없음."""


class ResultSource(Protocol):
    def blocks(self) -> list[str]: ...
    def variables(self, block: str) -> list[str]: ...
    def read(self, block: str, var: str) -> np.ndarray: ...


class LassoBinout:
    """lasso-python `lasso.dyna.Binout` 래퍼 (`binout*` 분할 파일 포함)."""

    def __init__(self, files: Sequence[Path]) -> None:
        try:
            from lasso.dyna import Binout  # 지연 import: 의존성 없는 환경에서도 패키지 import는 된다
        except Exception as e:  # pragma: no cover - 의존성 누락
            raise BinoutUnavailable(f"lasso-python is not installed: {e!r}") from e
        if not files:
            raise BinoutUnavailable("no binout files")
        first = str(files[0])
        pattern = first if len(files) == 1 else str(Path(first).parent / "binout*")
        try:
            self._b = Binout(pattern)
        except Exception as e:
            raise BinoutUnavailable(f"cannot open binout {pattern}: {e!r}") from e

    def blocks(self) -> list[str]:
        return list(self._b.read())

    def variables(self, block: str) -> list[str]:
        try:
            return list(self._b.read(*block.split("/")))
        except Exception:
            return []

    def read(self, block: str, var: str) -> np.ndarray:
        """`block`은 하위 폴더를 `/`로 이어 쓸 수 있다(예 `elout/solid`)."""
        try:
            return np.asarray(self._b.read(*block.split("/"), var))
        except Exception as e:
            raise KeyError(f"binout {block}/{var}: {e!r}") from e


class DictSource:
    """메모리 원본(시험·다른 변환기용): {block: {var: array}}."""

    def __init__(self, data: Mapping[str, Mapping[str, np.ndarray]]) -> None:
        self._d = {b: {k: np.asarray(v) for k, v in vs.items()} for b, vs in data.items()}

    def blocks(self) -> list[str]:
        return list(self._d)

    def variables(self, block: str) -> list[str]:
        return list(self._d.get(block, {}))

    def read(self, block: str, var: str) -> np.ndarray:
        try:
            return self._d[block][var]
        except KeyError:
            raise KeyError(f"{block}/{var}") from None


def binout_files(rundir: Path) -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(Path(rundir) / "binout*")) if Path(p).is_file())


def open_binout(rundir: Path) -> ResultSource | None:
    files = binout_files(rundir)
    if not files:
        return None
    try:
        return LassoBinout(files)
    except BinoutUnavailable:
        return None


def series(src: ResultSource, block: str, var: str, ncol: int) -> np.ndarray:
    """시계열 변수를 (n_time, ncol)로. lasso는 열이 1개면 1차원으로 준다."""
    a = np.asarray(src.read(block, var), dtype=float)
    if a.ndim == 1:
        a = a.reshape(-1, 1) if ncol == 1 else a.reshape(-1, ncol)
    return a
