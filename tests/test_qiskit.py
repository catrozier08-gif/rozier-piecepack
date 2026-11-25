import random

import pytest

qiskit = pytest.importorskip("qiskit")
from qiskit import QuantumCircuit
from qiskit.transpiler import CouplingMap, PassManager as TPassManager, Target

from rozier_piecepack import PiecePackLayout, transpile_with_fallback


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
    assert info["used"] == "stock" and info["piece_pack"] == "NOT_RUN"
    assert out.num_qubits == cmap.size()
