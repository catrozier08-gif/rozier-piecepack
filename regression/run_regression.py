"""
run_regression.py: everything rozier-piecepack has proven, checked again before every release.

Because the search uses a WORK budget (not seconds), each case gives the same answer on any machine,
so results can be compared exactly against a recorded baseline (baseline.json next to this file).

Three parts:
  A. SEARCH (fast, no stock compile): search_layout() on
       - the 48 boundary patterns (Ising Trotter on a fixed coupler subset, FakeKingston), and
       - planted patterns: random subsets of a connected patch of the chip, qubit numbers shuffled, on
         FakeKingston (156 q) and synthetic heavy-hex 291 q / 547 q (--big adds 1081 q).
     Every planted / boundary pattern fits by construction, so every FOUND must check out edge by edge.
     Compared with the baseline: a case that was FOUND and now is not -> FAIL (regression).
     A case that is FOUND now but was not -> reported as an improvement.
  B. HAPPY PATH: the boundary circuits stock level 3 solves on its own. transpile_with_fallback must
     not run the search and must return the same two-qubit gate count as stock.
  C. PIPELINE (--full, slow, ~25 min): the 16 boundary circuits stock misses, end to end. Never more
     two-qubit gates than stock; every fallback layout at the zero-swap count; found count >= baseline.

Usage (from the package folder, Jupyter or a terminal):
  python regression/run_regression.py --record     # first time: run A (+ C with --full) and save baseline.json
  python regression/run_regression.py              # A + B, compared with baseline.json (~5-15 min)
  python regression/run_regression.py --full       # A + B + C
  python regression/run_regression.py --quick      # boundary patterns only in A, B on 8 circuits
  python regression/run_regression.py --stock-check   # also ask: does stock VF2Layout (Qiskit's own settings)
                                                      # solve the cases our search did not? (info only, ~30 s each)
Exit code 0 = all passed, 1 = something failed. Results are written to --out (CSV + JSON).
"""
import argparse, csv, json, os, random, sys, time
from datetime import datetime

import networkx as nx
import rustworkx as rx

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE = os.path.join(HERE, "baseline.json")

from rozier_piecepack import FOUND, __version__, check_mapping
from rozier_piecepack.qiskit_tools import chip_graph, search_layout, transpile_with_fallback

QS = (0.65, 0.70, 0.75, 0.78, 0.80, 0.82, 0.85, 0.90)
LABELS = ("chip order", "scrambled")


# ---------------------------------------------------------------- case builders
def nx_chip(cmap_or_target):
    cmap = cmap_or_target.build_coupling_map() if hasattr(cmap_or_target, "build_coupling_map") else cmap_or_target
    g = nx.Graph()
    g.add_nodes_from(range(cmap.size()))
    g.add_edges_from((a, b) for a, b in cmap.get_edges() if a != b)
    return g


def boundary_circuit(hw, q, labels, seed, steps=2):
    """Same builder (same random draws) as boundary_repro.py / examples/boundary_benchmark.py."""
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


def pattern_from_pairs(pairs):
    active = sorted({x for e in pairs for x in e})
    g = rx.PyGraph()
    g.add_nodes_from(active)
    pos = {x: i for i, x in enumerate(active)}
    g.add_edges_from_no_data(sorted((pos[a], pos[b]) for a, b in pairs))
    return g


def boundary_pattern(hw, q, labels, seed):
    r = random.Random(seed)
    phys = sorted(hw)
    if labels == "scrambled":
        r.shuffle(phys)
    log = {x: i for i, x in enumerate(phys)}
    used = [e for e in sorted(tuple(sorted(e)) for e in hw.edges()) if r.random() < q]
    return pattern_from_pairs({tuple(sorted((log[u], log[v]))) for u, v in used})


def planted_pattern(hw, n, p, seed):
    """A connected patch of n chip qubits (random BFS), each coupler inside it kept with probability p,
    qubit numbers shuffled. Fits the chip by construction."""
    r = random.Random(seed)
    nodes = sorted(hw)
    start = r.choice(nodes)
    seen, order, frontier = {start}, [start], [start]
    while frontier and len(order) < n:
        nxt = []
        for u in frontier:
            nb = sorted(hw[u])
            r.shuffle(nb)
            for v in nb:
                if v not in seen and len(order) < n:
                    seen.add(v); order.append(v); nxt.append(v)
        frontier = nxt
    region = set(order)
    used = [e for e in sorted(tuple(sorted(e)) for e in hw.subgraph(region).edges()) if r.random() < p]
    labels = list(range(len(order)))
    r.shuffle(labels)
    lab = dict(zip(order, labels))
    return pattern_from_pairs({tuple(sorted((lab[a], lab[b]))) for a, b in used})


def search_cases(quick, big):
    from qiskit.transpiler import CouplingMap
    from qiskit_ibm_runtime.fake_provider import FakeKingston
    king = FakeKingston().target
    hw_k = nx_chip(king)
    cases = []
    for q in QS:
        for lab in LABELS:
            for s in (1, 2, 3):
                cases.append((f"boundary|{q}|{lab}|{s}", king, lambda q=q, lab=lab, s=s:
                              boundary_pattern(hw_k, q, lab, 1000 * s + int(q * 100))))
    if not quick:
        for n in (140, 148, 152, 156):
            for p in (0.5, 0.7, 0.85):
                for s in (1, 2, 3):
                    cases.append((f"planted|kingston|{n}|{p}|{s}", king, lambda n=n, p=p, s=s:
                                  planted_pattern(hw_k, n, p, 7000 + 100 * s + n)))
        synth = [(11, (262, 280, 291), (0.6, 0.8), (1, 2)), (15, (520, 547), (0.7, 0.85), (1,))]
        if big:
            synth.append((21, (1030, 1081), (0.7, 0.85), (1,)))
        for d, ns, ps, seeds in synth:
            cm = CouplingMap.from_heavy_hex(d)
            hw = nx_chip(cm)
            for n in ns:
                for p in ps:
                    for s in seeds:
                        cases.append((f"planted|heavyhex{cm.size()}|{n}|{p}|{s}", cm, lambda hw=hw, n=n, p=p, s=s:
                                      planted_pattern(hw, n, p, 9000 + 100 * s + n)))
    return cases


# ---------------------------------------------------------------- parts
def stock_vf2(pattern, device):
    """Does Qiskit's own VF2Layout (level-3 settings: seed=-1, call_limit=(3e7, 1e5)) find a zero-swap layout
    for this pattern? Returns (stop reason, seconds). Info only."""
    from qiskit import QuantumCircuit
    from qiskit.transpiler import PassManager
    from qiskit.transpiler.passes import VF2Layout
    n = (max(pattern[x] for x in pattern.node_indices()) + 1) if pattern.num_nodes() else 1
    qc = QuantumCircuit(n)
    for a, b in pattern.edge_list():
        qc.cz(pattern[a], pattern[b])
    kw = {"target": device} if hasattr(device, "operation_names") else {"coupling_map": device}
    pm = PassManager([VF2Layout(seed=-1, call_limit=(30_000_000, 100_000), **kw)])
    t0 = time.time()
    pm.run(qc)
    r = pm.property_set.get("VF2Layout_stop_reason")
    return getattr(r, "name", str(r)), time.time() - t0


def part_a(args, base, rows_out):
    print("\n== A. SEARCH (work budget, same answer on any machine) ==", flush=True)
    chips = {}
    res, fails, improved = {}, [], []
    cases = search_cases(args.quick, args.big)
    if args.only:
        cases = [c for c in cases if args.only in c[0]]
    for i, (cid, device, make) in enumerate(cases, 1):
        pattern = make()
        key = id(device)
        if key not in chips:
            chips[key] = chip_graph(device)
        chip = chips[key]
        t0 = time.time()
        mapping, info = search_layout(pattern, chip, device=device, circuit=None)
        secs = time.time() - t0
        status = info["status"]
        if status == FOUND and not check_mapping(chip, pattern, {k: mapping[k] for k in
                                                               (pattern[n] for n in pattern.node_indices())}):
            status = "BAD MAP"
        res[cid] = {"status": status, "stage": info.get("stage")}
        was = base.get("A", {}).get(cid, {}).get("status")
        verdict = ""
        if status == "BAD MAP":
            fails.append(cid); verdict = "FAIL (bad map)"
        elif was == FOUND and status != FOUND:
            fails.append(cid); verdict = "FAIL (was FOUND)"
        elif was is not None and was != FOUND and status == FOUND:
            improved.append(cid); verdict = "improved"
        stock = ""
        if args.stock_check and status != FOUND:
            reason, ssecs = stock_vf2(pattern, device)
            stock = f"{reason} ({ssecs:.1f}s)"
        rows_out.append(dict(part="A", case=cid, status=status, stage=info.get("stage") or "", secs=round(secs, 2),
                             baseline=was or "", verdict=verdict, stock_vf2=stock))
        print(f"[{i:3d}/{len(cases)}] {cid:42} {status:8} {info.get('stage') or '-':22} {secs:6.2f}s "
              f"{('| baseline ' + was) if was else ''} {verdict}{('| stock VF2Layout: ' + stock) if stock else ''}",
              flush=True)
    found = sum(v["status"] == FOUND for v in res.values())
    print(f"  A: {found}/{len(res)} found | failures {len(fails)} | improvements {len(improved)}")
    return res, fails, improved


def part_b(args, rows_out):
    print("\n== B. HAPPY PATH (stock solves it: search must not run, output must match stock) ==", flush=True)
    from qiskit import transpile
    from qiskit_ibm_runtime.fake_provider import FakeKingston
    king = FakeKingston().target
    hw = nx_chip(king)
    misses = {(0.70, "chip order", 3), (0.70, "scrambled", 3), (0.75, "chip order", 2), (0.75, "scrambled", 1),
              (0.78, "chip order", 1), (0.78, "chip order", 3), (0.78, "scrambled", 2), (0.78, "scrambled", 3),
              (0.80, "chip order", 1), (0.80, "chip order", 3), (0.80, "scrambled", 2), (0.80, "scrambled", 3),
              (0.82, "chip order", 1), (0.82, "scrambled", 3), (0.85, "chip order", 1), (0.85, "scrambled", 1)}
    grid = [(q, lab, s) for q in QS for lab in LABELS for s in (1, 2, 3) if (q, lab, s) not in misses]
    if args.quick:
        grid = grid[::4]
    fails = []
    for i, (q, lab, s) in enumerate(grid, 1):
        qc, _ = boundary_circuit(hw, q, lab, 1000 * s + int(q * 100))
        stock = transpile(qc, target=king, optimization_level=3, seed_transpiler=s)
        out, info = transpile_with_fallback(qc, king, optimization_level=3, seed_transpiler=s)
        s2q = sum(1 for x in stock.data if len(x.qubits) == 2)
        w2q = sum(1 for x in out.data if len(x.qubits) == 2)
        ok = info["vf2_stop"] == "SOLUTION_FOUND" and info["search"] == "NOT_RUN" and w2q == s2q
        cid = f"boundary|{q}|{lab}|{s}"
        if not ok:
            fails.append(cid)
        rows_out.append(dict(part="B", case=cid, status=info["vf2_stop"], stage=info["search"], secs="",
                             baseline=s2q, verdict="ok" if ok else f"FAIL (2q {w2q} vs stock {s2q})"))
        print(f"[{i:2d}/{len(grid)}] {cid:42} VF2 {info['vf2_stop']:17} search {info['search']:8} "
              f"2q stock {s2q} / package {w2q}  {'ok' if ok else 'FAIL'}", flush=True)
    print(f"  B: {len(grid) - len(fails)}/{len(grid)} ok | failures {len(fails)}")
    return fails


def part_c(args, base, rows_out):
    print("\n== C. PIPELINE on the 16 stock misses (slow) ==", flush=True)
    from qiskit import transpile
    from qiskit_ibm_runtime.fake_provider import FakeKingston
    king = FakeKingston().target
    hw = nx_chip(king)
    grid = [(0.70, "chip order", 3), (0.70, "scrambled", 3), (0.75, "chip order", 2), (0.75, "scrambled", 1),
            (0.78, "chip order", 1), (0.78, "chip order", 3), (0.78, "scrambled", 2), (0.78, "scrambled", 3),
            (0.80, "chip order", 1), (0.80, "chip order", 3), (0.80, "scrambled", 2), (0.80, "scrambled", 3),
            (0.82, "chip order", 1), (0.82, "scrambled", 3), (0.85, "chip order", 1), (0.85, "scrambled", 1)]
    fails, found = [], 0
    for i, (q, lab, s) in enumerate(grid, 1):
        qc, phys = boundary_circuit(hw, q, lab, 1000 * s + int(q * 100))
        stock = transpile(qc, target=king, optimization_level=3, seed_transpiler=s)
        out, info = transpile_with_fallback(qc, king, optimization_level=3, seed_transpiler=s)
        zero = transpile(qc, target=king, optimization_level=3, seed_transpiler=s, initial_layout=phys)
        s2q, w2q, z2q = (sum(1 for x in c.data if len(x.qubits) == 2) for c in (stock, out, zero))
        cid = f"boundary|{q}|{lab}|{s}"
        bad = w2q > s2q or (info["search"] == FOUND and w2q != z2q)
        found += info["search"] == FOUND
        if bad:
            fails.append(cid)
        rows_out.append(dict(part="C", case=cid, status=info["search"], stage=info.get("stage") or "", secs="",
                             baseline=s2q, verdict="FAIL" if bad else "ok"))
        print(f"[{i:2d}/16] {cid:42} {info['search']:8} 2q stock {s2q} -> {w2q} (zero-swap {z2q}) "
              f"{'FAIL' if bad else 'ok'}", flush=True)
    was = base.get("C", {}).get("found")
    if was is not None and found < was:
        fails.append(f"found {found} < baseline {was}")
    print(f"  C: found {found}/16 (baseline {was if was is not None else '-'}) | failures {len(fails)}")
    return {"found": found}, fails


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="save this run as the new baseline")
    ap.add_argument("--full", action="store_true", help="also run part C (slow)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--big", action="store_true", help="add 1081-qubit planted cases to part A")
    ap.add_argument("--stock-check", action="store_true", help="for cases our search did not solve, run stock VF2Layout (info)")
    ap.add_argument("--only", default=None, help="only part A cases whose name contains this text (e.g. heavyhex)")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args, _ = ap.parse_known_args(argv)
    os.makedirs(args.out, exist_ok=True)
    if args.record and args.only:
        print("--record with --only would save a partial baseline; run --record without --only.")
        return 1
    import qiskit
    print(f"REGRESSION | rozier-piecepack {__version__} | Qiskit {qiskit.__version__} | rustworkx {rx.__version__}")
    base = {}
    if os.path.exists(BASELINE) and not args.record:
        base = json.load(open(BASELINE, encoding="utf-8"))
        print(f"baseline: {base.get('version')} recorded {base.get('recorded')} "
              f"(Qiskit {base.get('qiskit')}, rustworkx {base.get('rustworkx')})")
        if base.get("rustworkx") != rx.__version__ or base.get("qiskit") != qiskit.__version__:
            print("  NOTE: different Qiskit/rustworkx than the baseline - search order can change; "
                  "treat differences as a reason to look, then re-record if they are fine.")
    elif not args.record:
        print("no baseline.json yet: run once with --record")

    rows, fails = [], []
    a_res, a_fail, a_imp = part_a(args, base, rows)
    fails += a_fail
    if not args.record and not args.only:
        fails += part_b(args, rows)
    c_res = None
    if args.full:
        c_res, c_fail = part_c(args, base, rows)
        fails += c_fail

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(args.out, f"regression_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["part", "case", "status", "stage", "secs", "baseline", "verdict", "stock_vf2"],
                           extrasaction="ignore", restval="")
        w.writeheader(); w.writerows(rows)

    if args.record:
        new = {"version": __version__, "recorded": stamp, "qiskit": qiskit.__version__,
               "rustworkx": rx.__version__, "A": a_res}
        if c_res is not None:
            new["C"] = c_res
        elif "C" in base:
            new["C"] = base["C"]
        json.dump(new, open(BASELINE, "w", encoding="utf-8"), indent=1, sort_keys=True)
        print(f"\nBaseline saved: {BASELINE}")

    print("\n" + ("=" * 60))
    if fails:
        print(f"REGRESSION FAILED: {len(fails)} problem(s)")
        for x in fails:
            print("   -", x)
    else:
        print("ALL CHECKS PASSED" + (f" | improvements: {len(a_imp)}" if a_imp else ""))
    print(f"Results: {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    code = main()
    if "ipykernel" not in sys.modules:
        sys.exit(code)
