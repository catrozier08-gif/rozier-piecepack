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
BUDGET = "BUDGET"  # the work budget (placements) ran out - like TIMEOUT, but the same on every machine


class _Timeout(Exception):
    pass


class _Budget(Exception):
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


def _indexed(chip):
    """Copy of `chip` whose node payloads are 0..N-1, plus a map back to the original payloads."""
    work = rx.PyGraph()
    work.add_nodes_from(list(range(chip.num_nodes())))
    remap = {n: i for i, n in enumerate(chip.node_indices())}
    work.add_edges_from_no_data(sorted({tuple(sorted((remap[a], remap[b]))) for a, b in chip.edge_list() if a != b}))
    back = {i: chip[n] for n, i in remap.items()}
    return work, back


def whole_chip_vf2(chip, pattern, time_limit=2.0, id_order=False, score=None, max_maps=50,
                   calls_per_sec=1_000_000, min_calls=10_000, vf2_calls=None):
    """Plain rustworkx VF2 of the whole pattern onto the whole chip, with a call budget sized to time_limit.

    id_order=False uses the VF2++ node order; True uses node-index order. The two often succeed on
    different instances. If `score` is given (mapping -> number, lower is better), up to `max_maps`
    mappings are scored and the best is returned; otherwise the first one.
    vf2_calls: a fixed VF2 call budget (same result on every machine); time_limit then only acts as a
    safety cap. Without it the budget is sized from time_limit.

    Returns (mapping or None, status, stats) like piece_pack.
    """
    t0 = time.time()
    deadline = t0 + time_limit
    work, back = _indexed(chip)
    budget = int(vf2_calls) if vf2_calls else max(min_calls, int(time_limit * calls_per_sec))
    best, best_s, seen = None, None, 0
    for m in rx.vf2_mapping(work, pattern, subgraph=True, induced=False, id_order=id_order, call_limit=budget):
        mapping = {pattern[ci]: back[hi] for hi, ci in m.items()}
        seen += 1
        if score is None:
            best = mapping
            break
        s = score(mapping)
        if best is None or s < best_s:
            best, best_s = mapping, s
        if seen >= max_maps or time.time() > deadline:
            break
    secs = round(time.time() - t0, 3)
    if best is not None:
        status = FOUND
    else:
        status = TIMEOUT if time.time() > deadline else NONE
    return best, status, {"maps_scored": seen, "score": best_s, "secs": secs}


def _shape_key(g):
    """Cheap invariant: graphs with different keys are never isomorphic."""
    return (g.num_nodes(), g.num_edges(), tuple(sorted((g.degree(n) for n in g.node_indices()), reverse=True)))


def piece_pack(chip, pattern, time_limit=10.0, calls_per_sec=1_000_000, min_calls=10_000, symmetry=True,
               sort_cap=5000, max_placements=None, vf2_calls=None):
    """Find a placement of `pattern` onto `chip` with every pattern edge on a chip edge.

    chip:     rx.PyGraph. Each node's payload is that node's physical id (e.g. the qubit index).
    pattern:  rx.PyGraph. Each node's payload is the logical id (e.g. the circuit's qubit index).
              Only include qubits that take part in two-qubit gates.
    time_limit: seconds. Each VF2 search is given a call budget sized to the time left
              (calls_per_sec is a rough speed estimate), so the limit holds approximately even inside
              one long VF2 call.
    symmetry: identical pieces (isomorphic components) are interchangeable, so trying piece A here and
              piece B there, then B here and A there, repeats the same layout. With symmetry=True,
              identical pieces are placed next to each other in the order and each one must take a
              placement that sorts after the previous one's (symmetry breaking, as in constraint
              solving). No solution is lost: any layout can be reordered to satisfy this.
              Placements of identical pieces are tried in sorted order (up to sort_cap of them), so the
              search order agrees with that rule instead of fighting it.
              symmetry=False keeps the v0.2 behaviour exactly (for comparisons).

    max_placements / vf2_calls: a WORK budget instead of a time budget - at most this many placements
              tried, and each VF2 search capped at this many calls. The same pattern on the same chip
              then gives the same answer on any machine, busy or idle; time_limit only acts as a safety
              cap. Without them the budget is time-based (v0.2 behaviour).

    Returns (mapping or None, status, stats), where mapping is {pattern payload: chip payload} and
    status is FOUND, TIMEOUT, BUDGET or NONE.
    """
    t0 = time.time()
    deadline = t0 + time_limit
    work, back = _indexed(chip)   # copy of the chip whose payloads are its own node indices
    N = work.num_nodes()

    comps = [sorted(c) for c in rx.connected_components(pattern)]
    if symmetry:
        graphs = [pattern.subgraph(c) for c in comps]
        order = sorted(range(len(comps)), key=lambda i: (-len(comps[i]), _shape_key(graphs[i]), comps[i]))
        comps = [comps[i] for i in order]
        pieces = [graphs[i] for i in order]
    else:
        comps = sorted(comps, key=len, reverse=True)
        pieces = [pattern.subgraph(c) for c in comps]
    sizes = [len(c) for c in comps]

    # same_as_prev[i]: piece i is isomorphic to piece i-1 (only checked when symmetry is on)
    same_as_prev = [False] * len(pieces)
    if symmetry:
        for i in range(1, len(pieces)):
            if _shape_key(pieces[i]) == _shape_key(pieces[i - 1]):
                same_as_prev[i] = rx.is_isomorphic(pieces[i], pieces[i - 1])
    last_key = [None] * len(pieces)
    # pieces that belong to a group of identical pieces: their placements are tried in sorted order, so
    # the search order agrees with the symmetry rule instead of fighting it
    in_group = [same_as_prev[i] or (i + 1 < len(pieces) and same_as_prev[i + 1]) for i in range(len(pieces))]

    def _sorted_placements(maps, P, F, cap=sort_cap):
        """Distinct placements of P, sorted by their qubit set. Falls back to VF2's own order (the ones
        collected so far, sorted, then the rest unsorted) if there are more than `cap`."""
        got, keys = [], set()
        it = iter(maps)
        for m in it:
            if time.time() > deadline:
                raise _Timeout()
            place = {P[ci]: F[hi] for hi, ci in m.items()}
            k = frozenset(place.values())
            if k in keys:
                continue
            keys.add(k)
            got.append((tuple(sorted(k)), place))
            if len(got) >= cap:
                got.sort(key=lambda t: t[0])
                yield from (p for _, p in got)
                for m2 in it:
                    yield {P[ci]: F[hi] for hi, ci in m2.items()}
                return
        got.sort(key=lambda t: t[0])
        yield from (p for _, p in got)

    used, assign = set(), {}
    stats = {"pieces": sizes, "placements_tried": 0, "closure_rejects": 0, "backtracks": 0,
             "symmetry_skips": 0, "identical_pieces": sum(same_as_prev)}

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
        budget = int(vf2_calls) if vf2_calls else max(min_calls, int((deadline - time.time()) * calls_per_sec))
        seen = set()
        floor = last_key[i - 1] if same_as_prev[i] else None
        maps = rx.vf2_mapping(F, P, subgraph=True, induced=False, id_order=False, call_limit=budget)
        if in_group[i]:
            maps = _sorted_placements(maps, P, F)
        for m in maps:
            if time.time() > deadline:
                raise _Timeout()
            place = {P[ci]: F[hi] for hi, ci in m.items()} if not isinstance(m, dict) else m
            key = frozenset(place.values())
            if key in seen:
                continue
            seen.add(key)
            skey = tuple(sorted(key))
            if floor is not None and skey <= floor:
                stats["symmetry_skips"] += 1
                continue
            stats["placements_tried"] += 1
            if max_placements and stats["placements_tried"] > max_placements:
                raise _Budget()
            if not fits(free_regions(key), sizes[i + 1:]):
                stats["closure_rejects"] += 1
                continue
            used.update(key)
            assign.update(place)
            last_key[i] = skey
            if rec(i + 1):
                return True
            used.difference_update(key)
            for k in place:
                assign.pop(k, None)
            last_key[i] = None
            stats["backtracks"] += 1
        return False

    try:
        if not fits(free_regions(), sizes):
            status = NONE
        else:
            status = FOUND if rec(0) else NONE
    except _Timeout:
        status = TIMEOUT
    except _Budget:
        status = BUDGET
    stats["secs"] = round(time.time() - t0, 3)
    if status != FOUND:
        return None, status, stats
    return {logical: back[phys] for logical, phys in assign.items()}, status, stats
