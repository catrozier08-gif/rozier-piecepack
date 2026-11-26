"""
boundary_benchmark.py: stock Qiskit vs transpile_with_fallback (v0.2: staged search + early exit) on chip-native circuits near the
heavy-hex bond-percolation point (see https://github.com/Qiskit/qiskit/issues/17609).

Circuits: Ising Trotter steps on FakeKingston (156 qubits): rx on every qubit, then rzz on a FIXED
random subset of the couplers (each kept with probability q), in edge-colour rounds, 2 steps. Every
circuit fits the chip with zero swaps by construction. "scrambled" shuffles the qubit numbering.

Needs: qiskit, qiskit-ibm-runtime (FakeKingston), networkx, and this package.
  python examples/boundary_benchmark.py --quick        # 4 known misses
  python examples/boundary_benchmark.py                # the 16 circuits stock missed on Qiskit 2.6.2
  python examples/boundary_benchmark.py --all          # all 48
  python examples/boundary_benchmark.py --time-budget  # old time-based search (v0.2) for comparison
"""
import argparse, csv, os, random, statistics, time
from datetime import datetime

import networkx as nx

from rozier_piecepack import __version__, transpile_with_fallback

# the 16 (q, labels, seed) where stock level 3 missed on Qiskit 2.6.2 / rustworkx 0.17.1
KNOWN_MISSES = [(0.70, "chip order", 3), (0.70, "scrambled", 3), (0.75, "chip order", 2), (0.75, "scrambled", 1),
                (0.78, "chip order", 1), (0.78, "chip order", 3), (0.78, "scrambled", 2), (0.78, "scrambled", 3),
                (0.80, "chip order", 1), (0.80, "chip order", 3), (0.80, "scrambled", 2), (0.80, "scrambled", 3),
                (0.82, "chip order", 1), (0.82, "scrambled", 3), (0.85, "chip order", 1), (0.85, "scrambled", 1)]


def chip_graph(target):
    g = nx.Graph()
    g.add_nodes_from(range(target.num_qubits))
    g.add_edges_from((a, b) for a, b in target.build_coupling_map().get_edges() if a != b)
    return g


def ising_fixed(hw, steps, q, labels, seed):
    from qiskit import QuantumCircuit
    r = random.Random(seed)
    reg = sorted(hw)
    phys = list(reg)
    if labels == "scrambled":
        r.shuffle(phys)
    log = {x: i for i, x in enumerate(phys)}
    used = [e for e in sorted(tuple(sorted(e)) for e in hw.edges()) if r.random() < q]
    sub = nx.Graph(used)
    colour = nx.greedy_color(nx.line_graph(sub), strategy="largest_first") if used else {}
    rounds = {}
    for e, c in colour.items():
        rounds.setdefault(c, []).append(e)
    qc = QuantumCircuit(len(reg))
    for _ in range(steps):
        qc.rx(0.3, range(len(reg)))
        for c in sorted(rounds):
            for u, v in rounds[c]:
                qc.rzz(0.2, log[u], log[v])
    return qc, phys


def twoq(qc):
    return sum(1 for inst in qc.data if len(inst.qubits) == 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=float, default=None, help="safety cap in seconds (default: package default)")
    ap.add_argument("--time-budget", action="store_true", help="use the old time-based search (v0.2) instead of the work budget")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    import qiskit
    from qiskit import transpile
    from qiskit_ibm_runtime.fake_provider import FakeKingston
    target = FakeKingston().target
    hw = chip_graph(target)
    if args.all:
        grid = [(q, lab, s) for q in (0.65, 0.70, 0.75, 0.78, 0.80, 0.82, 0.85, 0.90)
                for lab in ("chip order", "scrambled") for s in (1, 2, 3)]
    elif args.quick:
        grid = [(0.70, "scrambled", 3), (0.75, "scrambled", 1), (0.80, "scrambled", 2), (0.85, "scrambled", 1)]
    else:
        grid = KNOWN_MISSES
    print(f"rozier-piecepack {__version__} | Qiskit {qiskit.__version__} | FakeKingston | "
          f"{'time budget' if args.time_budget else 'work budget'} | safety cap {args.limit or 'default'} | "
          f"{len(grid)} circuits", flush=True)

    path = os.path.join(args.out, f"piecepack_benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    cols = ["q", "labels", "seed", "vf2_stop", "stock_secs", "stock_2q", "stage", "search", "search_secs",
            "used", "aborted_stock", "wrapper_secs", "final_2q", "zero_swap_2q", "pieces"]
    rows = []
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for i, (q, lab, s) in enumerate(grid, 1):
            qc, phys = ising_fixed(hw, 2, q, lab, seed=1000 * s + int(q * 100))
            t0 = time.time()
            stock = transpile(qc, target=target, optimization_level=3, seed_transpiler=s)
            stock_secs = time.time() - t0
            t0 = time.time()
            out, info = transpile_with_fallback(qc, target, optimization_level=3, seed_transpiler=s,
                                                time_limit=args.limit,
                                                budget=None if args.time_budget else "default")
            wrapper_secs = time.time() - t0
            zero = twoq(transpile(qc, target=target, optimization_level=3, seed_transpiler=s, initial_layout=phys))
            row = dict(q=q, labels=lab, seed=s, vf2_stop=info["vf2_stop"], stock_secs=round(stock_secs, 1),
                       stock_2q=twoq(stock), stage=info["stage"] or "", search=info["search"],
                       search_secs=info["search_secs"], used=info["used"], aborted_stock=info["aborted_stock"],
                       wrapper_secs=round(wrapper_secs, 1), final_2q=twoq(out), zero_swap_2q=zero,
                       pieces=" ".join(map(str, (info.get("pieces") or [])[:8])))
            rows.append(row); w.writerow(row); f.flush()
            print(f"[{i:2d}/{len(grid)}] q={q:.2f} {lab:10} s={s}: stock {row['stock_2q']:5d} ({row['stock_secs']:5.1f}s) | "
                  f"wrapper {row['final_2q']:5d} ({row['wrapper_secs']:5.1f}s) zero-swap {zero:5d} | "
                  f"{row['search']} via {row['stage'] or '-'} ({row['search_secs']}) | used {row['used']}", flush=True)

    M = [r for r in rows if r["vf2_stop"] == "NO_SOLUTION_FOUND"]
    print(f"\n=== {len(rows)} circuits, stock VF2Layout missed {len(M)} ===")
    if M:
        R = [r for r in M if r["search"] == "FOUND"]
        print(f"  zero-swap layout found on {len(R)}/{len(M)}")
        stages = {}
        for r in R:
            stages[r["stage"]] = stages.get(r["stage"], 0) + 1
        if stages:
            print("  found by: " + ", ".join(f"{k} {v}" for k, v in sorted(stages.items(), key=lambda kv: -kv[1])))
            print(f"  search time: median {statistics.median(r['search_secs'] for r in R):.2f} s, "
                  f"max {max(r['search_secs'] for r in R):.2f} s")
        print(f"  2q gates on misses: stock {sum(r['stock_2q'] for r in M)} -> wrapper {sum(r['final_2q'] for r in M)} "
              f"(zero-swap {sum(r['zero_swap_2q'] for r in M)})")
        for lab in ("chip order", "scrambled"):
            L = [r for r in M if r["labels"] == lab]
            if L:
                print(f"  {lab:10}: compile time median stock {statistics.median(r['stock_secs'] for r in L):.1f} s -> "
                      f"wrapper {statistics.median(r['wrapper_secs'] for r in L):.1f} s | 2q stock "
                      f"{sum(r['stock_2q'] for r in L)} -> {sum(r['final_2q'] for r in L)} (zero-swap {sum(r['zero_swap_2q'] for r in L)})")
        worse = [r for r in M if r["final_2q"] > r["stock_2q"]]
        if worse:
            print(f"  NOTE: wrapper ended with MORE 2q gates than stock on {len(worse)} circuits - check them")
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
