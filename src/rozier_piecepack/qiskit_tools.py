"""
Qiskit integration for piece-pack.

transpile_with_fallback(): runs the stock preset pass manager first. Only if its VF2Layout pass
reports NO_SOLUTION_FOUND does piece-pack run, on the exact interaction graph VF2Layout saw. If
piece-pack finds a zero-swap layout, the circuit is compiled again with that layout and the result
with fewer two-qubit gates is returned. On the happy path (VF2Layout succeeds) nothing extra runs.

PiecePackLayout: an AnalysisPass for custom pass managers. It sets property_set["layout"] when it
finds a zero-swap layout, and records PiecePackLayout_stop_reason / PiecePackLayout_stats.
"""
import time

import rustworkx as rx
from qiskit.transpiler.basepasses import AnalysisPass
from qiskit.transpiler.layout import Layout

from .core import FOUND, piece_pack

UNSUPPORTED = "UNSUPPORTED"   # gates on 3+ qubits / control flow, no connectivity, or too many qubits
NOT_RUN = "NOT_RUN"


def chip_graph(target_or_coupling_map):
    """rx.PyGraph of the device, node payload = physical qubit. None if the device has no connectivity limits."""
    cmap = target_or_coupling_map
    if hasattr(cmap, "build_coupling_map"):
        cmap = cmap.build_coupling_map()
    if cmap is None:
        return None
    g = rx.PyGraph()
    g.add_nodes_from(list(range(cmap.size())))
    g.add_edges_from_no_data(sorted({tuple(sorted(e)) for e in cmap.get_edges() if e[0] != e[1]}))
    return g


def interaction_graph(dag):
    """rx.PyGraph of the qubits that take part in 2q gates, node payload = qubit index in the DAG.
    Returns None if the DAG has operations on 3+ qubits (including control flow), like VF2Layout."""
    idx = {q: i for i, q in enumerate(dag.qubits)}
    edges = set()
    for node in dag.op_nodes(include_directives=False):
        n = len(node.qargs)
        if n > 2:
            return None
        if n == 2:
            a, b = idx[node.qargs[0]], idx[node.qargs[1]]
            if a != b:
                edges.add((min(a, b), max(a, b)))
    active = sorted({q for e in edges for q in e})
    g = rx.PyGraph()
    g.add_nodes_from(active)
    pos = {q: i for i, q in enumerate(active)}
    g.add_edges_from_no_data(sorted((pos[a], pos[b]) for a, b in edges))
    return g


def full_layout(mapping, num_virtual, num_physical):
    """List of length num_virtual: virtual qubit i -> physical qubit. Qubits not in `mapping` (idle in
    every 2q gate) get the unused physical qubits in ascending order."""
    free = [p for p in range(num_physical) if p not in set(mapping.values())]
    out = []
    for v in range(num_virtual):
        out.append(mapping[v] if v in mapping else free.pop(0))
    return out


def _run_on(dag, chip, num_physical, time_limit):
    pattern = interaction_graph(dag)
    if chip is None or pattern is None or dag.num_qubits() > num_physical:
        return None, UNSUPPORTED, {}
    mapping, status, stats = piece_pack(chip, pattern, time_limit=time_limit)
    if status != FOUND:
        return None, status, stats
    return full_layout(mapping, dag.num_qubits(), num_physical), status, stats


class PiecePackLayout(AnalysisPass):
    """Zero-swap layout search for interaction graphs split into several pieces.

    Args:
        target: a Target (or pass coupling_map instead).
        coupling_map: a CouplingMap, used if target is None.
        time_limit: seconds for the search.
        only_if_unset: if True (default), do nothing when property_set["layout"] is already set,
            so the pass can sit after VF2Layout as a fallback.
    """

    def __init__(self, target=None, coupling_map=None, time_limit=30.0, only_if_unset=True):
        super().__init__()
        self.target = target
        self.coupling_map = coupling_map
        self.time_limit = time_limit
        self.only_if_unset = only_if_unset

    def run(self, dag):
        if self.only_if_unset and self.property_set["layout"] is not None:
            self.property_set["PiecePackLayout_stop_reason"] = NOT_RUN
            return
        device = self.target if self.target is not None else self.coupling_map
        chip = chip_graph(device) if device is not None else None
        num_physical = chip.num_nodes() if chip is not None else 0
        layout, status, stats = _run_on(dag, chip, num_physical, self.time_limit)
        self.property_set["PiecePackLayout_stop_reason"] = status
        self.property_set["PiecePackLayout_stats"] = stats
        if layout is not None:
            self.property_set["layout"] = Layout({dag.qubits[v]: p for v, p in enumerate(layout)})


def _twoq(circuit):
    return sum(1 for inst in circuit.data if len(inst.qubits) == 2)


def _reason_name(r):
    return getattr(r, "name", None if r is None else str(r))


def transpile_with_fallback(circuit, target, optimization_level=3, seed_transpiler=None, time_limit=30.0,
                            **pass_manager_kwargs):
    """Stock preset pass manager first; piece-pack only if its VF2Layout returns NO_SOLUTION_FOUND.

    Extra keyword arguments go to generate_preset_pass_manager. If initial_layout is given, the stock
    pass manager is used as-is (there is no layout to search for).

    Returns (transpiled circuit, info dict). info["used"] is "stock" or "piece-pack".
    """
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    def make_pm(**extra):
        return generate_preset_pass_manager(optimization_level=optimization_level, target=target,
                                            seed_transpiler=seed_transpiler, **pass_manager_kwargs, **extra)

    info = {"used": "stock", "vf2_stop": None, "vf2_secs": None, "piece_pack": NOT_RUN,
            "piece_pack_secs": None, "stock_2q": None, "piece_pack_2q": None}
    if pass_manager_kwargs.get("initial_layout") is not None:
        out = make_pm().run(circuit)
        info["stock_2q"] = _twoq(out)
        return out, info

    # The callback only captures the interaction graph VF2Layout saw (cheap); the search runs after
    # the stock compile finishes.
    captured = {}

    def capture(pass_, dag=None, time=None, property_set=None, **kw):
        if type(pass_).__name__ == "VF2Layout" and "stop" not in captured:
            captured["stop"] = _reason_name(property_set.get("VF2Layout_stop_reason"))
            captured["secs"] = time
            if captured["stop"] == "NO_SOLUTION_FOUND":
                captured["pattern"] = interaction_graph(dag)
                captured["num_virtual"] = dag.num_qubits()

    t0 = time.time()
    out = make_pm().run(circuit, callback=capture)
    info.update(vf2_stop=captured.get("stop"), vf2_secs=captured.get("secs"), stock_2q=_twoq(out),
                stock_secs=round(time.time() - t0, 3))
    if captured.get("stop") != "NO_SOLUTION_FOUND" or captured.get("pattern") is None:
        return out, info

    chip = chip_graph(target)
    if chip is None or captured["num_virtual"] > chip.num_nodes():
        info["piece_pack"] = UNSUPPORTED
        return out, info
    mapping, status, stats = piece_pack(chip, captured["pattern"], time_limit=time_limit)
    info.update(piece_pack=status, piece_pack_secs=stats.get("secs"), pieces=stats.get("pieces"))
    if status != FOUND:
        return out, info
    layout = full_layout(mapping, captured["num_virtual"], chip.num_nodes())
    out2 = make_pm(initial_layout=layout).run(circuit)
    info["piece_pack_2q"] = _twoq(out2)
    if info["piece_pack_2q"] < info["stock_2q"]:
        info["used"] = "piece-pack"
        return out2, info
    return out, info
