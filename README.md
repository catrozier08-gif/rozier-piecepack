# rozier-piecepack

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.24543880.svg)](https://doi.org/10.5281/zenodo.24543880)

A fallback layout search for Qiskit. It finds zero-swap layouts that the stock `VF2Layout` pass misses when a circuit's two-qubit interaction graph is split into several pieces and nearly every qubit on the chip is in use.

Background and data: [Qiskit issue #17609](https://github.com/Qiskit/qiskit/issues/17609).

## The problem in one paragraph

On IBM's 156-qubit heavy-hex (Heron r2) graph, `transpile(optimization_level=3)` sometimes reports `VF2Layout` `NO_SOLUTION_FOUND` for circuits that fit the chip with zero swaps. It then falls back to SabreLayout. The miss costs 17-36 s of VF2 search. With the chip's own qubit numbering, Sabre usually still lands on a zero-swap layout, so only time is lost. With any other numbering, the result can have ~2.4x the two-qubit gates. On chip-numbered misses a second pass, `VF2PostLayout`, then repeats the same hard search, roughly doubling the compile time. It shows up for ordinary circuits. One example is Ising Trotter steps on a fixed subset of ~70-85% of the couplers, which is near heavy-hex's bond-percolation point: almost every qubit is active, but the pattern breaks into several medium pieces.

## How it works

When stock `VF2Layout` reports `NO_SOLUTION_FOUND`, `transpile_with_fallback` stops the stock run at that point (early exit). That skips the Sabre fallback and the repeated `VF2PostLayout` search. It then searches the exact interaction graph that `VF2Layout` saw, in stages:

0. **Quick piece-pack try** (20,000 placements). Most solves need far fewer.
1. **Round-trip `VF2Layout` retry** (2M VF2 calls). A fresh DAG gives Qiskit's canonical edge order, and Qiskit scores the layout by error.
2. **Whole-chip VF2, VF2++ order** (2M calls). Keeps the lowest-error of up to 50 layouts.
3. **Whole-chip VF2, node-index order** (2M calls). Same scoring.
4. **Piece-pack with the full budget** (300,000 placements).

**Piece-pack** places whole pieces instead of single qubits:

- Pieces are placed biggest first, each with rustworkx VF2 on the chip's still-free qubits.
- After every placement it checks a cheap necessary condition: can the leftover free regions still hold the leftover pieces? For each size *t*, the pieces of size ≥ *t* must fit into the sum over regions of ⌊region / *t*⌋ slots. This is forward checking in the constraint-satisfaction sense.
- **Symmetry breaking for identical pieces** (new in v0.3). Pieces with the same shape are interchangeable, so trying A here and B there, then B here and A there, repeats the same layout. Identical pieces are placed next to each other, each must take a spot that sorts after the previous one's, and their spots are tried in sorted order so the search order agrees with the rule. No solution is lost: any layout can be reordered to satisfy it. This is what makes circuits with many small pieces fast.
- Placements covering exactly the same qubits as one already tried are skipped.

**Work budget, not seconds** (new in v0.3). Every stage gets a fixed amount of work: placements tried, and VF2 calls. So the same circuit gives the same answer on any machine, busy or idle. Seconds are only a safety cap (120 s). `budget=None` restores the v0.2 time-based search.

If a zero-swap layout is found, the circuit is compiled with it as `initial_layout`. If nothing is found, the circuit is compiled with `layout_method="sabre"`, the same method stock falls back to. So the result is never worse than stock, and `VF2Layout` doesn't run twice. When `VF2Layout` succeeds, nothing extra runs.

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
out, info = transpile_with_fallback(circuit, target, optimization_level=3, seed_transpiler=1)
print(info["vf2_stop"], info["search"], info["stage"], info["used"], info["final_2q"])
```

`early_exit=False` lets the stock run finish and keeps whichever result has fewer two-qubit gates. `budget={"placements": 600_000}` (any key of `DEFAULT_BUDGET`) gives the search more work.

In a custom pass manager, `PiecePackLayout(target=...)` is an analysis pass that runs the same staged search and sets `property_set["layout"]` when it finds a zero-swap layout. By default it does nothing if a layout is already set. Its outcome is recorded in `property_set["PiecePackLayout_stop_reason"]`.

The searches alone need only rustworkx:

```python
from rozier_piecepack import piece_pack, check_mapping
mapping, status, stats = piece_pack(chip_graph, pattern_graph, max_placements=300_000, vf2_calls=10_000_000)
# status: FOUND / BUDGET / TIMEOUT / NONE
```

## Results (v0.3.0)

Setup: Qiskit 2.6.2, rustworkx 0.17.1, FakeKingston unless noted.

**The 16 circuits stock missed** (Ising Trotter on 65-90% of the couplers; `examples/boundary_benchmark.py`):

| | stock level 3 | with fallback | zero-swap |
|---|---|---|---|
| zero-swap layout found | 0/16 | **14/16** | 16/16 by construction |
| 2q gates, all 16 | 14780 | **8738** | 8738 |
| 2q gates, 8 with shuffled numbering | 10394 | **4352** | 4352 |
| compile time, chip numbering (median) | 93.8 s | **55.0 s** | |
| compile time, shuffled numbering (median) | 46.0 s | **45.2 s** | |

- **Every miss that cost gates was fixed**, and no circuit ended with more gates than stock. Median search 0.27 s. Found by piece-pack 13, whole-chip VF2 1.
- **The 2 not solved are chip-numbered** (pieces 49 28 16 11 … and 51 25 20 17 …). Stock's Sabre fallback already reaches their zero-swap gate count, so they cost time but not gates.
- These compile times were measured on a laptop in a power-saving mode. The work budget keeps the *results* the same on any machine; the *times* scale with the machine.

**Piece-pack alone, symmetry breaking off vs on** (all 48 boundary circuits; `examples/search_bench.py`): found 44/48 → **46/48**, never losing a case. Many-small-pieces circuits: 10/12 → **12/12**. For example, a case that timed out at 30 s now takes 0.01 s.

**Regression suite** (`regression/run_regression.py`, 100 search cases plus 32 happy-path circuits). Two runs gave identical results.

- Boundary patterns: 46/48. FakeKingston planted patterns (random patches, 140-156 qubits): **36/36**.
- Synthetic heavy-hex 291 / 547 qubits: 10/16. **The 6 not found are also missed by Qiskit's own `VF2Layout`** with level-3 settings. Each fits with zero swaps by construction; all are high-density patches (80-85% of couplers) that form one or two very large pieces. See Limits.
- Happy path: on all 32 circuits stock solves, the search did not run and the output matched stock's gate count exactly.

v0.2.1: 14/16 on the misses, median search 0.76 s, time-based budget. v0.1.0: 13/16, piece-pack only.

## Limits (read before relying on it)

- **Tested on heavy-hex only:** FakeKingston (Heron r2, 156 q) and synthetic `CouplingMap.from_heavy_hex` graphs (291, 547 q). Other topologies are untested.
- **Very large single pieces are still hard.** When most of the circuit is one giant piece (e.g. 260+ qubits at 80-85% coupler density), there is nothing for piece-pack to split, and it is plain VF2. Six such planted cases on 291/547-qubit chips are solved by neither this package nor stock `VF2Layout`.
- **Two hard Kingston cases remain** (two big pieces of different shapes). Stock gets their gate count anyway; they cost time.
- **Error-aware only in part.** The whole-chip stages keep the lowest-error of up to 50 layouts, and the round-trip stage uses Qiskit's scoring. Piece-pack returns the first valid layout it finds.
- **`NONE` is not a proof** that no layout exists. Inside each stage the VF2 searches have step caps, and a capped search can end without saying so.
- **The safety cap is in seconds.** On very large chips a search can hit the 120 s cap before its work budget runs out (one 291-qubit case does). That one outcome can depend on machine speed.
- **On cases nothing solves, the search spends its full budget** before falling back to Sabre. That can add tens of seconds over stock; it never adds gates.
- **Early exit stops the stock run from inside a pass-manager callback.** If a future Qiskit version changes how callbacks work, use `early_exit=False`; the tests include a check for this.

## Checking it yourself

```
pytest                                       # unit tests
python regression/run_regression.py          # search + happy path, compared with regression/baseline.json
python regression/run_regression.py --full   # also the 16-circuit pipeline (slow)
python examples/boundary_benchmark.py        # stock vs fallback, side by side
```

Every release passes the regression suite first. A case that was solved and no longer is fails the run.

## Related work

- VF2 (Cordella, Foggia, Sansone, Vento, 2004) and VF2++ (Jüttner & Madarasi, 2018), as used by rustworkx.
- Forward checking in constraint satisfaction (Haralick & Elliott, 1980), and symmetry breaking (e.g. lexicographic ordering of interchangeable variables).
- Heavy-tailed run times in backtracking search, with restarts (Gomes, Selman, Crato, Kautz, 2000; Luby, Sinclair, Zuckerman, 1993).
- Bond percolation on lattices: the coupler fraction where heavy-hex breaks into pieces.

## License and citation

Apache-2.0. If this helps your work, please cite it: [doi:10.5281/zenodo.24543880](https://doi.org/10.5281/zenodo.24543880) (details in `CITATION.cff`).

Built by Chris Rozier, Rozier Quantum LLC — [rozierquantum.com](https://rozierquantum.com).
