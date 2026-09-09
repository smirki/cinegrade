#!/usr/bin/env python3
"""Does the machine still FEEL usable? One number instead of an opinion.

    uv run --project sam python sam/spike/quiet_probe.py --seconds 30

The service's own footprint can be perfect while the Mac is unusable, and the
founder's complaint ("don't run it in a way that slows down my computer") is
about the second thing. Load average is the usual proxy and it is a poor one:
it counts runnable threads, not whether a keystroke is answered.

So this measures, from an ordinary unprivileged process competing for the same
machine, the two things that read as "slow" to a person:

* **scheduler latency**: ask to sleep 100 ms, measure how much longer than
  100 ms it actually took. That overshoot is exactly the delay between an event
  arriving and a thread being run, which is what a stuttering scroll is;
* **CPU service time**: a fixed small numpy matmul, timed. When something else
  owns the cores, the same work takes longer, and the ratio against an idle
  machine is how much slower everything else feels.

Percentiles rather than means, because responsiveness is about the bad moments:
a p50 of 1 ms with a p99 of 900 ms is a machine that feels broken, and a mean
would hide it.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time

import numpy as np

SLEEP_S = 0.1
MATMUL = 220          # a 220x220 float32 matmul: about a millisecond idle


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index] * 1000, 2)        # milliseconds


class Probe(threading.Thread):
    """Run alongside something expensive; `summary()` when it is done."""

    def __init__(self, label: str = "probe") -> None:
        super().__init__(daemon=True, name="quiet-probe")
        self.label = label
        self.stop = threading.Event()
        self.latency: list[float] = []
        self.matmul: list[float] = []
        self.loads: list[list[float]] = []
        self.started_at = None
        self.finished_at = None

    def run(self) -> None:
        left = np.random.rand(MATMUL, MATMUL).astype(np.float32)
        right = np.random.rand(MATMUL, MATMUL).astype(np.float32)
        self.started_at = time.time()
        next_load = 0.0
        while not self.stop.is_set():
            began = time.perf_counter()
            self.stop.wait(SLEEP_S)
            self.latency.append(max(0.0, time.perf_counter() - began - SLEEP_S))
            began = time.perf_counter()
            left @ right
            self.matmul.append(time.perf_counter() - began)
            if time.time() >= next_load:
                try:
                    self.loads.append([round(v, 2) for v in os.getloadavg()])
                except OSError:                                # noqa: BLE001
                    pass
                next_load = time.time() + 5.0
        self.finished_at = time.time()

    def finish(self, timeout: float = 5.0) -> dict:
        self.stop.set()
        self.join(timeout=timeout)
        return self.summary()

    def summary(self) -> dict:
        return {
            "label": self.label,
            "samples": len(self.latency),
            "seconds": round((self.finished_at or time.time())
                             - (self.started_at or time.time()), 1),
            # Milliseconds. The p50 is "how it feels most of the time" and the
            # p99 is "the moments that make a person notice".
            "latency_ms": {"p50": percentile(self.latency, 0.5),
                           "p95": percentile(self.latency, 0.95),
                           "p99": percentile(self.latency, 0.99),
                           "max": percentile(self.latency, 1.0)},
            "matmul_ms": {"p50": percentile(self.matmul, 0.5),
                          "p95": percentile(self.matmul, 0.95),
                          "p99": percentile(self.matmul, 0.99),
                          "max": percentile(self.matmul, 1.0)},
            "load": {"first": self.loads[0] if self.loads else None,
                     "last": self.loads[-1] if self.loads else None,
                     "worst_1m": max((l[0] for l in self.loads), default=None)},
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--label", default="idle")
    parser.add_argument("--json", default=None, help="write the summary here")
    args = parser.parse_args(argv)

    probe = Probe(args.label)
    probe.start()
    deadline = time.time() + args.seconds
    while time.time() < deadline and probe.is_alive():
        time.sleep(0.2)
    summary = probe.finish()
    print(json.dumps(summary, indent=2))
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(summary, handle, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
