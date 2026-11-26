# Changelog

## 0.3.0 (2026-11-26)
- **Symmetry breaking for identical pieces.** Same-shaped pieces are placed in a fixed order, and their spots are tried in sorted order. Many-small-pieces circuits went from 10/12 to 12/12 solved, with some 30 s timeouts now solved in hundredths of a second. No case lost.
- **Work budget instead of seconds** (`DEFAULT_BUDGET`). The same circuit gives the same answer on any machine; seconds are only a 120 s safety cap. `budget=None` restores the time-based search. New status `BUDGET`.
- **Quick piece-pack try** is now 20,000 placements, catching most solves before the other stages.
- **Regression suite** (`regression/run_regression.py`): 100 search cases (boundary, planted FakeKingston, planted 291/547-qubit heavy-hex) compared with a recorded baseline, plus 32 happy-path circuits that must match stock exactly. `--full` adds the 16-circuit pipeline; `--stock-check` asks Qiskit's own VF2Layout about unsolved cases.
- **`examples/search_bench.py`**: piece-pack alone, symmetry off vs on, in minutes.
- Fixed: chips whose node payloads aren't 0..N-1.
- Results: 14/16 on the stock misses, 2q 14780 → 8738, never worse than stock; regression 92/100 search cases + 32/32 happy path. The 8 search cases not found include 6 large-chip cases that stock VF2Layout also misses.

## 0.2.1 (2026-11-26)
- Quick 1 s piece-pack try before the other stages. Median search 6.15 s → 0.76 s.

## 0.2.0 (2026-11-25)
- Staged search: round-trip VF2Layout retry, whole-chip VF2 in two orders (error-scored), then piece-pack.
- Early exit: stop the stock run right after a VF2Layout miss (skips Sabre and the VF2PostLayout repeat); if nothing is found, compile with Sabre like stock.
- 14/16 on the stock misses (v0.1: 13).

## 0.1.0 (2026-11-25)
- First release: piece-pack fallback after VF2Layout NO_SOLUTION_FOUND. 13/16 on the stock misses, all 8 gate-costing misses fixed.
