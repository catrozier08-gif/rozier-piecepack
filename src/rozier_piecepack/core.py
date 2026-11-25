"""
Piece-pack: zero-swap layout search for interaction graphs that are split into several pieces.

Plain VF2 extends a partial match one node at a time and prunes by checking new nodes against nodes
that are already placed. When the pattern (the circuit's two-qubit interaction graph) falls apart into
several separate pieces, a newly started piece has nothing placed to check against, so a bad spot is
often only discovered when the chip is nearly full - and the search backtracks a very long way.

Piece-pack places whole pieces instead:
  1. pieces are placed biggest first, each one with rustworkx VF2 on the chip's still-free qubits
  2. after every placement, a cheap necessary condition is checked: can the leftover free regions of
     the chip still hold the leftover pieces? (for every size t, the pieces of size >= t must fit in
     the sum over free regions of floor(region size / t) slots)
  3. if not, that placement is rejected right away instead of stacking more work on a bad spot
  4. placements that cover exactly the same qubits as one already tried are skipped

Step 2 is a form of forward checking (constraint satisfaction): reject a partial assignment as soon as
the remaining variables provably can't all be satisfied, rather than discovering it deep in the search.

Only rustworkx is required here; the Qiskit integration lives in rozier_piecepack.qiskit_tools.
"""
import time

import rustworkx as rx

FOUND = "FOUND"
TIMEOUT = "TIMEOUT"
NONE = "NONE"  # no layout found within the search budget - NOT a proof that none exists


class _Timeout(Exception):
    pass


def fits(region_sizes, piece_sizes):
    """Necessary condition: can pieces of these sizes still go into free regions of these sizes?

    For every size t among the pieces, the pieces of size >= t each need their own slot of at least
    t qubits, and a region of size r holds at most floor(r / t) such slots. Passing this check does
    not guarantee a fit (shape matters too); failing it guarantees there is none.
    """
    if sum(piece_sizes) > sum(region_sizes):
        return False
    for t in sorted(set(piece_sizes)):
        need = sum(1 for s in piece_sizes if s >= t)
        room = sum(r // t for r in region_sizes)
        if need > room:
            return False
    return True


def check_mapping(chip, pattern, mapping):
    """True if `mapping` {pattern payload: chip payload} is one-to-one and puts every pattern edge on a chip edge."""
    if mapping is None or len(set(mapping.values())) != len(mapping):
        return False
    chip_index = {chip[n]: n for n in chip.node_indices()}
    for a, b in pattern.edge_list():
        pa, pb = pattern[a], pattern[b]
        if pa not in mapping or pb not in mapping:
            return False
        ca, cb = chip_index.get(mapping[pa]), chip_index.get(mapping[pb])
        if ca is None or cb is None or not chip.has_edge(ca, cb):
            return False
    return True


def piece_pack(chip, pattern, time_limit=10.0, calls_per_sec=1_000_000, min_calls=10_000):
    """Find a placement of `pattern` onto `chip` with every pattern edge on a chip edge.

    chip:     rx.PyGraph. Each node's payload is that node's physical id (e.g. the qubit index).
    pattern:  rx.PyGraph. Each node's payload is the logical id (e.g. the circuit's qubit index).
              Only include qubits that take part in two-qubit gates.
    time_limit: seconds. Each VF2 search is given a call budget sized to the time left
              (calls_per_sec is a rough speed estimate), so the limit holds approximately even inside
              one long VF2 call.

    Returns (mapping or None, status, stats), where mapping is {pattern payload: chip payload} and
    status is FOUND, TIMEOUT or NONE.
    """
    t0 = time.time()
    deadline = t0 + time_limit
    # work on a copy of the chip whose payloads are its own node indices
    work = rx.PyGraph()
    work.add_nodes_from(list(chip.node_indices()))
    remap = {n: i for i, n in enumerate(chip.node_indices())}
    work.add_edges_from_no_data(sorted({tuple(sorted((remap[a], remap[b]))) for a, b in chip.edge_list() if a != b}))
    back = {i: chip[n] for n, i in remap.items()}
    N = work.num_nodes()

    comps = sorted((sorted(c) for c in rx.connected_components(pattern)), key=len, reverse=True)
    pieces = [pattern.subgraph(c) for c in comps]
    sizes = [len(c) for c in comps]
    used, assign = set(), {}
    stats = {"pieces": sizes, "placements_tried": 0, "closure_rejects": 0, "backtracks": 0}

    def free_regions(extra=()):
        free = [q for q in range(N) if q not in used and q not in extra]
        return [len(c) for c in rx.connected_components(work.subgraph(free))]

    def rec(i):
        if time.time() > deadline:
            raise _Timeout()
        if i == len(pieces):
            return True
        P = pieces[i]
        free = [q for q in range(N) if q not in used]
        F = work.subgraph(free)
        budget = max(min_calls, int((deadline - time.time()) * calls_per_sec))
        seen = set()
        for m in rx.vf2_mapping(F, P, subgraph=True, induced=False, id_order=False, call_limit=budget):
            if time.time() > deadline:
                raise _Timeout()
            place = {P[ci]: F[hi] for hi, ci in m.items()}
            key = frozenset(place.values())
            if key in seen:
                continue
            seen.add(key)
            stats["placements_tried"] += 1
            if not fits(free_regions(key), sizes[i + 1:]):
                stats["closure_rejects"] += 1
                continue
            used.update(key)
            assign.update(place)
            if rec(i + 1):
                return True
            used.difference_update(key)
            for k in place:
                assign.pop(k, None)
            stats["backtracks"] += 1
        return False

    try:
        if not fits(free_regions(), sizes):
            status = NONE
        else:
            status = FOUND if rec(0) else NONE
    except _Timeout:
        status = TIMEOUT
    stats["secs"] = round(time.time() - t0, 3)
    if status != FOUND:
        return None, status, stats
    return {logical: back[phys] for logical, phys in assign.items()}, status, stats
