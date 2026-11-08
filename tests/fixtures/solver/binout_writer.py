"""합성 binout(LSDA) writer — 시험 픽스처·가짜 LS-DYNA 전용 (Impl-Solver).

실제 LS-DYNA binout과 같은 디렉터리 모양을 쓴다: `/<block>/metadata/<var>`(ids 등),
`/<block>/dNNNNNN/{time, <var>...}`. 하위 블록은 `elout/solid`처럼 `/`로 잇는다.
lasso-python `lasso.dyna.lsda_py3.Lsda`의 쓰기 모드를 쓴다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

I4, R8 = 3, 10


def write_binout(path: Path, blocks: Mapping[str, Mapping[str, object]]) -> None:
    """blocks = {block: {"metadata": {var: ints}, "time": [t...], "series": {var: (n_t, n_col) array}}}."""
    from lasso.dyna.lsda_py3 import Lsda

    lsda = Lsda(str(path), "w")
    for block, spec in blocks.items():
        base = "/" + block.strip("/")
        meta: Mapping[str, Sequence[int]] = spec.get("metadata", {})  # type: ignore[assignment]
        if meta:
            lsda.cd(base + "/metadata", 1)
            for var, vals in meta.items():
                lsda.write(var, I4, [int(v) for v in np.asarray(vals).reshape(-1)])
        time = np.asarray(spec["time"], dtype=float)
        series: Mapping[str, np.ndarray] = spec.get("series", {})  # type: ignore[assignment]
        for i, t in enumerate(time):
            lsda.cd(f"{base}/d{i + 1:06d}", 1)
            lsda.write("time", R8, [float(t)])
            for var, arr in series.items():
                a = np.asarray(arr, dtype=float)
                row = a[i] if a.ndim > 1 else a[i:i + 1]
                lsda.write(var, R8, [float(v) for v in np.asarray(row).reshape(-1)])
    lsda.close()
