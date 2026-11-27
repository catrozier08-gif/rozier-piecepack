"""
mirror_study.py: the mirror test on MANY circuits and several chips, with error bars.

Same method as mirror_test.py (same circuit builder, same mirror, same wrong-bit score):
  1. LOCAL (no IBM time): for each chip, collect up to --circuits circuits where stock level 3 MISSES the
     zero-swap layout and rozier-piecepack finds it. Compile each both ways and build the two mirrors.
  2. Print the plan and the estimated IBM seconds, check your remaining free time, and ask before sending.
  3. One job per chip per repeat, holding every circuit for that chip (stock and package side by side).
     Job IDs are saved the moment they're submitted, so if Jupyter crashes nothing is lost: run again
     with --collect <that jobs file> to fetch the results.
  4. One CSV with every circuit's result, and a summary: mean / median / worst ratio, a 95% interval,
     and how many circuits the package did better on.

  %run "<package>\\examples\\mirror_study.py"                         # dry run: finds circuits, prints the plan
  %run "<package>\\examples\\mirror_study.py" --run                   # asks "yes" before sending anything
  %run "<package>\\examples\\mirror_study.py" --run --repeats 2       # run every job twice
  %run "<package>\\examples\\mirror_study.py" --collect "<out>\\mirror_study_jobs_<stamp>.json"
  options: --backends ibm_kingston,ibm_fez,ibm_marrakesh  --circuits 10  --shots 2000
           --max-candidates 40  --keep-seconds 150  --out "C:\\ROZIER_WORKSHOP\\bench_results"

Uses the IBM account saved on this computer (see mirror_test.py). Never put the key in a notebook.
The local search takes a while: misses cost stock ~17 s each, so expect roughly 5-10 minutes per chip.
"""
# --- use THIS folder's package code (no setup cell needed) ---
import os as _os, sys as _sys
_SRC = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "src"))
if _os.path.isdir(_SRC) and _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)
_m = _sys.modules.get("rozier_piecepack")
if _m is not None and not _os.path.abspath(getattr(_m, "__file__", "")).startswith(_SRC):
    for _k in [k for k in _sys.modules if k == "rozier_piecepack" or k.startswith("rozier_piecepack.")]:
        del _sys.modules[_k]
# ---------------------------------------------------------------
import argparse, csv, json, os, random, statistics, time
from datetime import datetime

import networkx as nx

from rozier_piecepack import __version__, transpile_with_fallback

DEFAULT_BACKENDS = "ibm_kingston,ibm_fez,ibm_marrakesh"
SECONDS_PER_PAIR = 3.0   # conservative: a lone pair was billed 3 s; a 10-pair job was billed ~13 s (~1.3 s/pair)

# The 14 from mirror_test.py first (so (0.78, 2) is the same circuit as the published runs), then more.
_FIRST = [(0.78, 2), (0.80, 2), (0.80, 3), (0.75, 1), (0.78, 3), (0.82, 3), (0.85, 1), (0.70, 3),
          (0.78, 1), (0.80, 1), (0.82, 1), (0.75, 2), (0.85, 2), (0.82, 2)]
_MORE = [(q, s) for s in range(1, 9) for q in (0.70, 0.72, 0.75, 0.78, 0.80, 0.82, 0.85)]
CANDIDATES = _FIRST + [c for c in _MORE if c not in _FIRST]


# ---------- same helpers as mirror_test.py ----------
def chip_nx(target):
    g = nx.Graph()
    g.add_nodes_from(range(target.num_qubits))
    g.add_edges_from((a, b) for a, b in target.build_coupling_map().get_edges() if a != b)
    return g


def ising_fixed(hw, steps, q, seed):
    """Same builder as boundary_repro.py / mirror_test.py, shuffled numbering."""
    from qiskit import QuantumCircuit
    r = random.Random(seed)
    phys = sorted(hw)
    r.shuffle(phys)
    log = {x: i for i, x in enumerate(phys)}
    used = [e for e in sorted(tuple(sorted(e)) for e in hw.edges()) if r.random() < q]
    sub = nx.Graph(used)
    colour = nx.greedy_color(nx.line_graph(sub), strategy="largest_first") if used else {}
    rounds = {}
    for e, c in colour.items():
        rounds.setdefault(c, []).append(e)
    qc = QuantumCircuit(len(phys))
    for _ in range(steps):
        qc.rx(0.3, range(len(phys)))
        for c in sorted(rounds):
            for u, v in rounds[c]:
                qc.rzz(0.2, log[u], log[v])
    return qc


def twoq(qc):
    return sum(1 for inst in qc.data if len(inst.qubits) == 2 and inst.operation.name != "barrier")


def mirror(compiled, target):
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
    m = compiled.copy()
    m.barrier()
    m.compose(compiled.inverse(), inplace=True)
    m.measure_all()
    pm = generate_preset_pass_manager(optimization_level=1, target=target,
                                      initial_layout=list(range(m.num_qubits)))
    return pm.run(m)


def wrong_bits(counts, n):
    shots = sum(counts.values())
    ones = sum(c * k.count("1") for k, c in counts.items())
    return ones / (shots * n), counts.get("0" * n, 0) / shots, shots


# ---------- study ----------
def collect_circuits(backend, n_want, max_candidates):
    """Local only. Returns (records, pubs): pubs alternate stock, package for each record."""
    from qiskit import transpile
    target = backend.target
    hw = chip_nx(target)
    print(f"\n[{backend.name}] {target.num_qubits} qubits | {hw.number_of_edges()} working couplers | "
          f"looking for {n_want} stock misses (local, no IBM time)", flush=True)
    records, pubs = [], []
    for k, (q, s) in enumerate(CANDIDATES[:max_candidates], 1):
        qc = ising_fixed(hw, 2, q, seed=1000 * s + int(q * 100))
        t0 = time.time()
        fb, info = transpile_with_fallback(qc, target, optimization_level=3, seed_transpiler=s)
        miss = info["vf2_stop"] == "NO_SOLUTION_FOUND"
        rescued = miss and info["used"] == "fallback layout"
        tag = "MISS, rescued" if rescued else ("MISS, not rescued" if miss else "stock found it")
        print(f"  {k:2d}. q={q:.2f} seed={s} | {tag} | {time.time() - t0:.1f}s", flush=True)
        if not rescued:
            continue
        stock = transpile(qc, target=target, optimization_level=3, seed_transpiler=s)
        m_stock, m_fb = mirror(stock, target), mirror(fb, target)
        records.append({"backend": backend.name, "q": q, "seed": s,
                        "stock_2q": twoq(stock), "package_2q": twoq(fb),
                        "mirror_stock_2q": twoq(m_stock), "mirror_package_2q": twoq(m_fb),
                        "depth_stock": m_stock.depth(), "depth_package": m_fb.depth(),
                        "stage": info.get("stage")})
        pubs += [m_stock, m_fb]
        print(f"      kept #{len(records)}: 2q stock {twoq(stock)} | package {twoq(fb)} | "
              f"mirror depth {m_stock.depth()} vs {m_fb.depth()}", flush=True)
        if len(records) >= n_want:
            break
    if len(records) < n_want:
        print(f"  only {len(records)} found in {max_candidates} candidates (raise --max-candidates for more)")
    return records, pubs


def remaining_seconds(service):
    try:
        u = service.usage()
        return u.get("usage_remaining_seconds"), u.get("usage_consumed_seconds"), u.get("usage_limit_seconds")
    except Exception:
        return None, None, None


def fetch_results(service, jobs_file, out_dir):
    """Fetch every job in a saved jobs file, write one CSV, print the summary."""
    plan = json.load(open(jobs_file))
    rows = []
    for j in plan["jobs"]:
        print(f"waiting for {j['backend']} repeat {j['repeat']} job {j['job_id']} ...", flush=True)
        job = service.job(j["job_id"])
        res = job.result()
        try:
            secs = job.usage()
        except Exception:
            secs = None
        for i, rec in enumerate(j["records"]):
            n = None
            out = {}
            for name, pub in (("stock", res[2 * i]), ("package", res[2 * i + 1])):
                counts = pub.data.meas.get_counts()
                n = len(next(iter(counts)))
                frac, p0, shots = wrong_bits(counts, n)
                out[name] = frac
            rows.append(dict(rec, repeat=j["repeat"], job_id=j["job_id"], job_seconds=secs,
                             shots=plan["shots"], wrong_stock=out["stock"], wrong_package=out["package"],
                             ratio=(out["package"] / out["stock"]) if out["stock"] > 0 else None))
    stamp = plan["stamp"]
    path = os.path.join(out_dir, f"mirror_study_{stamp}.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    summarize(rows)
    print(f"\nSaved {path}")
    return rows


def _boot_ci(vals, reps=5000, seed=0):
    if len(vals) < 2:
        return None
    r = random.Random(seed)
    means = sorted(statistics.mean(r.choices(vals, k=len(vals))) for _ in range(reps))
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def summarize(rows):
    rs = [r["ratio"] for r in rows if r["ratio"] is not None]
    print("\n=== mirror study summary (ratio = package wrong bits / stock wrong bits; below 1 = package better) ===")
    print(f"{'chip':15s} {'runs':>4s} {'mean':>6s} {'median':>7s} {'best':>6s} {'worst':>6s} "
          f"{'stock %':>8s} {'pkg %':>7s} {'pkg better':>11s}")
    groups = {}
    for r in rows:
        groups.setdefault(r["backend"], []).append(r)
    groups["ALL CHIPS"] = rows
    for name, g in groups.items():
        v = [r["ratio"] for r in g if r["ratio"] is not None]
        if not v:
            continue
        better = sum(1 for r in g if r["wrong_package"] < r["wrong_stock"])
        print(f"{name:15s} {len(v):4d} {statistics.mean(v):6.2f} {statistics.median(v):7.2f} {min(v):6.2f} "
              f"{max(v):6.2f} {100 * statistics.mean(r['wrong_stock'] for r in g):8.2f} "
              f"{100 * statistics.mean(r['wrong_package'] for r in g):7.2f} {better:>5d}/{len(g)}")
    ci = _boot_ci(rs)
    if ci:
        print(f"\n95% interval for the mean ratio (bootstrap over all runs): {ci[0]:.2f} to {ci[1]:.2f}")
    # run-to-run spread (only if repeats > 1)
    pairs = {}
    for r in rows:
        pairs.setdefault((r["backend"], r["q"], r["seed"]), []).append(r["ratio"])
    diffs = [abs(v[0] - v[1]) for v in pairs.values() if len(v) >= 2 and None not in v[:2]]
    if diffs:
        print(f"repeat-to-repeat change in ratio, same circuit and chip: mean {statistics.mean(diffs):.3f}, "
              f"largest {max(diffs):.3f}")
    gr = [r["package_2q"] / r["stock_2q"] for r in rows if r["stock_2q"]]
    if gr:
        print(f"for comparison, two-qubit gate ratio (package / stock): mean {statistics.mean(gr):.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="submit jobs (asks first)")
    ap.add_argument("--yes", action="store_true", help="don't ask before submitting")
    ap.add_argument("--collect", default=None, help="fetch results for a saved mirror_study_jobs_*.json")
    ap.add_argument("--backends", default=DEFAULT_BACKENDS)
    ap.add_argument("--circuits", type=int, default=10, help="stock-miss circuits per chip")
    ap.add_argument("--repeats", type=int, default=1, help="run each chip's job this many times")
    ap.add_argument("--shots", type=int, default=2000)
    ap.add_argument("--max-candidates", type=int, default=40, help="circuits to try per chip")
    ap.add_argument("--keep-seconds", type=float, default=150, help="IBM seconds to leave unused, never spend")
    ap.add_argument("--out", default="results")
    args, _ = ap.parse_known_args()
    os.makedirs(args.out, exist_ok=True)

    import qiskit, qiskit_ibm_runtime
    from qiskit_ibm_runtime import QiskitRuntimeService
    print(f"mirror_study | rozier-piecepack {__version__} | Qiskit {qiskit.__version__} | "
          f"qiskit-ibm-runtime {qiskit_ibm_runtime.__version__}", flush=True)
    try:
        service = QiskitRuntimeService()
    except Exception as e:
        print("Could not load a saved IBM account (see mirror_test.py for the one-time setup).")
        print("  detail:", str(e)[:200])
        return

    if args.collect:
        fetch_results(service, args.collect, args.out)
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    t_start = time.time()
    per_chip = []
    for name in [b.strip() for b in args.backends.split(",") if b.strip()]:
        backend = service.backend(name)
        records, pubs = collect_circuits(backend, args.circuits, args.max_candidates)
        if records:
            per_chip.append((backend, records, pubs))
    print(f"\nlocal search done in {(time.time() - t_start) / 60:.1f} min")
    if not per_chip:
        print("No rescued stock misses found on any chip. Nothing to send.")
        return

    n_pairs = sum(len(r) for _, r, _ in per_chip) * args.repeats
    est = n_pairs * SECONDS_PER_PAIR * args.shots / 2000
    left, used, limit = remaining_seconds(service)
    print("\n=== plan ===")
    for backend, records, _ in per_chip:
        print(f"  {backend.name}: {len(records)} circuits x 2 versions x {args.shots} shots, "
              f"{args.repeats} job(s)")
    print(f"  estimated IBM time: at most about {est:.0f} s (conservative; batched jobs have billed ~1.3 s per pair)")
    if left is not None:
        print(f"  your account: {used} s used of {limit} s, {left} s left; this script keeps "
              f"{args.keep_seconds:.0f} s untouched")
        if est > left - args.keep_seconds:
            print("  STOP: the estimate is more than you can spend. Lower --circuits or --repeats. Nothing sent.")
            return
    else:
        print("  (couldn't read your remaining time; check the IBM page before saying yes)")

    plan_path = os.path.join(args.out, f"mirror_study_plan_{stamp}.json")
    json.dump({"stamp": stamp, "package": __version__, "qiskit": qiskit.__version__, "shots": args.shots,
               "records": [r for _, rs, _ in per_chip for r in rs]}, open(plan_path, "w"), indent=1)
    if not args.run:
        print(f"\nDRY RUN: nothing was sent. Plan saved to {plan_path}. Add --run to submit.")
        return
    if not args.yes:
        ans = input(f"\nSend {len(per_chip) * args.repeats} job(s), about {est:.0f} s of IBM time? Type yes: ")
        if ans.strip().lower() != "yes":
            print("Not sent.")
            return

    from qiskit_ibm_runtime import SamplerV2 as Sampler
    jobs_path = os.path.join(args.out, f"mirror_study_jobs_{stamp}.json")
    saved = {"stamp": stamp, "package": __version__, "qiskit": qiskit.__version__, "shots": args.shots,
             "jobs": []}
    for rep in range(1, args.repeats + 1):
        for backend, records, pubs in per_chip:
            job = Sampler(mode=backend).run(pubs, shots=args.shots)
            saved["jobs"].append({"backend": backend.name, "repeat": rep, "job_id": job.job_id(),
                                  "records": records})
            json.dump(saved, open(jobs_path, "w"), indent=1)   # saved after EVERY submit
            print(f"submitted {backend.name} repeat {rep}: job {job.job_id()}", flush=True)
    print(f"\nJob IDs saved to {jobs_path}")
    print("If this cell gets interrupted, the jobs still run. Fetch them later with:")
    print(f'  %run "{os.path.abspath(__file__)}" --collect "{jobs_path}" --out "{args.out}"\n')
    fetch_results(service, jobs_path, args.out)
    left2, used2, _ = remaining_seconds(service)
    if used2 is not None:
        print(f"IBM time now: {used2} s used, {left2} s left (account totals can lag a few minutes)")


if __name__ == "__main__":
    main()
