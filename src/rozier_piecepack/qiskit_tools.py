"""
Qiskit integration for piece-pack.

transpile_with_fallback(): runs the stock preset pass manager. If its VF2Layout pass reports
NO_SOLUTION_FOUND, the stock run is stopped right there (early_exit=True, the default) - the rest of a
stock run after a miss is Sabre plus, on chip-numbered circuits, a VF2PostLayout search that can take as
long as the VF2Layout miss itself. Then search_layout() tries, on the exact interaction graph VF2Layout saw:

  0. a quick piece-pack try (2 s; with symmetry breaking most piece-pack wins take under 2 s)
  1. a round-trip VF2Layout retry (circuit -> DAG again gives Qiskit's canonical edge order), scored by
     Qiskit's own error heuristic
  2. whole-chip rustworkx VF2 in VF2++ order, best of up to 50 layouts by error
  3. whole-chip rustworkx VF2 in node-index order, same
  4. piece-pack (components biggest first, closure check) with all the time that is left

Stages 1-3 each get a fixed few seconds (stage_seconds); they cover the many-small-components case where
piece-pack is weakest. If a zero-swap layout is found, the circuit is compiled with it as initial_layout.
If nothing is found, the circuit is compiled with layout_method="sabre" - the same layout method stock
falls back to - so the result matches what stock would have produced, without paying for VF2Layout twice.
On the happy path (VF2Layout succeeds) nothing extra runs.

PiecePackLayout: an AnalysisPass running search_layout() for custom pass managers.
"""
import time

import rustworkx as rx
from qiskit.transpiler.basepasses import AnalysisPass
from qiskit.transpiler.layout import Layout

from .core import BUDGET, FOUND, NONE, TIMEOUT, check_mapping, piece_pack, whole_chip_vf2

# Work budget (the default): the same circuit gets the same answer on any machine, busy or idle.
# Sized from the FakeKingston boundary set: the hardest solved case used ~170k placements; quick try
# catches most solves (most use under 20k placements). Seconds are kept only as a safety cap.
DEFAULT_BUDGET = {"quick_placements": 20_000,   # stage 0: quick piece-pack try
                  "stage_calls": 2_000_000,     # stages 1-3: VF2 calls each
                  "placements": 300_000,        # stage 4: piece-pack
                  "vf2_calls": 10_000_000}      # cap for each single VF2 search inside piece-pack
SAFETY_SECONDS = 120.0


def _resolve(budget, time_limit):
    """budget: "default" / dict / None (None = old time-based search). Returns (budget dict or None, seconds)."""
    if isinstance(budget, str):
        if budget != "default":
            raise ValueError('budget must be "default", a dict, or None')
        budget = DEFAULT_BUDGET
    if budget is not None:
        budget = {**DEFAULT_BUDGET, **budget}
    return budget, (time_limit if time_limit is not None else (SAFETY_SECONDS if budget else 30.0))

UNSUPPORTED = "UNSUPPORTED"   # gates on 3+ qubits / control flow, no connectivity, or too many qubits
NOT_RUN = "NOT_RUN"


class _StopAfterVF2Miss(Exception):
    """Raised from the callback to stop the stock run right after VF2Layout misses."""


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
    used = set(mapping.values())
    free = [p for p in range(num_physical) if p not in used]
    return [mapping[v] if v in mapping else free.pop(0) for v in range(num_virtual)]


def error_maps(target):
    """({(a, b): lowest 2q error}, {q: measure error}) from a Target; empty dicts if it has no error data."""
    edge_err, q_err = {}, {}
    if target is None or not hasattr(target, "operation_names"):
        return edge_err, q_err
    for name in target.operation_names:
        try:
            props = target[name]
        except Exception:
            continue
        for qargs, p in (props or {}).items():
            if qargs is None or p is None or getattr(p, "error", None) is None:
                continue
            if len(qargs) == 2:
                e = (min(qargs), max(qargs))
                edge_err[e] = min(edge_err.get(e, 1.0), p.error)
            elif len(qargs) == 1 and name == "measure":
                q_err[qargs[0]] = p.error
    return edge_err, q_err


def _scorer(pattern, target):
    edge_err, q_err = error_maps(target)
    if not edge_err and not q_err:
        return None
    edges = [(pattern[a], pattern[b]) for a, b in pattern.edge_list()]

    def score(m):
        s = sum(edge_err.get((min(m[a], m[b]), max(m[a], m[b])), 0.0) for a, b in edges)
        return s + sum(q_err.get(p, 0.0) for p in m.values())
    return score


def _roundtrip_vf2layout(circuit, device, seconds, calls=None):
    """Qiskit's VF2Layout on a fresh DAG built from `circuit` (canonical edge order). {virtual: physical} or None.
    With `calls` the budget is a fixed call count (machine-independent) and `seconds` is not used."""
    from qiskit.transpiler import PassManager
    from qiskit.transpiler.passes import VF2Layout
    kw = {"target": device} if hasattr(device, "build_coupling_map") else {"coupling_map": device}
    if calls:
        pm = PassManager([VF2Layout(seed=-1, call_limit=(int(calls), 100_000), **kw)])
    else:
        calls = int(max(100_000, 0.8 * seconds * 1_000_000))
        try:
            pm = PassManager([VF2Layout(seed=-1, call_limit=(calls, 100_000), time_limit=seconds, **kw)])
        except TypeError:            # older Qiskit without time_limit
            pm = PassManager([VF2Layout(seed=-1, call_limit=(calls, 100_000), **kw)])
    pm.run(circuit)
    lay = pm.property_set["layout"]
    if lay is None:
        return None
    out = {}
    for q, p in lay.get_virtual_bits().items():
        try:
            out[circuit.find_bit(q).index] = p
        except Exception:
            pass
    return out


def search_layout(pattern, chip, device=None, circuit=None, time_limit=None, stage_seconds=2.0, quick_seconds=2.0,
                  budget="default"):
    """Staged zero-swap layout search on an interaction graph.

    Order: a quick piece-pack try (quick_seconds; most piece-pack wins take well under a second), then
    round-trip VF2Layout, whole-chip VF2++ and whole-chip id order (stage_seconds each), then piece-pack
    again with all the time left.

    budget="default" (or a dict overriding DEFAULT_BUDGET keys): each stage gets a fixed amount of WORK, so
    the result is the same on any machine; time_limit (default 120 s) is only a safety cap.
    budget=None: the v0.2 time-based search (time_limit default 30 s, stage_seconds / quick_seconds slices).

    pattern: rx.PyGraph from interaction_graph(); chip: rx.PyGraph from chip_graph().
    device: Target or CouplingMap (needed for the round-trip VF2Layout stage and for error scoring).
    circuit: the circuit as VF2Layout saw it (needed for the round-trip stage; skipped if None).
    Returns (mapping {virtual: physical} or None, info dict with "stage", "status", per-stage "log").
    """
    budget, time_limit = _resolve(budget, time_limit)
    t0 = time.time()
    deadline = t0 + time_limit
    target = device if hasattr(device, "operation_names") else None
    score = _scorer(pattern, target)
    log = []

    def left():
        return deadline - time.time()

    active = [pattern[n] for n in pattern.node_indices()]

    def accept(m, stage, extra=None):
        if m is not None and check_mapping(chip, pattern, {k: m[k] for k in active if k in m}):
            info = {"stage": stage, "status": FOUND, "secs": round(time.time() - t0, 3), "log": log}
            if extra:
                info.update(extra)
            return info
        return None

    def pp(limit_s, placements):
        if budget:
            return piece_pack(chip, pattern, time_limit=left(), max_placements=placements,
                              vf2_calls=budget["vf2_calls"])
        return piece_pack(chip, pattern, time_limit=limit_s)

    # 0. quick piece-pack try
    if (budget or quick_seconds) and left() > 0.05:
        m, status, st = pp(min(quick_seconds, left()), budget and budget["quick_placements"])
        log.append(("piece-pack (quick)", status, st.get("secs")))
        got = accept(m, "piece-pack", {"pieces": st.get("pieces")})
        if got:
            return m, got

    # 1. round-trip VF2Layout
    if circuit is not None and device is not None and left() > 0.05:
        s = time.time()
        try:
            m = _roundtrip_vf2layout(circuit, device, min(stage_seconds, left()),
                                     calls=budget and budget["stage_calls"])
        except Exception as e:      # never let a stage error stop the search
            m = None
            log.append(("round-trip VF2Layout", f"error: {e}"[:80], round(time.time() - s, 3)))
        else:
            log.append(("round-trip VF2Layout", FOUND if m else NONE, round(time.time() - s, 3)))
        got = accept(m, "round-trip VF2Layout")
        if got:
            return m, got

    # 2-3. whole-chip VF2, two search orders
    for order, name in ((False, "whole-chip VF2++"), (True, "whole-chip id order")):
        if left() <= 0.05:
            break
        if budget:
            m, status, st = whole_chip_vf2(chip, pattern, time_limit=left(), id_order=order, score=score,
                                           vf2_calls=budget["stage_calls"])
        else:
            m, status, st = whole_chip_vf2(chip, pattern, time_limit=min(stage_seconds, left()), id_order=order,
                                           score=score)
        log.append((name, status, st["secs"]))
        got = accept(m, name, {"maps_scored": st["maps_scored"]})
        if got:
            return m, got

    # 4. piece-pack with the rest
    status, st = TIMEOUT, {}
    if left() > 0.05:
        m, status, st = pp(left(), budget and budget["placements"])
        log.append(("piece-pack", status, st.get("secs")))
        got = accept(m, "piece-pack", {"pieces": st.get("pieces")})
        if got:
            return m, got
    return None, {"stage": None, "status": status if status != FOUND else NONE,
                  "secs": round(time.time() - t0, 3), "log": log, "pieces": st.get("pieces")}


class PiecePackLayout(AnalysisPass):
    """Staged zero-swap layout search (round-trip VF2Layout, whole-chip VF2 x2, piece-pack).

    Args:
        target: a Target (or pass coupling_map instead).
        coupling_map: a CouplingMap, used if target is None.
        time_limit: safety cap in seconds (default 120 with the work budget, 30 with budget=None).
        budget: "default", a dict overriding DEFAULT_BUDGET, or None for the old time-based search.
        stage_seconds: seconds for each of the first three stages.
        only_if_unset: if True (default), do nothing when property_set["layout"] is already set,
            so the pass can sit after VF2Layout as a fallback.
    """

    def __init__(self, target=None, coupling_map=None, time_limit=None, stage_seconds=2.0, quick_seconds=2.0,
                 only_if_unset=True, budget="default"):
        super().__init__()
        self.target = target
        self.coupling_map = coupling_map
        self.time_limit = time_limit
        self.stage_seconds = stage_seconds
        self.quick_seconds = quick_seconds
        self.only_if_unset = only_if_unset
        self.budget = budget

    def run(self, dag):
        if self.only_if_unset and self.property_set["layout"] is not None:
            self.property_set["PiecePackLayout_stop_reason"] = NOT_RUN
            return
        from qiskit.converters import dag_to_circuit
        device = self.target if self.target is not None else self.coupling_map
        chip = chip_graph(device) if device is not None else None
        pattern = interaction_graph(dag)
        if chip is None or pattern is None or dag.num_qubits() > chip.num_nodes():
            self.property_set["PiecePackLayout_stop_reason"] = UNSUPPORTED
            return
        mapping, info = search_layout(pattern, chip, device=device, circuit=dag_to_circuit(dag),
                                      time_limit=self.time_limit, stage_seconds=self.stage_seconds,
                                      quick_seconds=self.quick_seconds, budget=self.budget)
        self.property_set["PiecePackLayout_stop_reason"] = info["status"]
        self.property_set["PiecePackLayout_stats"] = info
        if mapping is not None:
            layout = full_layout(mapping, dag.num_qubits(), chip.num_nodes())
            self.property_set["layout"] = Layout({dag.qubits[v]: p for v, p in enumerate(layout)})


def _twoq(circuit):
    return sum(1 for inst in circuit.data if len(inst.qubits) == 2)


def _reason_name(r):
    return getattr(r, "name", None if r is None else str(r))


def _is_abort(e):
    seen = set()
    while e is not None and id(e) not in seen:
        if isinstance(e, _StopAfterVF2Miss):
            return True
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return False


def transpile_with_fallback(circuit, target, optimization_level=3, seed_transpiler=None, time_limit=None,
                            stage_seconds=2.0, quick_seconds=2.0, early_exit=True, budget="default",
                            **pass_manager_kwargs):
    """Stock preset pass manager; on a VF2Layout NO_SOLUTION_FOUND, a staged zero-swap search.

    early_exit=True stops the stock run as soon as VF2Layout misses (saves Sabre + VF2PostLayout time);
    if nothing is found, the circuit is compiled with layout_method="sabre", like stock's own fallback.
    early_exit=False lets the stock run finish and keeps whichever result has fewer two-qubit gates.
    budget="default" gives the search a fixed amount of work (same answer on any machine; time_limit, default
    120 s, is only a safety cap); budget=None restores the v0.2 time-based search (time_limit default 30 s).
    Extra keyword arguments go to generate_preset_pass_manager. With initial_layout given, stock is used as-is.

    Returns (transpiled circuit, info dict). info["used"]: "stock", "fallback layout" or "sabre (stock fallback)".
    """
    from qiskit.converters import dag_to_circuit
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager

    def make_pm(**extra):
        kw = dict(pass_manager_kwargs)
        kw.update(extra)
        return generate_preset_pass_manager(optimization_level=optimization_level, target=target,
                                            seed_transpiler=seed_transpiler, **kw)

    info = {"used": "stock", "vf2_stop": None, "vf2_secs": None, "search": NOT_RUN, "stage": None,
            "search_secs": None, "stock_2q": None, "final_2q": None, "aborted_stock": False}
    if pass_manager_kwargs.get("initial_layout") is not None:
        out = make_pm().run(circuit)
        info["stock_2q"] = info["final_2q"] = _twoq(out)
        return out, info

    captured = {}

    def capture(pass_, dag=None, time=None, property_set=None, **kw):
        if type(pass_).__name__ == "VF2Layout" and "stop" not in captured:
            captured["stop"] = _reason_name(property_set.get("VF2Layout_stop_reason"))
            captured["secs"] = time
            if captured["stop"] == "NO_SOLUTION_FOUND":
                captured["pattern"] = interaction_graph(dag)
                captured["num_virtual"] = dag.num_qubits()
                captured["circuit"] = dag_to_circuit(dag)
                if early_exit:
                    raise _StopAfterVF2Miss()

    t0 = time.time()
    out = None
    try:
        out = make_pm().run(circuit, callback=capture)
    except Exception as e:
        # our own stop signal (possibly wrapped by Qiskit); anything else is a real error
        ours = _is_abort(e) or (early_exit and captured.get("stop") == "NO_SOLUTION_FOUND" and "circuit" in captured)
        if not ours:
            raise
        info["aborted_stock"] = True
    info.update(vf2_stop=captured.get("stop"), vf2_secs=captured.get("secs"), stock_secs=round(time.time() - t0, 3))
    if out is not None:
        info["stock_2q"] = info["final_2q"] = _twoq(out)
    if captured.get("stop") != "NO_SOLUTION_FOUND":
        return out, info

    chip = chip_graph(target)
    pattern = captured.get("pattern")
    mapping, sinfo = None, {"status": UNSUPPORTED, "stage": None, "secs": None, "log": []}
    if chip is not None and pattern is not None and captured["num_virtual"] <= chip.num_nodes():
        mapping, sinfo = search_layout(pattern, chip, device=target, circuit=captured["circuit"],
                                       time_limit=time_limit, stage_seconds=stage_seconds,
                                       quick_seconds=quick_seconds, budget=budget)
    info.update(search=sinfo["status"], stage=sinfo["stage"], search_secs=sinfo["secs"],
                search_log=sinfo["log"], pieces=sinfo.get("pieces"))

    if mapping is not None:
        layout = full_layout(mapping, captured["num_virtual"], chip.num_nodes())
        out2 = make_pm(initial_layout=layout).run(circuit)
        if out is None or _twoq(out2) < _twoq(out):
            info.update(used="fallback layout", final_2q=_twoq(out2))
            return out2, info
        return out, info

    if out is None:   # stock was stopped early and nothing was found: finish the way stock would have
        extra = {} if "layout_method" in pass_manager_kwargs else {"layout_method": "sabre"}
        out = make_pm(**extra).run(circuit)
        info.update(used="sabre (stock fallback)", final_2q=_twoq(out))
    return out, info
