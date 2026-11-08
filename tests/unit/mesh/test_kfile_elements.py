"""kfile writer/reader(§2 형식)와 요소 기하 함수 단위 테스트."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from matobserver.mesh import elements, kfile, structured


# ------------------------------------------------------------------ 고정 폭 필드
def test_e16_and_i8_widths():
    assert kfile.e16(-0.0) == " 0.000000000e+00"
    assert len(kfile.e16(-1234.5678)) == 16
    assert kfile.i8(5_000_000) == " 5000000"
    with pytest.raises(kfile.KFormatError):
        kfile.i8(123_456_789)
    with pytest.raises(kfile.KFormatError):
        kfile.e16(float("nan"))


def test_f10_roundtrip_precision():
    for v in (7.85e-9, 2.1e5, 0.3, 1.0, 7.8125e-9, 1 / 3, -0.25):
        s = kfile.f10(v)
        assert len(s) == 10 and s[0] == " "
        assert abs(float(s) - v) <= 1e-4 * abs(v)


def test_quantize_matches_file_text():
    x = np.array([[0.0123456789012345, -0.0, 1.0 / 3.0]])
    mm = kfile.quantize_mm(x)
    assert [float(kfile.e16(v)) for v in mm[0]] == list(mm[0])


def _tiny(topology: str) -> str:
    coords = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
    conn = np.array([[1, 2, 3, 4]])
    if topology == "tet10":
        nodes, t10 = structured.add_midside(coords, np.array([[0, 1, 2, 3]]))
        coords, conn = nodes, t10 + 1
    hdr = kfile.header_lines("box", "1.0.0", topology, 1e-3, "ab" * 32)
    return kfile.specimen_text(header=hdr, topology=topology, node_ids=list(range(1, len(coords) + 1)),
                               coords_mm=coords, elem_ids=[1], elem_pids=[7], elem_nodes=conn,
                               node_sets=[kfile.NodeSet(901, "sym_x0", (1, 3, 4))], part_set=(900, [7]))


def test_tet4_degenerate_two_line_format():
    text = _tiny("tet4")
    lines = text.splitlines()
    i = lines.index("*ELEMENT_SOLID")
    assert lines[i + 1] == "       1       7"
    assert lines[i + 2] == "".join(kfile.i8(n) for n in (1, 2, 3, 4, 4, 4, 4, 4))
    kf = kfile.read_k(text.encode())
    assert kfile.solid_topology(kf.solids[1][1]) == "tet4"
    assert kf.node_sets[901] == ("sym_x0", (1, 3, 4)) and kf.part_sets[900] == (7,)


def test_tet10_line_is_80_columns_and_order():
    text = _tiny("tet10")
    lines = text.splitlines()
    i = lines.index("*ELEMENT_SOLID")
    assert len(lines[i + 2]) == 80
    kf = kfile.read_k(text.encode())
    _pid, conn = kf.solids[1]
    assert kfile.solid_topology(conn) == "tet10"
    X = np.array([kf.nodes[n] for n in conn])
    for k, (a, b) in enumerate(elements.TET10_EDGES):  # N5=1-2, N6=2-3, N7=3-1, N8=1-4, N9=2-4, N10=3-4
        assert np.allclose(X[4 + k], 0.5 * (X[a] + X[b]))
    assert [tuple(e) for e in elements.TET10_EDGES] == [(0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)]


def test_header_fields():
    kf = kfile.read_k(_tiny("tet4").encode())
    h = kf.header_fields()
    assert kf.header[0] == "$ matobserver mesh"
    assert h["generator"] == "box" and h["topology"] == "tet4" and h["target_size_mm"] == "1"
    assert h["units"] == "mm-N-s-tonne" and h["input_sha256"] == "ab" * 32


@pytest.mark.parametrize("mutate, msg", [
    (lambda t: t.replace("\n", "\r\n"), "CR"),
    (lambda t: t.replace("*KEYWORD\n", "", 1), "first line"),
    (lambda t: t.replace("*END\n", ""), "last line"),
    (lambda t: t.replace("*SET_PART_LIST", "*PART"), "not allowed"),
    (lambda t: t.replace("$ matobserver mesh\n", ""), "header"),
    (lambda t: t.replace("       1       7\n", "       1       7       1\n"), "EID i8 PID i8"),
])
def test_reader_rejects_violations(mutate, msg):
    with pytest.raises(kfile.KFormatError, match=msg):
        kfile.read_k(mutate(_tiny("tet4")).encode())


def test_reserved_axisym_writer():
    with pytest.raises(kfile.KFormatError, match="reserved"):
        kfile.specimen_text(header=[], topology="quad4_axisym", node_ids=[], coords_mm=np.zeros((0, 3)),
                            elem_ids=[], elem_pids=[], elem_nodes=np.zeros((0, 4)), node_sets=[], part_set=None)


# ------------------------------------------------------------------ 요소 기하
CUBE = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)


def test_hex_unit_cube():
    X = (CUBE * [2.0, 1.0, 0.5])[None]
    assert np.isclose(elements.hex_volume(X)[0], 1.0)
    sj, ar = elements.hex_quality(X)
    assert np.isclose(sj[0], 1.0) and np.isclose(ar[0], 4.0)


def test_regular_tet_quality_and_inverted():
    X = np.array([[[1, 1, 1], [-1, 1, -1], [1, -1, -1], [-1, -1, 1]]], float)
    sj, ar = elements.tet4_quality(X)
    assert np.isclose(sj[0], 1.0) and np.isclose(ar[0], 1.0)
    assert elements.tet4_volume(X)[0] > 0
    assert elements.tet4_volume(X[:, [0, 2, 1, 3]])[0] < 0


def test_kuhn_split_positive_and_volume():
    tets = CUBE[structured.KUHN]
    v = elements.tet4_volume(tets)
    assert (v > 0).all() and np.isclose(v.sum(), 1.0)


def test_tet10_straight_and_curved_volume():
    corners = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
    nodes, t10 = structured.add_midside(corners, np.array([[0, 1, 2, 3]]))
    X = nodes[t10]
    assert np.isclose(elements.tet10_volume(X)[0], 1 / 6)
    sj, _ = elements.tet10_quality(X)
    assert np.isclose(sj[0], elements.tet4_quality(X[:, :4])[0][0])
    # 변 1-2 중간절점을 휘게 한 곡면 요소: Keast 5점(3차 정확) == 고차 수치 적분
    Xc = X.copy()
    Xc[0, 4] += [0.02, -0.05, 0.03]
    ref = _numeric_volume(Xc[0])
    assert np.isclose(elements.tet10_volume(Xc)[0], ref, rtol=1e-12)
    assert (elements.tet10_detj(Xc) > 0).all()


def _numeric_volume(X10: np.ndarray) -> float:
    """고차 적분(층별 Gauss 텐서곱 + Duffy 변환)으로 detJ 적분 — tet10_volume 검증용."""
    g, w = np.polynomial.legendre.leggauss(8)
    g, w = 0.5 * (g + 1), 0.5 * w
    total = 0.0
    for (a, wa), (b, wb), (c, wc) in itertools.product(zip(g, w), zip(g, w), zip(g, w)):
        xi = a
        eta = b * (1 - a)
        zeta = c * (1 - a) * (1 - b)
        jac = (1 - a) ** 2 * (1 - b)
        dN = elements._tet10_dN(np.array([xi, eta, zeta]))
        total += wa * wb * wc * jac * np.linalg.det(X10.T @ dN)
    return total


def test_grid_conforming_tet_mesh():
    xs = ys = zs = np.linspace(0, 1, 3)
    nodes, tets, _cells = structured.build("tet4", xs, ys, zs)
    faces: dict[tuple, int] = {}
    for t in tets:
        for f in itertools.combinations(sorted(t), 3):
            faces[f] = faces.get(f, 0) + 1
    assert max(faces.values()) == 2  # 내부 면은 정확히 두 요소가 공유(적합)
    assert np.isclose(elements.tet4_volume(nodes[tets]).sum(), 1.0)
