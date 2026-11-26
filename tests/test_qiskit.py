import random

import pytest

qiskit = pytest.importorskip("qiskit")
from qiskit import QuantumCircuit
from qiskit.transpiler import CouplingMap, PassManager as TPassManager, Target

from rozier_piecepack import PiecePackLayout, search_layout, transpile_with_fallback
from rozier_piecepack.qiskit_tools import _StopAfterVF2Miss, _is_abort, chip_graph, error_maps, interaction_graph


def planted_circuit(cmap, q, seed, layers=2):
    r = random.Random(seed)
    edges = sorted({tuple(sorted(e)) for e in cmap.get_edges()})
    used = [e for e in edges if r.random() < q]
    n = cmap.size()
    phys = list(range(n))
    r.shuffle(phys)
    log = {p: i for i, p in enumerate(phys)}
    qc = QuantumCircuit(n)
    for _ in range(layers):
        qc.rx(0.3, range(n))
        for a, b in used:
            qc.cz(log[a], log[b])
    return qc


def test_pass_sets_valid_layout():
    cmap = CouplingMap.from_heavy_hex(5)
    qc = planted_circuit(cmap, 0.8, 1)
    pm = TPassManager([PiecePackLayout(coupling_map=cmap, time_limit=20)])
    pm.run(qc)
    assert pm.property_set["PiecePackLayout_stop_reason"] == "FOUND"
    layout = pm.property_set["layout"]
    edges = {tuple(sorted(e)) for e in cmap.get_edges()}
    for inst in qc.data:
        if len(inst.qubits) == 2:
            a, b = (layout[q] for q in inst.qubits)
            assert tuple(sorted((a, b))) in edges


def test_pass_skips_when_layout_set():
    cmap = CouplingMap.from_heavy_hex(3)
    qc = planted_circuit(cmap, 0.8, 2)
    from qiskit.transpiler.passes import SetLayout
    pm = TPassManager([SetLayout(list(range(qc.num_qubits))), PiecePackLayout(coupling_map=cmap)])
    pm.run(qc)
    assert pm.property_set["PiecePackLayout_stop_reason"] == "NOT_RUN"


def test_fallback_happy_path_is_stock():
    cmap = CouplingMap.from_heavy_hex(3)
    target = Target.from_configuration(basis_gates=["cz", "rz", "sx", "x"], coupling_map=cmap)
    qc = planted_circuit(cmap, 0.8, 3)
    out, info = transpile_with_fallback(qc, target, seed_transpiler=1)
    assert info["vf2_stop"] == "SOLUTION_FOUND"
    assert info["used"] == "stock" and info["search"] == "NOT_RUN" and not info["aborted_stock"]
    assert out.num_qubits == cmap.size()


def triangle_circuit(n):
    qc = QuantumCircuit(n)
    qc.cz(0, 1); qc.cz(1, 2); qc.cz(0, 2)       # a triangle can't sit on heavy-hex (no odd cycles)
    return qc


def test_early_exit_falls_back_to_sabre():
    cmap = CouplingMap.from_heavy_hex(3)
    target = Target.from_configuration(basis_gates=["cz", "rz", "sx", "x"], coupling_map=cmap)
    out, info = transpile_with_fallback(triangle_circuit(5), target, seed_transpiler=1, time_limit=5)
    assert info["vf2_stop"] == "NO_SOLUTION_FOUND"
    assert info["aborted_stock"] is True            # the stock run was stopped right after VF2Layout
    assert info["search"] in ("NONE", "TIMEOUT")
    assert info["used"] == "sabre (stock fallback)"
    assert info["final_2q"] >= 3                   # routed: needs at least one swap's worth of extra gates


def test_no_early_exit_keeps_stock():
    cmap = CouplingMap.from_heavy_hex(3)
    target = Target.from_configuration(basis_gates=["cz", "rz", "sx", "x"], coupling_map=cmap)
    out, info = transpile_with_fallback(triangle_circuit(5), target, seed_transpiler=1, time_limit=5, early_exit=False)
    assert info["vf2_stop"] == "NO_SOLUTION_FOUND" and not info["aborted_stock"]
    assert info["used"] == "stock" and info["final_2q"] == info["stock_2q"]


def test_search_layout_stages_and_validity():
    from qiskit.converters import circuit_to_dag
    cmap = CouplingMap.from_heavy_hex(5)
    qc = planted_circuit(cmap, 0.8, 4)
    dag = circuit_to_dag(qc)
    chip, pattern = chip_graph(cmap), interaction_graph(dag)
    mapping, info = search_layout(pattern, chip, device=cmap, circuit=qc, time_limit=20)
    assert info["status"] == "FOUND" and info["stage"] is not None
    edges = {tuple(sorted(e)) for e in cmap.get_edges()}
    for a, b in pattern.edge_list():
        pa, pb = pattern[a], pattern[b]
        assert tuple(sorted((mapping[pa], mapping[pb]))) in edges


def test_error_maps_and_abort_chain():
    cmap = CouplingMap.from_heavy_hex(3)
    target = Target.from_configuration(basis_gates=["cz", "rz", "sx", "x"], coupling_map=cmap)
    e2, e1 = error_maps(target)                      # no error data here: empty is fine, must not crash
    assert isinstance(e2, dict) and isinstance(e1, dict)
    try:
        try:
            raise _StopAfterVF2Miss()
        except _StopAfterVF2Miss as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert _is_abort(outer)
    assert not _is_abort(ValueError("other"))
