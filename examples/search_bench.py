"""
search_bench.py: piece-pack's search on its own - no stock compile in front - so changes to the search
can be compared in minutes instead of an hour.

The circuits are the boundary set (Ising Trotter steps on FakeKingston, one fixed random coupler subset
per circuit). Their init stage removes nothing, so the interaction graph read straight from the circuit
is the one VF2Layout sees. Every circuit fits the chip with zero swaps by construction, so a correct
search must always find a layout; NONE or TIMEOUT means the search ran out, not that none exists.

For each circuit, piece-pack runs with symmetry breaking OFF (v0.2 behaviour) and ON, and the script
reports found / time / placements tried / backtracks for both.

Needs: qiskit, qiskit-ibm-runtime (FakeKingston), networkx, and this package.
  python examples/search_bench.py --out C:\\ROZIER_WORKSHOP\\bench_results            # 48 circuits (~5-15 min)
  python examples/search_bench.py --quick --out ...                                  # the 16 stock misses only
  python examples/search_bench.py --seeds 10 --out ...                               # 160 circuits
  python examples/search_bench.py --placements 0 --limit 30 --out ...                # old time-based search
"""
import argparse, csv, os, random, statistics, time
from datetime import datetime

import networkx as nx
import rustworkx as rx

from rozier_piecepack import FOUND, __version__, check_mapping, piece_pack
from rozier_piecepack.qiskit_tools import chip_graph

# the 16 (q, labels, seed) where stock level 3 missed on Qiskit 2.6.2 / rustworkx 0.17.1
KNOWN_MISSES = {(0.70, "chip order", 3), (0.70, "scrambled", 3), (0.75, "chip order", 2), (0.75, "scrambled", 1),
                (0.78, "chip order", 1), (0.78, "chip order", 3), (0.78, "scrambled", 2), (0.78, "scrambled", 3),
                (0.80, "chip order", 1), (0.80, "chip order", 3), (0.80, "scrambled", 2), (0.80, "scrambled", 3),
                (0.82, "chip order", 1), (0.82, "scrambled", 3), (0.85, "chip order", 1), (0.85, "scrambled", 1)}


def nx_chip(target):
    g = nx.Graph()
    g.add_nodes_from(range(target.num_qubits))
    g.add_edges_from((a, b) for a, b in target.build_coupling_map().get_edges() if a != b)
    return g


def ising_pattern(hw, q, labels, seed):
    """Interaction graph of boundary_test's circuit (same random draws as boundary_repro / boundary_benchmark),
    built directly - the rx/rzz gates themselves don't matter to the search, only which pairs interact."""
    r = random.Random(seed)
    phys = sorted(hw)
    if labels == "scrambled":
        r.shuffle(phys)
    log = {x: i for i, x in enumerate(phys)}
    used = [e for e in sorted(tuple(sorted(e)) for e in hw.edges()) if r.random() < q]
    pairs = sorted({tuple(sorted((log[u], log[v]))) for u, v in used})
    active = sorted({x for e in pairs for x in e})
    g = rx.PyGraph()
    g.add_nodes_from(active)
    pos = {x: i for i, x in enumerate(active)}
    g.add_edges_from_no_data([(pos[a], pos[b]) for a, b in pairs])
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="only the 16 circuits stock missed")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--limit", type=float, default=120.0, help="safety cap in seconds")
    ap.add_argument("--placements", type=int, default=300_000, help="work budget per search (0 = old time-based)")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    import qiskit
    from qiskit_ibm_runtime.fake_provider import FakeKingston
    target = FakeKingston().target
    hw, chip = nx_chip(target), chip_graph(target)
    qs = (0.65, 0.70, 0.75, 0.78, 0.80, 0.82, 0.85, 0.90)
    grid = [(q, lab, s) for q in qs for lab in ("chip order", "scrambled") for s in range(1, args.seeds + 1)]
    if args.quick:
        grid = [g for g in grid if g in KNOWN_MISSES]
    print(f"search_bench | rozier-piecepack {__version__} | Qiskit {qiskit.__version__} | rustworkx {rx.__version__} | "
          f"FakeKingston | budget {args.placements or 'time'} placements, safety {args.limit:g} s | {len(grid)} circuits", flush=True)

    path = os.path.abspath(os.path.join(args.out, f"search_bench_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"))
    cols = ["q", "labels", "seed", "stock_missed", "pieces", "largest", "identical",
            "off_status", "off_secs", "off_tried", "off_backtracks",
            "on_status", "on_secs", "on_tried", "on_backtracks", "on_symmetry_skips"]
    rows = []
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for i, (q, lab, s) in enumerate(grid, 1):
            pattern = ising_pattern(hw, q, lab, seed=1000 * s + int(q * 100))
            res = {}
            for sym in (False, True):
                if args.placements:
                    m, status, st = piece_pack(chip, pattern, time_limit=args.limit, symmetry=sym,
                                               max_placements=args.placements, vf2_calls=10_000_000)
                else:
                    m, status, st = piece_pack(chip, pattern, time_limit=args.limit, symmetry=sym)
                if status == FOUND and not check_mapping(chip, pattern, m):
                    status = "BAD MAP"
                res[sym] = (status, st)
            off, on = res[False], res[True]
            sizes = on[1]["pieces"]
            row = dict(q=q, labels=lab, seed=s, stock_missed=(q, lab, s) in KNOWN_MISSES, pieces=len(sizes),
                       largest=sizes[0] if sizes else 0, identical=on[1]["identical_pieces"],
                       off_status=off[0], off_secs=off[1]["secs"], off_tried=off[1]["placements_tried"],
                       off_backtracks=off[1]["backtracks"],
                       on_status=on[0], on_secs=on[1]["secs"], on_tried=on[1]["placements_tried"],
                       on_backtracks=on[1]["backtracks"], on_symmetry_skips=on[1]["symmetry_skips"])
            rows.append(row); w.writerow(row); f.flush()
            print(f"[{i:3d}/{len(grid)}] q={q:.2f} {lab:10} s={s}{' *' if row['stock_missed'] else '  '} "
                  f"pieces {row['pieces']:2d} (largest {row['largest']:3d}, identical {row['identical']:2d}) | "
                  f"OFF {off[0]:7} {off[1]['secs']:6.2f}s bt {off[1]['backtracks']:5d} | "
                  f"ON {on[0]:7} {on[1]['secs']:6.2f}s bt {on[1]['backtracks']:5d}", flush=True)

    def summary(name, R):
        if not R:
            return
        print(f"  {name} ({len(R)} circuits)")
        for side in ("off", "on"):
            got = [r for r in R if r[f"{side}_status"] == FOUND]
            t = [r[f"{side}_secs"] for r in got]
            print(f"    symmetry {side.upper():3}: found {len(got)}/{len(R)}"
                  + (f" | time median {statistics.median(t):.2f} s, max {max(t):.2f} s" if t else "")
                  + f" | out of budget/time {sum(r[f'{side}_status'] in ('TIMEOUT', 'BUDGET') for r in R)}"
                  + f" | bad maps {sum(r[f'{side}_status'] == 'BAD MAP' for r in R)}")
        lost = [r for r in R if r["off_status"] == FOUND and r["on_status"] != FOUND]
        won = [r for r in R if r["on_status"] == FOUND and r["off_status"] != FOUND]
        print(f"    found only with ON: {len(won)} | found only with OFF: {len(lost)}"
              + ("  <- check these" if lost else ""))

    print("\n=== summary (* = stock level 3 missed it) ===")
    summary("all circuits", rows)
    summary("stock misses", [r for r in rows if r["stock_missed"]])
    summary("many small pieces (largest < 40)", [r for r in rows if r["largest"] < 40])
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
