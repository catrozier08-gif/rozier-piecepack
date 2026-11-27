"""
mirror_test.py: do fewer two-qubit gates mean less noise ON A REAL CHIP? (one circuit, one chip)
For many circuits and chips with error bars, use mirror_study.py.

1. On the real backend's own coupling map, find a circuit (Ising Trotter on a fixed coupler subset, shuffled
   qubit numbering) where stock level 3 MISSES the zero-swap layout and rozier-piecepack finds it. This part
   runs on your computer and costs no IBM time.
2. Compile it two ways: stock level 3, and transpile_with_fallback (zero swaps).
3. Make each a MIRROR: the compiled circuit, a barrier, then its exact inverse. The ideal result is all zeros;
   every 1 measured is an error. (The barrier stops the optimizer from cancelling the mirror away.)
4. Run both in ONE job and compare the fraction of wrong bits.

By default it only PREPARES (no job is sent). Add --run to submit.

  %run "<package>\\examples\\mirror_test.py"                       # dry run: backend, circuit, gate counts
  %run "<package>\\examples\\mirror_test.py" --run                 # submit (one job, 2 circuits x 2000 shots)
  ... --backend ibm_kingston --shots 2000 --out "C:\\ROZIER_WORKSHOP\\bench_results"

Needs an IBM Quantum account saved ONCE on this computer (never put the key in a notebook you keep):
  from qiskit_ibm_runtime import QiskitRuntimeService
  QiskitRuntimeService.save_account(channel="ibm_quantum_platform", token="YOUR_KEY",
                                    instance="YOUR_CRN", set_as_default=True, overwrite=True)
Run that in a scratch cell, then delete the cell.
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
import argparse, json, os, random, time
from datetime import datetime

import networkx as nx

from rozier_piecepack import __version__, transpile_with_fallback

CANDIDATES = [(0.78, 2), (0.80, 2), (0.80, 3), (0.75, 1), (0.78, 3), (0.82, 3), (0.85, 1), (0.70, 3),
              (0.78, 1), (0.80, 1), (0.82, 1), (0.75, 2), (0.85, 2), (0.82, 2)]


def chip_nx(target):
    g = nx.Graph()
    g.add_nodes_from(range(target.num_qubits))
    g.add_edges_from((a, b) for a, b in target.build_coupling_map().get_edges() if a != b)
    return g


def ising_fixed(hw, steps, q, seed):
    """Same builder as boundary_repro.py, shuffled numbering."""
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
    """compiled + barrier + compiled^-1 + measure, re-expressed in the backend's basis WITHOUT moving qubits."""
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
    m = compiled.copy()
    m.barrier()
    m.compose(compiled.inverse(), inplace=True)
    m.measure_all()
    pm = generate_preset_pass_manager(optimization_level=1, target=target,
                                      initial_layout=list(range(m.num_qubits)))
    out = pm.run(m)
    return out


def wrong_bits(counts, n):
    """Fraction of measured bits that are 1 (ideal: 0), and the all-zero probability."""
    shots = sum(counts.values())
    ones = sum(c * k.count("1") for k, c in counts.items())
    zero = counts.get("0" * n, 0)
    return ones / (shots * n), zero / shots, shots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="actually submit the job (uses IBM time)")
    ap.add_argument("--backend", default=None, help="backend name; default: least busy with 156 qubits")
    ap.add_argument("--shots", type=int, default=2000)
    ap.add_argument("--max-candidates", type=int, default=10, help="how many circuits to try before giving up")
    ap.add_argument("--out", default="results")
    args, _ = ap.parse_known_args()
    os.makedirs(args.out, exist_ok=True)

    import qiskit, qiskit_ibm_runtime
    from qiskit import transpile
    from qiskit_ibm_runtime import QiskitRuntimeService
    print(f"mirror_test | rozier-piecepack {__version__} | Qiskit {qiskit.__version__} | "
          f"qiskit-ibm-runtime {qiskit_ibm_runtime.__version__}", flush=True)
    try:
        service = QiskitRuntimeService()
    except Exception as e:
        print("Could not load a saved IBM account. Save it once (see the top of this file), then run again.")
        print("  detail:", str(e)[:200])
        return
    if args.backend:
        backend = service.backend(args.backend)
    else:
        cands = service.backends(min_num_qubits=156, operational=True, simulator=False)
        print("156-qubit backends available:", ", ".join(b.name for b in cands) or "none")
        if not cands:
            print("No 156-qubit backend available on this account. Try --backend with a name from your account.")
            return
        backend = service.least_busy(min_num_qubits=156, operational=True, simulator=False)
    target = backend.target
    hw = chip_nx(target)
    print(f"backend: {backend.name} | {target.num_qubits} qubits | {hw.number_of_edges()} working couplers", flush=True)

    # 1. find a stock miss that the package rescues (local compute only)
    chosen = None
    for k, (q, s) in enumerate(CANDIDATES[:args.max_candidates], 1):
        qc = ising_fixed(hw, 2, q, seed=1000 * s + int(q * 100))
        t0 = time.time()
        fb, info = transpile_with_fallback(qc, target, optimization_level=3, seed_transpiler=s)
        print(f"  candidate {k}: q={q:.2f} seed={s} | stock VF2Layout {info['vf2_stop']} | search {info['search']} "
              f"({info.get('stage') or '-'}) | {time.time() - t0:.1f}s", flush=True)
        if info["vf2_stop"] == "NO_SOLUTION_FOUND" and info["used"] == "fallback layout":
            chosen = (q, s, qc, fb, info)
            break
    if chosen is None:
        print("No candidate where stock misses and the package rescues it on this backend's map. "
              "Try --max-candidates 14 or another backend. Nothing was sent.")
        return
    q, s, qc, fb, info = chosen

    # 2. stock compile of the same circuit
    t0 = time.time()
    stock = transpile(qc, target=target, optimization_level=3, seed_transpiler=s)
    print(f"stock compile {time.time() - t0:.1f}s", flush=True)

    # 3. mirrors
    m_stock, m_fb = mirror(stock, target), mirror(fb, target)
    print(f"\nchosen: q={q:.2f} seed={s} on {backend.name}")
    print(f"  forward circuit 2q gates: stock {twoq(stock)} | package {twoq(fb)}")
    print(f"  mirror (forward + inverse) 2q gates: stock {twoq(m_stock)} | package {twoq(m_fb)} | "
          f"depth stock {m_stock.depth()} | package {m_fb.depth()}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    record = {"backend": backend.name, "q": q, "seed": s, "shots": args.shots, "package": __version__,
              "qiskit": qiskit.__version__, "stock_2q": twoq(stock), "package_2q": twoq(fb),
              "mirror_2q": {"stock": twoq(m_stock), "package": twoq(m_fb)}, "stage": info.get("stage")}
    if not args.run:
        print("\nDRY RUN: nothing was sent to IBM. Add --run to submit one job (2 circuits x "
              f"{args.shots} shots).")
        json.dump(record, open(os.path.join(args.out, f"mirror_dryrun_{stamp}.json"), "w"), indent=1)
        return

    # 4. one job, both circuits
    from qiskit_ibm_runtime import SamplerV2 as Sampler
    sampler = Sampler(mode=backend)
    job = sampler.run([m_stock, m_fb], shots=args.shots)
    print(f"\nsubmitted job {job.job_id()} - waiting for the result (the queue can take a while)...", flush=True)
    res = job.result()
    n = m_stock.num_qubits
    out = {}
    for name, pub in (("stock", res[0]), ("package", res[1])):
        counts = pub.data.meas.get_counts()
        frac, p0, shots = wrong_bits(counts, n)
        out[name] = {"wrong_bit_fraction": frac, "all_zero_prob": p0, "shots": shots}
    usage = None
    try:
        usage = job.usage()          # quantum seconds billed for this job
    except Exception:
        try:
            usage = job.metrics().get("usage", {}).get("quantum_seconds")
        except Exception:
            pass
    record.update(job_id=job.job_id(), results=out, quantum_seconds=usage)
    path = os.path.join(args.out, f"mirror_result_{stamp}.json")
    json.dump(record, open(path, "w"), indent=1)

    a, b = out["stock"]["wrong_bit_fraction"], out["package"]["wrong_bit_fraction"]
    print(f"\n=== mirror test on {backend.name} (ideal: every bit 0) ===")
    print(f"  stock   ({twoq(m_stock)} two-qubit gates): wrong bits {a:.4f} ({a * 100:.2f}%)")
    print(f"  package ({twoq(m_fb)} two-qubit gates): wrong bits {b:.4f} ({b * 100:.2f}%)")
    if a > 0:
        print(f"  package / stock wrong-bit ratio: {b / a:.2f}")
    print(f"  IBM time used: {f'{usage} s' if usage is not None else 'not reported yet (see the job page)'}")
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
