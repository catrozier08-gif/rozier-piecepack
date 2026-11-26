import random

import pytest
import rustworkx as rx

from rozier_piecepack import FOUND, NONE, check_mapping, fits, piece_pack


def heavy_hex(d):
    hh = rx.generators.heavy_hex_graph(d)
    g = rx.PyGraph()
    g.add_nodes_from(list(range(hh.num_nodes())))
    g.add_edges_from_no_data(sorted({tuple(sorted(e)) for e in hh.edge_list()}))
    return g


def planted(chip, q, seed):
    """Random subset of the chip's edges (each kept with probability q), qubits relabelled at random."""
    r = random.Random(seed)
    edges = [e for e in sorted(chip.edge_list()) if r.random() < q]
    active = sorted({v for e in edges for v in e})
    labels = list(range(len(active)))
    r.shuffle(labels)
    lab = dict(zip(active, labels))
    p = rx.PyGraph()
    nodes = sorted(labels)
    p.add_nodes_from(nodes)
    pos = {x: i for i, x in enumerate(nodes)}
    p.add_edges_from_no_data([(pos[lab[a]], pos[lab[b]]) for a, b in edges])
    return p


def test_fits_basic():
    assert fits([10], [4, 3, 3])
    assert not fits([10], [6, 5])          # 11 > 10
    assert not fits([5, 5], [6])           # no region big enough
    assert fits([5, 5], [5, 5])
    assert not fits([9], [5, 5])           # total too big
    assert not fits([7, 3], [4, 4])        # only one region holds a 4-piece
    assert fits([], [])


@pytest.mark.parametrize("q", [0.6, 0.75, 0.8, 0.85, 1.0])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_planted_found_and_valid(q, seed):
    chip = heavy_hex(5)
    pattern = planted(chip, q, seed)
    mapping, status, stats = piece_pack(chip, pattern, time_limit=20)
    assert status == FOUND, stats
    assert check_mapping(chip, pattern, mapping)


def test_degree_too_high_is_none():
    chip = heavy_hex(3)                    # heavy-hex: max degree 3
    star = rx.PyGraph()
    star.add_nodes_from([0, 1, 2, 3, 4])
    star.add_edges_from_no_data([(0, 1), (0, 2), (0, 3), (0, 4)])
    mapping, status, _ = piece_pack(chip, star, time_limit=5)
    assert mapping is None and status == NONE


def test_too_big_is_none():
    chip = heavy_hex(3)
    big = rx.generators.path_graph(chip.num_nodes() + 1)
    p = rx.PyGraph()
    p.add_nodes_from(list(range(big.num_nodes())))
    p.add_edges_from_no_data(big.edge_list())
    mapping, status, _ = piece_pack(chip, p, time_limit=5)
    assert mapping is None and status == NONE


def test_empty_pattern():
    mapping, status, _ = piece_pack(heavy_hex(3), rx.PyGraph(), time_limit=1)
    assert status == FOUND and mapping == {}


def test_check_mapping_rejects_bad():
    chip = heavy_hex(3)
    pattern = planted(chip, 1.0, 1)
    mapping, status, _ = piece_pack(chip, pattern, time_limit=10)
    assert status == FOUND
    a, b = list(mapping)[:2]
    broken = dict(mapping)
    broken[a] = mapping[b]                 # two logical qubits on one physical qubit
    assert not check_mapping(chip, pattern, broken)


@pytest.mark.parametrize("order", [False, True])
def test_whole_chip_vf2_planted(order):
    from rozier_piecepack import whole_chip_vf2
    chip = heavy_hex(5)
    pattern = planted(chip, 0.8, 4)
    mapping, status, stats = whole_chip_vf2(chip, pattern, time_limit=10, id_order=order)
    assert status == FOUND, stats
    assert check_mapping(chip, pattern, mapping)


def test_whole_chip_vf2_scored_is_valid_and_best_seen():
    from rozier_piecepack import whole_chip_vf2
    chip = heavy_hex(5)
    pattern = planted(chip, 0.8, 5)
    score = lambda m: sum(m.values())          # any deterministic score
    mapping, status, stats = whole_chip_vf2(chip, pattern, time_limit=10, score=score, max_maps=20)
    assert status == FOUND and check_mapping(chip, pattern, mapping)
    assert stats["maps_scored"] >= 1 and stats["score"] == score(mapping)


def test_noncontiguous_chip_payloads():
    """Chip node payloads that aren't 0..N-1 come back as the payloads, not internal indices."""
    base = heavy_hex(3)
    chip = rx.PyGraph()
    chip.add_nodes_from([100 + i for i in range(base.num_nodes())])
    chip.add_edges_from_no_data(base.edge_list())
    pattern = planted(base, 0.8, 6)
    mapping, status, _ = piece_pack(chip, pattern, time_limit=10)
    assert status == FOUND
    assert all(p >= 100 for p in mapping.values())
    assert check_mapping(chip, pattern, mapping)


# ---------- symmetry breaking (identical pieces) ----------

def disjoint_edges(k):
    """Pattern of k separate 2-qubit pieces (all identical)."""
    p = rx.PyGraph()
    p.add_nodes_from(list(range(2 * k)))
    p.add_edges_from_no_data([(2 * i, 2 * i + 1) for i in range(k)])
    return p


def path_chip(n):
    g = rx.PyGraph()
    g.add_nodes_from(list(range(n)))
    g.add_edges_from_no_data([(i, i + 1) for i in range(n - 1)])
    return g


@pytest.mark.parametrize("symmetry", [False, True])
def test_exact_fit_perfect_matching(symmetry):
    """8 identical edges on a 16-qubit path: only ONE set of positions works (a perfect matching).
    Symmetry breaking must still find it."""
    chip, pattern = path_chip(16), disjoint_edges(8)
    mapping, status, stats = piece_pack(chip, pattern, time_limit=20, symmetry=symmetry)
    assert status == FOUND, stats
    assert check_mapping(chip, pattern, mapping)


def test_symmetry_reports_identical_pieces():
    chip, pattern = path_chip(16), disjoint_edges(8)
    _, status, stats = piece_pack(chip, pattern, time_limit=20, symmetry=True)
    assert status == FOUND and stats["identical_pieces"] == 7


def max_pairs(chip):
    """Most separate 2-qubit pieces the chip can hold = size of a maximum matching (independent check)."""
    return len(rx.max_weight_matching(chip, max_cardinality=True, weight_fn=lambda e: 1))


def test_symmetry_answer_matches_ground_truth():
    """k separate pairs fit exactly when k <= maximum matching size. Symmetry ON must give exactly that
    answer (FOUND or NONE, never a wrong one); OFF must agree or run out of time (it is slower, not wrong)."""
    cases = []
    for chip in (path_chip(9), path_chip(7), heavy_hex(3)):
        m = max_pairs(chip)
        cases += [(chip, m, FOUND), (chip, m + 1, NONE)]
    for chip, k, truth in cases:
        on = piece_pack(chip, disjoint_edges(k), time_limit=30, symmetry=True)
        assert on[1] == truth, (k, truth, on[2])
        if on[1] == FOUND:
            assert check_mapping(chip, disjoint_edges(k), on[0])
        off = piece_pack(chip, disjoint_edges(k), time_limit=10, symmetry=False)[1]
        assert off in (truth, "TIMEOUT"), (k, truth, off)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_many_small_pieces_planted(seed):
    """Sparse planted pattern on heavy-hex (lots of small, often identical pieces) must be found and valid."""
    chip = heavy_hex(5)
    pattern = planted(chip, 0.45, seed)
    mapping, status, stats = piece_pack(chip, pattern, time_limit=20, symmetry=True)
    assert status == FOUND, stats
    assert check_mapping(chip, pattern, mapping)


# ---------- work budget (machine-independent) ----------

def test_work_budget_is_deterministic():
    """Same pattern, same chip, same work budget -> same answer and same amount of work, every run."""
    chip = heavy_hex(5)
    pattern = planted(chip, 0.8, 2)
    runs = [piece_pack(chip, pattern, time_limit=60, max_placements=20_000, vf2_calls=1_000_000) for _ in range(2)]
    (m1, s1, st1), (m2, s2, st2) = runs
    assert s1 == s2 == FOUND
    assert m1 == m2
    assert st1["placements_tried"] == st2["placements_tried"]
    assert check_mapping(chip, pattern, m1)


def test_work_budget_runs_out_as_budget_not_timeout():
    from rozier_piecepack import BUDGET
    chip = heavy_hex(3)
    k = max_pairs(chip) + 1                     # impossible: forces a long search
    _, status, st = piece_pack(chip, disjoint_edges(k), time_limit=60, symmetry=False, max_placements=50)
    assert status == BUDGET and st["placements_tried"] == 51
