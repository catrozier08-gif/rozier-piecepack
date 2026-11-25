"""
boundary_benchmark.py: stock Qiskit vs transpile_with_fallback on chip-native circuits near the
heavy-hex bond-percolation point (see https://github.com/Qiskit/qiskit/issues/17609).

Circuits: Ising Trotter steps on FakeKingston (156 qubits): rx on every qubit, then rzz on a FIXED
random subset of the couplers (each kept with probability q), in edge-colour rounds, 2 steps. Every
circuit fits the chip with zero swaps by construction. "scrambled" shuffles the qubit numbering.

Needs: qiskit, qiskit-ibm-runtime (FakeKingston), networkx, and this package.
  python examples/boundary_benchmark.py --quick        # 4 known misses
  python examples/boundary_benchmark.py                # the 16 circuits stock missed on Qiskit 2.6.2
  python examples/boundary_benchmark.py --all          # all 48
  python examples/boundary_benchmark.py --limit 30     # piece-pack time limit (default 30 s)
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
    ap.add_argument("--limit", type=float, default=30.0)
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
    print(f"rozier-piecepack {__version__} | Qiskit {qiskit.__version__} | FakeKingston | limit {args.limit:g} s | "
          f"{len(grid)} circuits", flush=True)

    path = os.path.join(args.out, f"piecepack_benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    cols = ["q", "labels", "seed", "vf2_stop", "vf2_secs", "piece_pack", "piece_pack_secs", "pieces",
            "used", "stock_2q", "final_2q", "zero_swap_2q", "total_secs"]
    rows = []
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for i, (q, lab, s) in enumerate(grid, 1):
            qc, phys = ising_fixed(hw, 2, q, lab, seed=1000 * s + int(q * 100))
            t0 = time.time()
            out, info = transpile_with_fallback(qc, target, optimization_level=3, seed_transpiler=s,
                                                time_limit=args.limit)
            total = time.time() - t0
            zero = twoq(transpile(qc, target=target, optimization_level=3, seed_transpiler=s, initial_layout=phys))
            row = dict(q=q, labels=lab, seed=s, vf2_stop=info["vf2_stop"], vf2_secs=round(info["vf2_secs"] or 0, 1),
                       piece_pack=info["piece_pack"], piece_pack_secs=info["piece_pack_secs"],
                       pieces=" ".join(map(str, (info.get("pieces") or [])[:8])), used=info["used"],
                       stock_2q=info["stock_2q"], final_2q=twoq(out), zero_swap_2q=zero, total_secs=round(total, 1))
            rows.append(row); w.writerow(row); f.flush()
            print(f"[{i:2d}/{len(grid)}] q={q:.2f} {lab:10} s={s}: VF2 {row['vf2_stop']} ({row['vf2_secs']:.1f}s) | "
                  f"piece-pack {row['piece_pack']} ({row['piece_pack_secs']}) | 2q stock {row['stock_2q']} -> "
                  f"{row['final_2q']} (zero-swap {zero}) | used {row['used']}", flush=True)

    M = [r for r in rows if r["vf2_stop"] == "NO_SOLUTION_FOUND"]
    print(f"\n=== {len(rows)} circuits, stock VF2Layout missed {len(M)} ===")
    if M:
        R = [r for r in M if r["piece_pack"] == "FOUND"]
        print(f"  piece-pack found a zero-swap layout on {len(R)}/{len(M)}")
        if R:
            print(f"  piece-pack time: median {statistics.median(r['piece_pack_secs'] for r in R):.2f} s, "
                  f"max {max(r['piece_pack_secs'] for r in R):.2f} s "
                  f"(stock VF2Layout had already spent median {statistics.median(r['vf2_secs'] for r in M):.1f} s)")
        print(f"  2q gates on misses: stock {sum(r['stock_2q'] for r in M)} -> with fallback "
              f"{sum(r['final_2q'] for r in M)} (zero-swap {sum(r['zero_swap_2q'] for r in M)})")
        bad = [r for r in R if r["final_2q"] > r["zero_swap_2q"]]
        if bad:
            print(f"  NOTE: {len(bad)} rescued circuits ended above the zero-swap count - check them")
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
