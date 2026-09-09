#!/usr/bin/env python3
"""The real model check for quiet mode: three launchers, one clip, one table.

    uv run --project sam python sam/spike/quiet_measure.py --runs plain,quiet
    uv run --project sam python sam/spike/quiet_measure.py --runs taskpolicy \
        --proxy sam/spike/out/quiet-<stamp>/proxy.mp4

Bounded on purpose, because this is minutes of real GPU on the founder's own
machine: two windows, 720 wide, one hard timeout per run, and the proxy is built
once and reused so every run tracks the identical pictures (which is what makes
a byte comparison of the mattes mean anything).

Each run is `spike/track_memory.py` as a subprocess, so the model lock is taken
and released by that script the way the service takes it, and each service is
stopped by the pid that started it. While a run happens, `quiet_probe.Probe`
measures what the rest of the machine is getting: scheduler latency and CPU
service time, in percentiles.

The three launchers:

  plain       the service as it has always run: no duty cycle, no nice
  quiet       `--quiet`: duty cycle 0.5, nice 15, MLX memory limit 6144 MB
  taskpolicy  `--quiet` plus `taskpolicy -b`, the macOS background QoS tier

`plain` is also the reference the mattes are compared against: quiet mode
inserts sleeps and changes scheduling priority, and neither can change an
arithmetic result, so anything other than byte identical mattes is a bug.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

SAM = Path(__file__).resolve().parent.parent
ROOT = SAM.parent
if str(SAM) not in sys.path:
    sys.path.insert(0, str(SAM))
sys.path.insert(0, str(SAM / "spike"))

from quiet_probe import Probe                                      # noqa: E402
from track_memory import CLIP, load_average, make_proxy            # noqa: E402

PYTHON = SAM / ".venv" / "bin" / "python"
OUT = SAM / "spike" / "out"

RUNS = {
    "plain": [],
    "quiet": ["--quiet"],
    "taskpolicy": ["--quiet", "--taskpolicy"],
}


def say(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def matte_hashes(run_dir: Path) -> dict[str, str]:
    """sha256 per PNG matte frame, keyed by `<object_id>/<frame>.png`.

    NOT by the path, which is the mistake this function was written wrong once
    already: a matte's directory is `m_<digest of the recipe AND the clip key>`,
    and the clip key carries the run's own timestamp, so two runs of identical
    work land in differently named directories and a path keyed comparison finds
    nothing in common and cheerfully reports 0 of 0 identical. The object id and
    the frame number are what actually name the same picture across two runs.
    """
    out: dict[str, str] = {}
    mattes = run_dir / "mattes"
    if not mattes.is_dir():
        return out
    for matte in sorted(p for p in mattes.iterdir() if p.is_dir()):
        object_id = matte.name
        index = matte / "index.json"
        if index.is_file():
            try:
                object_id = json.loads(index.read_text()).get("object_id") \
                    or matte.name
            except (json.JSONDecodeError, OSError):
                pass
        for path in sorted(matte.glob("*.png")):
            out[f"{object_id}/{path.name}"] = \
                hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def one_run(name: str, args, proxy: Path) -> dict:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = f"quiet-{name}"
    command = [str(PYTHON), str(SAM / "spike" / "track_memory.py"),
               "--tag", tag, "--proxy", str(proxy),
               "--width", str(args.width),
               "--chunk-frames", str(args.chunk_frames),
               "--windows", str(args.windows),
               "--timeout-s", str(args.job_timeout_s),
               "--ready-s", str(args.ready_s)] + RUNS[name]
    say(f"=== {name}: " + " ".join(RUNS[name] or ["(no flags)"]))
    say("  " + " ".join(command))
    probe = Probe(name)
    probe.start()
    began = time.time()
    try:
        done = subprocess.run(command, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=args.wall_timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        # The child is already killed by subprocess. Say so loudly: the model
        # lock may need a look, because a killed process runs no `finally`.
        done = subprocess.CompletedProcess(command, -1,
                                           (exc.stdout or b"").decode() if
                                           isinstance(exc.stdout, bytes)
                                           else (exc.stdout or ""),
                                           (exc.stderr or b"").decode() if
                                           isinstance(exc.stderr, bytes)
                                           else (exc.stderr or ""))
        timed_out = True
    wall = time.time() - began
    responsiveness = probe.finish()
    print(done.stdout[-4000:] if done.stdout else "")
    if done.returncode != 0:
        print((done.stderr or "")[-2000:])
    # track_memory writes to out/track-memory-<tag>-<stamp>; find the newest.
    candidates = sorted(OUT.glob(f"track-memory-{tag}-*"),
                        key=lambda p: p.stat().st_mtime)
    run_dir = candidates[-1] if candidates else None
    result = {}
    if run_dir is not None and (run_dir / "result.json").is_file():
        result = json.loads((run_dir / "result.json").read_text())
    return {"name": name, "flags": RUNS[name], "stamp": stamp,
            "exit": done.returncode, "timed_out": timed_out,
            "wall_s": round(wall, 1), "run_dir": str(run_dir) if run_dir else None,
            "result": result, "responsiveness": responsiveness,
            "hashes": matte_hashes(run_dir) if run_dir else {}}


def table(rows: list[dict]) -> str:
    head = (f"{'launcher':>11}  {'frames':>6}  {'s/frame busy':>12}  "
            f"{'busy frac':>9}  {'wall s':>7}  {'top MEM':>8}  {'CMPRS':>7}  "
            f"{'proc peak':>9}  {'lat p50':>8}  {'lat p99':>8}  {'load 1m':>7}")
    lines = [head, "-" * len(head)]
    for row in rows:
        result = row.get("result") or {}
        quiet = result.get("throttle_final") or {}
        job = result.get("job") or {}
        after = (result.get("after_job") or {}).get("top") or {}
        peaks = [r.get("process_peak_mb") for r in (result.get("rows") or [])
                 if r.get("process_peak_mb") is not None]
        resp = row.get("responsiveness") or {}

        def show(value, unit=""):
            return "?" if value is None else f"{value}{unit}"

        lines.append(
            f"{row['name']:>11}  "
            f"{show(job.get('done_frames')):>6}  "
            f"{show(result.get('s_per_frame_busy')):>12}  "
            f"{show(quiet.get('busy_fraction')):>9}  "
            f"{show(row.get('wall_s')):>7}  "
            f"{show(after.get('mem_mb')):>8}  "
            f"{show(after.get('cmprs_mb')):>7}  "
            f"{show(max(peaks) if peaks else None):>9}  "
            f"{show((resp.get('latency_ms') or {}).get('p50')):>8}  "
            f"{show((resp.get('latency_ms') or {}).get('p99')):>8}  "
            f"{show((resp.get('load') or {}).get('worst_1m')):>7}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", default="plain,quiet,taskpolicy",
                        help="comma separated: " + ", ".join(RUNS))
    parser.add_argument("--clip", default=str(CLIP))
    parser.add_argument("--proxy", default=None,
                        help="reuse a proxy from an earlier run. STRONGLY "
                             "preferred across runs: identical pictures are "
                             "what make the matte comparison mean anything.")
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--chunk-frames", type=int, default=6)
    parser.add_argument("--windows", type=int, default=2)
    parser.add_argument("--job-timeout-s", type=float, default=480.0,
                        help="track_memory's own bounded wait, which unwinds "
                             "cleanly and releases the model lock")
    parser.add_argument("--wall-timeout-s", type=float, default=900.0,
                        help="the outer last resort. Reaching it KILLS the "
                             "child, which runs no finally, so check "
                             "/tmp/fixxr-sam-model.lock afterwards.")
    parser.add_argument("--ready-s", type=float, default=600.0)
    parser.add_argument("--reference", default=None,
                        help="an earlier run directory whose mattes are the "
                             "byte comparison reference, for when `plain` was "
                             "measured in a previous session and its GPU time "
                             "should not be spent again")
    parser.add_argument("--idle-probe-s", type=float, default=15.0,
                        help="measure the machine with no model running at "
                             "all first, as the baseline every run is read "
                             "against. 0 skips it.")
    args = parser.parse_args(argv)

    names = [n.strip() for n in args.runs.split(",") if n.strip()]
    unknown = [n for n in names if n not in RUNS]
    if unknown:
        raise SystemExit(f"unknown run(s) {unknown}; known: {', '.join(RUNS)}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    session = OUT / f"quiet-{stamp}"
    session.mkdir(parents=True, exist_ok=True)

    total_frames = (args.chunk_frames - 1) * args.windows + 1
    if args.proxy:
        proxy = Path(args.proxy)
        if not proxy.is_file():
            raise SystemExit(f"there is no proxy at {proxy}")
    else:
        proxy = make_proxy(Path(args.clip), session / "proxy.mp4", args.width,
                           total_frames)

    out: dict = {"stamp": stamp, "runs": names, "proxy": str(proxy),
                 "width": args.width, "chunk_frames": args.chunk_frames,
                 "windows": args.windows, "total_frames": total_frames,
                 "load_before": load_average()}
    if args.idle_probe_s > 0:
        say(f"measuring the machine with no model running, {args.idle_probe_s}s")
        idle = Probe("idle")
        idle.start()
        time.sleep(args.idle_probe_s)
        out["idle"] = idle.finish()
        say(f"idle: latency p50 {out['idle']['latency_ms']['p50']} ms, "
            f"p99 {out['idle']['latency_ms']['p99']} ms, "
            f"matmul p50 {out['idle']['matmul_ms']['p50']} ms, "
            f"load {out['idle']['load']['last']}")

    rows = []
    for name in names:
        rows.append(one_run(name, args, proxy))
        (session / "runs.json").write_text(json.dumps(rows, indent=2, default=str))
        say(f"{name} finished, exit {rows[-1]['exit']}, "
            f"wall {rows[-1]['wall_s']}s")
    out["rows"] = rows
    out["load_after"] = load_average()

    print()
    print(f"{total_frames} frames of {Path(args.clip).name} through "
          f"{proxy.name} at {args.width} wide, windows of {args.chunk_frames}")
    if out.get("idle"):
        print(f"machine with NO model running: latency p50 "
              f"{out['idle']['latency_ms']['p50']} ms, p99 "
              f"{out['idle']['latency_ms']['p99']} ms, matmul p50 "
              f"{out['idle']['matmul_ms']['p50']} ms, load "
              f"{out['idle']['load']['last']}")
    print()
    print(table(rows))
    print()

    # The correctness claim, measured rather than argued.
    reference = next((r for r in rows if r["name"] == "plain"), None)
    if reference is None and args.reference:
        ref_dir = Path(args.reference)
        reference = {"name": f"reference {ref_dir.name}",
                     "hashes": matte_hashes(ref_dir)}
        out["reference"] = str(ref_dir)
        print(f"matte reference: {ref_dir} ({len(reference['hashes'])} mattes)")
    comparisons = []
    if reference and reference["hashes"]:
        for row in rows:
            if row is reference or not row["hashes"]:
                continue
            shared = sorted(set(reference["hashes"]) & set(row["hashes"]))
            same = [k for k in shared if reference["hashes"][k] == row["hashes"][k]]
            differ = [k for k in shared if reference["hashes"][k] != row["hashes"][k]]
            comparisons.append({"against": row["name"], "compared": len(shared),
                                "identical": len(same), "differing": differ,
                                "only_in_reference":
                                    sorted(set(reference["hashes"]) - set(row["hashes"])),
                                "only_in_other":
                                    sorted(set(row["hashes"]) - set(reference["hashes"]))})
            print(f"mattes, {reference['name']} against {row['name']}: "
                  f"{len(same)}/{len(shared)} "
                  f"byte identical" + (f", DIFFERING: {differ}" if differ else ""))
    else:
        print("no `plain` run in this session, so the mattes were not compared. "
              "Run with --runs plain,... to get the correctness claim.")
    out["matte_comparison"] = comparisons
    (session / "summary.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwritten: {session / 'summary.json'}")
    return 0 if all(r["exit"] == 0 for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
