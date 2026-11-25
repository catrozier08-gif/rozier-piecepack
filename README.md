# rozier-piecepack

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.24543880.svg)](https://doi.org/10.5281/zenodo.24543880)

A fallback layout search for Qiskit. It finds zero-swap layouts that the stock `VF2Layout` pass misses when a circuit's two-qubit interaction graph is split into several medium-sized pieces and nearly every qubit on the chip is in use.

Background and data: [Qiskit issue #17609](https://github.com/Qiskit/qiskit/issues/17609).

## The problem in one paragraph

On IBM's 156-qubit heavy-hex (Heron r2) graph, `transpile(optimization_level=3)` sometimes reports `VF2Layout` `NO_SOLUTION_FOUND` for circuits that fit the chip with zero swaps. It then falls back to SabreLayout. The miss costs 17-36 s of VF2 search. With the chip's own qubit numbering, Sabre usually still lands on a zero-swap layout, so only time is lost. With any other numbering, the result can have ~2.4x the two-qubit gates. It shows up for ordinary circuits. One example is Ising Trotter steps on a fixed subset of ~70-85% of the couplers, which is near heavy-hex's bond-percolation point: almost every qubit is active, but the pattern breaks into several medium pieces.

## How piece-pack works

VF2 grows a match one qubit at a time and prunes by checking each new qubit against qubits already placed. A piece that isn't connected to anything placed yet has nothing to be checked against. A bad spot for it is often only found when the chip is nearly full, and the search backtracks a very long way.

Piece-pack places whole pieces instead:

1. Pieces go biggest first, each one placed with rustworkx VF2 on the chip's still-free qubits.
2. After every placement it checks a cheap necessary condition: can the leftover free regions still hold the leftover pieces? For each size *t*, the pieces of size ≥ *t* must fit into the sum over regions of ⌊region / *t*⌋ slots. If the condition fails, the placement is rejected at once. This is forward checking in the constraint-satisfaction sense.
3. Placements covering exactly the same qubits as one already tried are skipped.

`transpile_with_fallback` runs stock Qiskit first. Piece-pack runs only if stock `VF2Layout` returns `NO_SOLUTION_FOUND`, and it searches the exact interaction graph that `VF2Layout` saw. When VF2 succeeds, nothing extra runs. When piece-pack finds a layout, the circuit is compiled again with it, and the version with fewer two-qubit gates is returned.

## Install

```
pip install git+https://github.com/catrozier08-gif/rozier-piecepack     # until it is on PyPI
pip install "rozier-piecepack[qiskit]"                                   # once on PyPI
```

## Use

```python
from qiskit_ibm_runtime.fake_provider import FakeKingston
from rozier_piecepack import transpile_with_fallback

target = FakeKingston().target
out, info = transpile_with_fallback(circuit, target, optimization_level=3, seed_transpiler=1, time_limit=10)
print(info["vf2_stop"], info["piece_pack"], info["used"], info["stock_2q"], info["piece_pack_2q"])
```

In a custom pass manager, `PiecePackLayout(target=..., time_limit=...)` is an analysis pass that sets `property_set["layout"]` when it finds a zero-swap layout. By default it does nothing if a layout is already set. Its outcome is recorded in `property_set["PiecePackLayout_stop_reason"]`.

The search alone needs only rustworkx:

```python
from rozier_piecepack import piece_pack, check_mapping
mapping, status, stats = piece_pack(chip_graph, pattern_graph, time_limit=10)   # status: FOUND / TIMEOUT / NONE
```

## Results

Run with `examples/boundary_benchmark.py` (this package, v0.1.0). Setup: Qiskit 2.6.2, rustworkx 0.17.1, FakeKingston, 30 s limit. The 16 circuits are the ones where stock level-3 `VF2Layout` returned `NO_SOLUTION_FOUND`, out of 48 Ising Trotter circuits on 65-90% of the couplers.

| | stock level 3 | with fallback | zero-swap |
|---|---|---|---|
| zero-swap layout found | 0/16 | **13/16** | 16/16 by construction |
| 2q gates, all 16 | 14780 | **8738** | 8738 |
| 2q gates, 8 with shuffled numbering | 10394 | **4352** | 4352 |

- **Every miss that cost gates was fixed.** All 8 shuffled-numbering circuits were brought to their zero-swap count.
- **Piece-pack time: median 0.45 s, max 14.4 s.** Stock `VF2Layout` had already spent a median 28.8 s before giving up.
- **The 3 not solved are all chip-order circuits.** Stock's Sabre fallback already reaches the zero-swap gate count on those, so they cost time but not gates. One of them is the many-small-pieces case below.
- 0 invalid layouts. The package's own tests check every returned layout edge by edge.

## Limits (read before relying on it)

- **Tested on heavy-hex only:** FakeKingston (Heron r2, 156 q) and synthetic `CouplingMap.from_heavy_hex` graphs. Other topologies are untested.
- **First valid layout, not error-scored.** Stock `VF2Layout` scores candidate layouts by error rates; piece-pack returns the first zero-swap layout it finds. Noise-aware choice is planned.
- **The time limit is approximate.** Each VF2 call gets a budget sized to the time left, assuming about 1e6 VF2 states per second.
- **`NONE` is not a proof** that no layout exists, only that none was found within the budget.
- **Many small pieces are a weak spot.** With 25 pieces and the largest only 17 qubits, piece-pack needed 14 s on one circuit and timed out on another. Whole-chip VF2 or a round-trip `VF2Layout` retry can do better there; adding those in front of piece-pack is planned for v0.2.
- **Some cases are still out of reach.** Two more circuits stayed unsolved at 30 s (pieces 49 28 16 11 … and 51 25 20 17 …).
- **Default time limit is 30 s.** A search that succeeds returns early, so the limit only matters on the hard cases, and stock `VF2Layout` has usually already spent 20-40 s on those.
- **On a miss, it costs one extra compile** on top of the stock run, plus the search time.

## Related work

- VF2 (Cordella, Foggia, Sansone, Vento, 2004) and VF2++ (Jüttner & Madarasi, 2018), as used by rustworkx.
- Heavy-tailed run times in backtracking search, with restarts (Gomes, Selman, Crato, Kautz, 2000; Luby, Sinclair, Zuckerman, 1993).
- Forward checking in constraint satisfaction (Haralick & Elliott, 1980).
- Bond percolation on lattices: the coupler fraction where heavy-hex breaks into pieces.

## License and citation

Apache-2.0. If this helps your work, please cite it: [doi:10.5281/zenodo.24543880](https://doi.org/10.5281/zenodo.24543880) (details in `CITATION.cff`).

Built by Chris Rozier, Rozier Quantum LLC — [rozierquantum.com](https://rozierquantum.com).
