# rozier-piecepack

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.24543879.svg)](https://doi.org/10.5281/zenodo.24543879)

A fallback layout search for Qiskit. It finds zero-swap layouts that the stock `VF2Layout` pass misses when a circuit's two-qubit interaction graph is split into several pieces and nearly every qubit on the chip is in use.

**On real IBM hardware, those layouts matter.** In a mirror test of 10 such circuits on three 156-qubit chips, the package's layouts gave fewer errors in 60 of 60 runs, with 44% of stock's error on average ([details below](#real-hardware)).

Background and data: [Qiskit issue #17609](https://github.com/Qiskit/qiskit/issues/17609).

## The problem in one paragraph

On IBM's 156-qubit heavy-hex (Heron r2) graph, `transpile(optimization_level=3)` sometimes reports `VF2Layout` `NO_SOLUTION_FOUND` for circuits that fit the chip with zero swaps. It then falls back to SabreLayout. The miss costs 17-36 s of VF2 search. With the chip's own qubit numbering, Sabre usually still lands on a zero-swap layout, so only time is lost. With any other numbering, the result can have ~2.4x the two-qubit gates. On chip-numbered misses a second pass, `VF2PostLayout`, then repeats the same hard search, roughly doubling the compile time.

This happens with ordinary circuits. One example is Ising Trotter steps on a fixed subset of ~70-85% of the couplers. That is near heavy-hex's bond-percolation point: almost every qubit is active, but the pattern breaks into several medium-sized pieces.

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
- **Symmetry breaking for identical pieces** (new in v0.3):
  - Pieces with the same shape are interchangeable. Trying A here and B there, then B here and A there, repeats the same layout.
  - So identical pieces are placed next to each other, and each must take a spot that sorts after the previous one's. Their spots are tried in sorted order, so the search order agrees with the rule.
  - No solution is lost, because any layout can be reordered to satisfy the rule.
  - This is what makes circuits with many small pieces fast.
- Placements covering exactly the same qubits as one already tried are skipped.

**Work budget, not seconds** (new in v0.3). Every stage gets a fixed amount of work: placements tried, and VF2 calls. So the same circuit gives the same answer on any machine, busy or idle. Seconds are only a safety cap (120 s). `budget=None` restores the v0.2 time-based search.

**What happens after the search:**
- If a zero-swap layout is found, the circuit is compiled with it as `initial_layout`.
- If nothing is found, the circuit is compiled with `layout_method="sabre"`, the same method stock falls back to. So the result is never worse than stock, and `VF2Layout` doesn't run twice.
- When `VF2Layout` succeeds in the first place, nothing extra runs.

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

In a custom pass manager, use `PiecePackLayout(target=...)`. It is an analysis pass that runs the same staged search and sets `property_set["layout"]` when it finds a zero-swap layout. By default it does nothing if a layout is already set. Its outcome is recorded in `property_set["PiecePackLayout_stop_reason"]`.

The searches alone need only rustworkx:

```python
from rozier_piecepack import piece_pack, check_mapping
mapping, status, stats = piece_pack(chip_graph, pattern_graph, max_placements=300_000, vf2_calls=10_000_000)
# status: FOUND / BUDGET / TIMEOUT / NONE
```

## Real hardware

**Question:** when stock misses the zero-swap layout and this package finds it, does the circuit really run better on a quantum computer?

**Method: a mirror test** (`examples/mirror_study.py`).
1. Take an Ising Trotter circuit that stock level 3 missed and the package rescued, and compile it both ways.
2. Run each compiled circuit forward, then a barrier, then its exact inverse. The ideal output is every qubit 0, so every 1 measured is an error.
3. Score each version by its **fraction of wrong bits**.

Both versions ran side by side in the same job, at 2000 shots each.

**Setup:**
- 10 circuits: the first 10 stock misses found, with coupler fractions 0.70–0.85 and shuffled qubit numbering.
- 3 chips: IBM `ibm_kingston`, `ibm_fez` and `ibm_marrakesh` (Heron r2, 156 qubits).
- 2 repeats of every job.
- Software: Qiskit 2.6.2, qiskit-ibm-runtime 0.52.1, rozier-piecepack 0.3.0, run on 2026-11-26.
- Default Sampler settings for both versions.
- Total IBM time: about 78 s.

| chip | runs | stock wrong bits | package wrong bits | package ÷ stock (mean) | package better |
|---|---|---|---|---|---|
| ibm_kingston | 20 | 17.3% | 7.7% | 0.45 | 20/20 |
| ibm_fez | 20 | 22.3% | 9.0% | 0.41 | 20/20 |
| ibm_marrakesh | 20 | 20.4% | 9.5% | 0.47 | 20/20 |
| **all** | **60** | **20.0%** | **8.7%** | **0.44** | **60/60** |

- **Fewer errors in every run, on every chip.** On average the package's layouts had 44% of stock's wrong bits. The 95% interval, bootstrapped over the 10 circuits, is 0.42–0.48.
- **Repeat runs agreed closely.** For the same circuit on the same chip, the ratio changed by 0.004 on average and by at most 0.014.
- **The error drop follows the gate drop.** Across circuits, the package-to-stock ratio of two-qubit gates predicts the ratio of wrong bits (r = 0.79). The extra errors come from the extra gates that stock's fallback layout needs.

Per circuit, averaged over the three chips and both repeats:

| coupler fraction, seed | stock 2q gates | package 2q gates | gate ratio | wrong-bit ratio |
|---|---|---|---|---|
| 0.82, 3 | 1621 | 580 | 0.36 | 0.37 |
| 0.85, 1 | 1514 | 592 | 0.39 | 0.40 |
| 0.82, 4 | 1460 | 566 | 0.39 | 0.40 |
| 0.78, 3 | 1310 | 550 | 0.42 | 0.42 |
| 0.80, 4 | 1548 | 586 | 0.38 | 0.42 |
| 0.80, 2 | 1305 | 548 | 0.42 | 0.45 |
| 0.80, 3 | 1336 | 554 | 0.41 | 0.45 |
| 0.75, 1 | 1167 | 522 | 0.45 | 0.47 |
| 0.78, 2 | 1233 | 544 | 0.44 | 0.49 |
| 0.70, 3 | 908 | 462 | 0.51 | 0.55 |

**What this does and doesn't show:**
- **Only circuits stock misses were run.** When stock finds the layout itself, the package doesn't run and the output is identical.
- **One circuit family at one size:** 2-step Ising Trotter on 156 qubits. Other circuits and sizes are untested on hardware.
- **The three chips have the same coupler map** (176 working couplers, none broken). So the 10 circuits are identical on all three; only each chip's noise differs.
- **Error suppression was off.** No dynamical decoupling or twirling was used for either version. With it on, the gap may change.
- **The mirror test measures accumulated error,** not the accuracy of a physics result.
- **Piece-pack takes the first valid layout,** not the lowest-error one, so there may be more to gain.

To run it yourself you need an IBM Quantum account. The script finds circuits locally first, shows the plan and the estimated IBM time, and sends nothing until you type `yes`. `examples/mirror_test.py` is the single-circuit version.

## Results (v0.3.0, simulated compile)

Setup: Qiskit 2.6.2, rustworkx 0.17.1, FakeKingston unless noted.

**The 16 circuits stock missed** (Ising Trotter on 65-90% of the couplers; `examples/boundary_benchmark.py`):

| | stock level 3 | with fallback | zero-swap |
|---|---|---|---|
| zero-swap layout found | 0/16 | **14/16** | 16/16 by construction |
| 2q gates, all 16 | 14780 | **8738** | 8738 |
| 2q gates, 8 with shuffled numbering | 10394 | **4352** | 4352 |
| compile time, chip numbering (median) | 93.8 s | **55.0 s** | |
| compile time, shuffled numbering (median) | 46.0 s | **45.2 s** | |

- **Every miss that cost gates was fixed**, and no circuit ended with more gates than stock. Median search 0.27 s. Piece-pack found 13 and whole-chip VF2 found 1.
- **The 2 not solved are chip-numbered** (pieces 49 28 16 11 … and 51 25 20 17 …). Stock's Sabre fallback already reaches their zero-swap gate count, so they cost time but not gates.
- These compile times were measured on a laptop in a power-saving mode. The work budget keeps the *results* the same on any machine; the *times* scale with the machine.

**Piece-pack alone, symmetry breaking off vs on** (all 48 boundary circuits; `examples/search_bench.py`): found 44/48 → **46/48**, never losing a case. Many-small-pieces circuits: 10/12 → **12/12**. For example, a case that timed out at 30 s now takes 0.01 s.

**Regression suite** (`regression/run_regression.py`, 100 search cases plus 32 happy-path circuits). Two runs gave identical results.

- Boundary patterns: 46/48. FakeKingston planted patterns (random patches, 140-156 qubits): **36/36**.
- Synthetic heavy-hex 291 / 547 qubits: 10/16. **The 6 not found are also missed by Qiskit's own `VF2Layout`** with level-3 settings. Each fits with zero swaps by construction. All are high-density patches (80-85% of couplers) that break into 8-16 pieces, the largest of 64-513 qubits. See Limits.
- Happy path: on all 32 circuits stock solves, the search did not run and the output matched stock's gate count exactly.

v0.2.1: 14/16 on the misses, median search 0.76 s, time-based budget. v0.1.0: 13/16, piece-pack only.

## Limits (read before relying on it)

- **Tested on heavy-hex only:** FakeKingston (Heron r2, 156 q), the three real Heron r2 chips above, and synthetic `CouplingMap.from_heavy_hex` graphs (291, 547 q). Other topologies are untested.
- **Large, dense patterns on bigger chips are still hard.** Six planted cases on 291/547-qubit chips are solved by neither this package nor stock `VF2Layout`. They fail in two ways:
  - **Three have one piece too big for VF2.** The largest piece alone (195, 405 or 513 qubits) is already too hard for VF2 within its call budget, so piece-pack never makes its first placement.
  - **Three fail in packing.** Each piece fits on its own, but no packing of all of them was found within budget.
  - More budget didn't help (5× gave 0/6), and neither did other VF2 matching orders.
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
python examples/mirror_study.py              # real hardware (IBM account; asks before using any IBM time)
```

Every release passes the regression suite first. A case that was solved and no longer is fails the run.

## Related work

- VF2 (Cordella, Foggia, Sansone, Vento, 2004) and VF2++ (Jüttner & Madarasi, 2018), as used by rustworkx.
- Forward checking in constraint satisfaction (Haralick & Elliott, 1980), and symmetry breaking (e.g. lexicographic ordering of interchangeable variables).
- Heavy-tailed run times in backtracking search, with restarts (Gomes, Selman, Crato, Kautz, 2000; Luby, Sinclair, Zuckerman, 1993).
- Bond percolation on lattices: the coupler fraction where heavy-hex breaks into pieces.
- Mirror circuits as a hardware benchmark (Proctor et al., "Measuring the capabilities of quantum computers", Nature Physics 2022). The test here is the simplest form: the compiled circuit, then its exact inverse.

## License and citation

Apache-2.0. If this helps your work, please cite it: [doi:10.5281/zenodo.24543879](https://doi.org/10.5281/zenodo.24543879). That DOI covers all versions and always points to the latest; each version also has its own DOI on Zenodo (v0.3.1: [10.5281/zenodo.24586994](https://doi.org/10.5281/zenodo.24586994)). Details in `CITATION.cff`.

Built by Chris Rozier, Rozier Quantum LLC — [rozierquantum.com](https://rozierquantum.com).
