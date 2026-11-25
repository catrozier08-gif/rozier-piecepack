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
