#!/usr/bin/env python3
"""Grade one clip end to end through the Fixxr Studio agent API.

Standard library only: this is the proof that an agent needs nothing more
than urllib, json and a subprocess call to ffmpeg (already a studio
requirement, not a new dependency) to steer a real grade. It talks to a
running `studio/server.py` over HTTP exactly the way a browser tab does,
using the same routes documented in studio/README.md under "Agent API".

What it does, in order:

1. GET /api/state for the clip list, the ref list, the parameter defaults
   and (implicitly) confirms the server is reachable and, if a token was
   given, that the token is accepted.
2. Work out a TARGET: the stats (luma percentiles, mean saturation, channel
   means) the grade should end up close to. Three sources, exactly one of
   --ref, --target or --preset-target:
     --preset-target NAME  loads that preset's config (GET /api/preset) and
                            measures it on the clip (POST /api/stats): the
                            target is what that preset actually looks like
                            on THIS footage, not the preset's raw numbers.
     --target FILE.json    a stats dict in the same shape /api/stats
                            returns (or a subset of it: luma.p50, luma.p5,
                            luma.p95, saturation.mean, channels.r/g/b).
                            Missing pieces just skip the rule that needed
                            them.
     --ref NAME             fits a look toward that reference image first
                            (POST /api/match) and applies it (POST
                            /api/session), then measures the reference
                            image ITSELF (GET /api/ref, decoded locally
                            with ffmpeg) as the target for the primaries
                            refinement loop that follows.
3. Loop up to --max-steps times: measure the clip's current stats (POST
   /api/stats), compute a small patch from five deterministic rules over
   PRIMARIES ONLY (exposure, contrast, saturation, temperature, tint), push
   it live with POST /api/session (by=agent, or whatever --by says), and
   measure again. Stops when every tracked metric is within tolerance, or
   when the total error stops improving for two steps running, or at
   --max-steps. Every step is printed as a table: metric, target, current,
   delta, what was actually changed.
4. Saves the result (PUT /api/grade, or POST /api/preset if --save preset
   or if the grade route is not present on the server being tested), then
   reads it back and prints where it landed.

This tool does not touch curves, the secondary, the look choice (except the
one look --ref applies) or any FX: see the "Agent grading loop" entry in
studio/static/limits.js for exactly what that leaves on the table.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# HTTP, stdlib only
# --------------------------------------------------------------------------


class AgentError(Exception):
    """Something the operator can fix: a bad flag, a 4xx from the server."""


def _url(base: str, path: str, params: dict | None = None) -> str:
    url = base.rstrip("/") + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    return url


def request(method: str, base: str, path: str, token: str | None = None,
           params: dict | None = None, body: dict | None = None,
           want_json: bool = True, timeout: float = 60.0):
    """One HTTP call. Raises AgentError with the server's own message on a
    4xx/5xx, because "the server said X" is the whole point of a tool whose
    job is to show every step honestly, including the failing ones.
    """
    url = _url(base, path, params)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        msg = raw.decode("utf-8", "replace")
        try:
            msg = json.loads(msg).get("error", msg)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise AgentError(f"{method} {path} -> HTTP {exc.code}: {msg}") from None
    except urllib.error.URLError as exc:
        raise AgentError(f"{method} {path} -> could not reach {base}: "
                         f"{exc.reason}") from None
    if not want_json:
        return raw
    if not raw:
        return {}
    return json.loads(raw)


# --------------------------------------------------------------------------
# reading a reference image the way an agent has to: over the network, not
# off local disk. GET /api/ref returns a JPEG; ffmpeg (already a studio
# requirement, invoked the same way server.py invokes it) turns that into
# raw pixels, and the small stats function below is a pure Python
# reimplementation of the same handful of numbers server.py's frame_stats()
# computes with numpy, so a reference image and a rendered clip frame are
# measured on the same definitions and can be compared directly.
# --------------------------------------------------------------------------

def measure_rgb24(data: bytes, width: int, height: int) -> dict:
    n = width * height
    if n <= 0 or len(data) < n * 3:
        raise AgentError("decoded reference image is smaller than its own "
                         "reported dimensions")
    sum_r = sum_g = sum_b = 0.0
    sum_sat = 0.0
    luma = [0.0] * n
    for i in range(n):
        o = i * 3
        r, g, b = data[o], data[o + 1], data[o + 2]
        rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
        sum_r += rf
        sum_g += gf
        sum_b += bf
        mx = max(rf, gf, bf)
        mn = min(rf, gf, bf)
        sum_sat += 0.0 if mx <= 1e-6 else (mx - mn) / mx
        luma[i] = 0.2126 * rf + 0.7152 * gf + 0.0722 * bf
    luma.sort()

    def pct(p: float) -> float:
        idx = p / 100.0 * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return luma[lo] + (luma[hi] - luma[lo]) * frac

    return {
        "luma": {"p5": pct(5), "p25": pct(25), "p50": pct(50), "p75": pct(75),
                 "p95": pct(95), "mean": sum(luma) / n},
        "saturation": {"mean": sum_sat / n},
        "channels": {"r": sum_r / n, "g": sum_g / n, "b": sum_b / n},
    }


def measure_reference_image(base: str, token: str | None, name: str,
                            width: int) -> dict:
    jpeg = request("GET", base, "/api/ref", token=token,
                   params={"name": name, "w": width}, want_json=False)
    with tempfile.TemporaryDirectory(prefix="agent_grade_ref_") as tmp:
        jpg_path = Path(tmp) / "ref.jpg"
        jpg_path.write_bytes(jpeg)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json",
             str(jpg_path)],
            capture_output=True, text=True)
        if probe.returncode != 0:
            raise AgentError(f"ffprobe could not read the fetched reference "
                             f"image: {probe.stderr.strip()}")
        dims = json.loads(probe.stdout)["streams"][0]
        w, h = int(dims["width"]), int(dims["height"])
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(jpg_path),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-frames:v", "1", "-"],
            capture_output=True)
        if raw.returncode != 0 or len(raw.stdout) < w * h * 3:
            raise AgentError("ffmpeg could not decode the fetched reference "
                             f"image: {raw.stderr.decode('utf-8', 'replace')}")
    return measure_rgb24(raw.stdout, w, h)


# --------------------------------------------------------------------------
# the five primaries rules
#
# Ranges below are copied from studio/static/schema.js (the primaries
# controls, S(["primaries", ...])), because that is the only place the
# studio states them: /api/state's "defaults" gives the DEFAULT value of
# each knob, not its min/max, and schema.js is a browser file the server
# does not serve as data. Seeds (the starting value of each knob) DO come
# from /api/state at run time, so a future default change is picked up
# without editing this file; only the clamp bounds are a static copy and
# would need updating here if schema.js's ranges ever move.
# --------------------------------------------------------------------------

PRIMARIES_RANGES = {
    "contrast": (0.3, 2.5),
    "saturation": (0.0, 2.5),
    "temperature": (-0.5, 0.5),
    "tint": (-0.5, 0.5),
    "brightness": (-0.5, 0.5),
}

DAMPING = 0.6          # how much of the measured gap to close in one step
EPSILON = 0.002        # a total-error improvement smaller than this is "no progress"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _get(d: dict | None, *path):
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    if isinstance(cur, (int, float)):
        return float(cur)
    return None


class Rule:
    """One knob, one measured gap. `kind` decides how a gap in the metric's
    own units turns into a change to the knob:

    additive - the knob and the metric share units (an offset moves the
               metric by about the same amount), so the new value is just
               the old value plus a damped share of the gap.
    ratio    - the knob is a multiplier (contrast, saturation), so the new
               value is the old value scaled by a damped share of
               (target / current), which is well behaved as long as the
               current measurement is not zero (guarded below).
    """

    def __init__(self, key: str, label: str, cfg_key: str, kind: str,
                current_fn, target_fn, tolerance: float):
        self.key = key
        self.label = label
        self.cfg_key = cfg_key
        self.kind = kind
        self.current_fn = current_fn
        self.target_fn = target_fn
        self.tolerance = tolerance


RULES = [
    Rule("exposure", "median luma (exposure proxy)", "brightness", "additive",
        lambda s: _get(s, "luma", "p50"),
        lambda t: _get(t, "luma", "p50"),
        0.010),
    Rule("contrast", "p5-p95 spread", "contrast", "ratio",
        lambda s: (_get(s, "luma", "p95") - _get(s, "luma", "p5"))
                 if _get(s, "luma", "p95") is not None and _get(s, "luma", "p5") is not None
                 else None,
        lambda t: (_get(t, "luma", "p95") - _get(t, "luma", "p5"))
                 if _get(t, "luma", "p95") is not None and _get(t, "luma", "p5") is not None
                 else None,
        0.020),
    Rule("saturation", "mean saturation", "saturation", "ratio",
        lambda s: _get(s, "saturation", "mean"),
        lambda t: _get(t, "saturation", "mean"),
        0.010),
    Rule("wb_warmth", "red minus blue (warmth)", "temperature", "additive",
        lambda s: (_get(s, "channels", "r") - _get(s, "channels", "b"))
                 if _get(s, "channels", "r") is not None and _get(s, "channels", "b") is not None
                 else None,
        lambda t: (_get(t, "channels", "r") - _get(t, "channels", "b"))
                 if _get(t, "channels", "r") is not None and _get(t, "channels", "b") is not None
                 else None,
        0.010),
    Rule("wb_tint", "green minus red/blue average (tint)", "tint", "additive",
        lambda s: (_get(s, "channels", "g")
                  - (_get(s, "channels", "r") + _get(s, "channels", "b")) / 2.0)
                 if None not in (_get(s, "channels", "r"), _get(s, "channels", "g"),
                                _get(s, "channels", "b"))
                 else None,
        lambda t: (_get(t, "channels", "g")
                  - (_get(t, "channels", "r") + _get(t, "channels", "b")) / 2.0)
                 if None not in (_get(t, "channels", "r"), _get(t, "channels", "g"),
                                _get(t, "channels", "b"))
                 else None,
        0.010),
]


def next_value(rule: Rule, old_value: float, current: float, target: float) -> float:
    lo, hi = PRIMARIES_RANGES[rule.cfg_key]
    if rule.kind == "additive":
        new_value = old_value + DAMPING * (target - current)
    else:  # ratio
        safe_current = current if abs(current) > 1e-4 else (1e-4 if current >= 0 else -1e-4)
        ratio = target / safe_current
        new_value = old_value * (1.0 + DAMPING * (ratio - 1.0))
    return clamp(new_value, lo, hi)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def fmt(x) -> str:
    return "n/a" if x is None else f"{x:+.4f}" if x < 0 else f"{x:.4f}"


def print_table(step: int, rows: list[dict]) -> None:
    print(f"\n-- step {step} --")
    header = f"{'metric':<32} {'target':>10} {'current':>10} {'delta':>10}  patch applied"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['label']:<32} {fmt(row['target']):>10} "
              f"{fmt(row['current']):>10} {fmt(row['delta']):>10}  {row['patch']}")


def run_loop(base: str, token: str, clip: str, time_s: float, width: int,
            by: str, max_steps: int, values: dict, target: dict,
            look_lut: str | None) -> tuple[dict, dict]:
    """Runs the measure/patch/measure loop. Returns (final values, final
    stats). `values` is mutated in place is NOT relied upon: the return
    value is what the caller should trust.
    """
    values = dict(values)
    history: list[float] = []
    no_improve = 0
    last_stats = None

    for step in range(1, max_steps + 1):
        cfg_patch = {"primaries": dict(values)}
        if look_lut is not None:
            cfg_patch["look"] = {"lut": look_lut}
        session = request("POST", base, "/api/session", token=token,
                          body={"clip": clip, "time": time_s,
                                "config": cfg_patch, "by": by})
        stats = request("POST", base, "/api/stats", token=token,
                        body={"clip": clip, "time": time_s, "width": width,
                              "config": cfg_patch})["stats"]
        last_stats = stats

        rows = []
        total_error = 0.0
        tracked = 0
        converged = True
        for rule in RULES:
            current = rule.current_fn(stats)
            tgt = rule.target_fn(target)
            if current is None or tgt is None:
                rows.append({"label": rule.label, "target": tgt,
                            "current": current, "delta": None,
                            "patch": "skipped: no target data"})
                continue
            delta = tgt - current
            tracked += 1
            total_error += abs(delta)
            old_value = values[rule.cfg_key]
            if abs(delta) <= rule.tolerance:
                rows.append({"label": rule.label, "target": tgt,
                            "current": current, "delta": delta,
                            "patch": f"primaries.{rule.cfg_key} "
                                     f"{old_value:.4f} (within tolerance)"})
                continue
            converged = False
            new_value = next_value(rule, old_value, current, tgt)
            values[rule.cfg_key] = new_value
            rows.append({"label": rule.label, "target": tgt,
                        "current": current, "delta": delta,
                        "patch": f"primaries.{rule.cfg_key} "
                                 f"{old_value:.4f} -> {new_value:.4f}"})
        print_table(step, rows)
        print(f"   session rev {session['rev']}, by={session['by']!r}, "
              f"total error {total_error:.4f} over {tracked} tracked metric(s)")

        if tracked == 0:
            print("   stopping: no metric in the target has anything to "
                 "compare against")
            break
        if converged:
            print(f"   converged: every tracked metric is within tolerance "
                 f"after {step} step(s)")
            break
        history.append(total_error)
        if len(history) >= 2 and history[-1] >= history[-2] - EPSILON:
            no_improve += 1
        else:
            no_improve = 0
        if no_improve >= 2:
            print(f"   stopping: total error stalled for 2 steps running "
                 f"({history[-2]:.4f} -> {history[-1]:.4f})")
            break
    else:
        print(f"   stopping: reached --max-steps {max_steps}")

    # One final measurement at the settled values, so what gets saved is
    # exactly what the table's last row claims, not a step behind it.
    cfg_patch = {"primaries": dict(values)}
    if look_lut is not None:
        cfg_patch["look"] = {"lut": look_lut}
    request("POST", base, "/api/session", token=token,
           body={"clip": clip, "time": time_s, "config": cfg_patch, "by": by})
    final_stats = request("POST", base, "/api/stats", token=token,
                          body={"clip": clip, "time": time_s, "width": width,
                                "config": cfg_patch})["stats"]
    return values, final_stats


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------

def target_from_preset(base: str, token: str, preset_name: str, clip: str,
                       time_s: float, width: int) -> dict:
    preset_cfg = request("GET", base, "/api/preset", token=token,
                         params={"name": preset_name})["config"]
    print(f"measuring preset {preset_name!r} on {clip} at {time_s}s to build "
         "the target (POST /api/stats with the preset's own config)...")
    stats = request("POST", base, "/api/stats", token=token,
                    body={"clip": clip, "time": time_s, "width": width,
                          "config": preset_cfg})["stats"]
    return stats


def target_from_file(path: str) -> dict:
    data = json.loads(Path(path).read_text())
    # Accept either a full /api/stats response ({"stats": {...}, ...}) or
    # just the stats dict itself, so a file saved straight from that
    # endpoint's response works without editing.
    return data.get("stats", data) if isinstance(data, dict) else data


def apply_match(base: str, token: str, clip: str, time_s: float,
                ref_name: str) -> str | None:
    print(f"POST /api/match: fitting a look toward ref {ref_name!r}...")
    result = request("POST", base, "/api/match", token=token,
                     body={"ref": ref_name, "clip": clip, "time": time_s})
    for warning in result.get("warnings", []):
        print(f"   warning: {warning}")
    if not result.get("ok"):
        print("   match did not pass its own health check (ok: false); "
             "not applying a look, refining primaries toward the reference "
             "alone")
        return None
    print(f"   applied look {result['name']!r} "
         f"(distance {result.get('distance', {}).get('before', {}).get('total')} "
         f"-> {result.get('distance', {}).get('after', {}).get('total')})")
    request("POST", base, "/api/session", token=token,
           body={"clip": clip, "time": time_s,
                 "config": {"look": {"lut": result["name"]}}, "by": "agent"})
    return result["name"]


# --------------------------------------------------------------------------
# saving
# --------------------------------------------------------------------------

def save_grade(base: str, token: str, clip: str, cfg: dict) -> tuple[str, dict]:
    """Try PUT /api/grade first; fall back to POST /api/preset if that route
    is not on the server being tested (contract C3 landed in the same wave
    as this tool, but a caller may be pointed at an older build).
    """
    try:
        result = request("PUT", base, "/api/grade", token=token,
                         body={"clip": clip, "config": cfg})
        return "grade", result
    except AgentError as exc:
        print(f"PUT /api/grade not usable ({exc}); saving a preset instead")
        return save_preset(base, token, clip, cfg)


def save_preset(base: str, token: str, clip: str, cfg: dict) -> tuple[str, dict]:
    name = f"agent_{Path(clip).stem}"
    result = request("POST", base, "/api/preset", token=token,
                     body={"name": name, "config": cfg,
                           "comment": f"agent_grade.py toward its target for {clip}"})
    return "preset", result


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip", help="clip name, as it appears in GET /api/clips")
    target_group = ap.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--ref", metavar="NAME",
                              help="reference image name from GET /api/state's "
                                   "refs list; fits and applies a look first, "
                                   "then refines primaries toward it")
    target_group.add_argument("--target", metavar="FILE.json",
                              help="a stats dict (the shape POST /api/stats "
                                   "returns) to grade toward")
    target_group.add_argument("--preset-target", metavar="NAME",
                              help="measure this preset's own stats on the "
                                   "clip and grade toward those numbers")
    ap.add_argument("--time", type=float, default=1.0)
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--base-url", default="http://127.0.0.1:7431")
    ap.add_argument("--token", default=None,
                    help="Authorization: Bearer token from POST /api/auth/token; "
                         "unused (and unneeded) when the server has no --auth")
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--save", choices=["grade", "preset", "none"], default="grade")
    ap.add_argument("--by", default="agent",
                    help="the `by` field on every /api/session patch, so a "
                         "browser watching session/wait can tell this apart "
                         "from a human dragging a slider")
    args = ap.parse_args()

    print(f"GET /api/state ({args.base_url})...")
    state = request("GET", args.base_url, "/api/state", token=args.token)
    clip_names = {c["name"] for c in state["clips"]}
    if args.clip not in clip_names:
        raise AgentError(f"{args.clip!r} is not in GET /api/clips: "
                         f"{sorted(clip_names)}")
    seed = state["defaults"]["primaries"]
    values = {k: float(seed[k]) for k in PRIMARIES_RANGES}
    print(f"clip {args.clip!r} confirmed, seeding primaries from "
         f"/api/state defaults: {values}")

    look_lut = None
    if args.ref:
        ref_names = {r["name"] for r in state["refs"]}
        if args.ref not in ref_names:
            raise AgentError(f"{args.ref!r} is not in the refs list: "
                             f"{sorted(ref_names)}")
        look_lut = apply_match(args.base_url, args.token, args.clip, args.time,
                               args.ref)
        print(f"measuring reference image {args.ref!r} itself, over "
             "GET /api/ref, decoded locally with ffmpeg, as the target for "
             "the primaries refinement loop...")
        target = measure_reference_image(args.base_url, args.token, args.ref,
                                         args.width)
    elif args.preset_target:
        target = target_from_preset(args.base_url, args.token,
                                    args.preset_target, args.clip, args.time,
                                    args.width)
    else:
        target = target_from_file(args.target)

    print(f"target: luma p5/p50/p95 = "
         f"{_get(target,'luma','p5')}, {_get(target,'luma','p50')}, "
         f"{_get(target,'luma','p95')}; saturation mean = "
         f"{_get(target,'saturation','mean')}; channels r/g/b = "
         f"{_get(target,'channels','r')}, {_get(target,'channels','g')}, "
         f"{_get(target,'channels','b')}")

    final_values, final_stats = run_loop(
        args.base_url, args.token, args.clip, args.time, args.width, args.by,
        args.max_steps, values, target, look_lut)

    final_cfg = {"primaries": final_values}
    if look_lut is not None:
        final_cfg["look"] = {"lut": look_lut}

    print(f"\nfinal primaries: {final_values}"
         + (f", look.lut={look_lut!r}" if look_lut else ""))
    print(f"final stats: {json.dumps(final_stats, indent=2)}")

    if args.save == "none":
        print("\n--save none: not saving")
    else:
        kind = "grade" if args.save == "grade" else "preset"
        if kind == "grade":
            saved_kind, result = save_grade(args.base_url, args.token,
                                            args.clip, final_cfg)
        else:
            saved_kind, result = save_preset(args.base_url, args.token,
                                             args.clip, final_cfg)
        if saved_kind == "grade":
            print(f"\nsaved via PUT /api/grade: key={result['key']} "
                 f"updated_at={result['updated_at']}")
            check = request("GET", args.base_url, "/api/grade", token=args.token,
                            params={"clip": args.clip})
            print(f"verified with GET /api/grade?clip={args.clip}: "
                 f"exists={check['exists']} updated_at={check['updated_at']}")
        else:
            print(f"\nsaved via POST /api/preset: {result['saved']} at "
                 f"{result['path']}")
            check = request("GET", args.base_url, "/api/preset", token=args.token,
                            params={"name": Path(result["saved"]).stem})
            print(f"verified with GET /api/preset?name="
                 f"{Path(result['saved']).stem}: config present="
                 f"{check.get('config') is not None}")

    session = request("GET", args.base_url, "/api/session", token=args.token)
    print(f"\nGET /api/session: rev={session['rev']} by={session['by']!r} "
         f"clip={session['clip']!r} time={session['time']}")


if __name__ == "__main__":
    try:
        main()
    except AgentError as exc:
        sys.exit(f"agent_grade.py: {exc}")
