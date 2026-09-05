#!/usr/bin/env python3
"""W2d cache-prune race proof: before (raises) and after (tolerant).

Two things share studio/cache in the real deployment this arc is building
toward: two server processes on two ports, or just two threads inside one
process (the exact case a ThreadingHTTPServer already has today). Either way
one of them can unlink a cache file between another one's path.exists() and
its read of that same path. This script recreates that race directly against
the real cache directory and real cache functions, with real ffmpeg decodes,
rather than hoping two live server processes happen to collide in wall clock
time.

Two things are proven:
  1. _prune_cache's own glob-then-stat listing, hammered by a concurrent
     "another process" deleting files out of the same folder while it runs.
  2. source_frame()'s disk cache read, hammered by a concurrent deleter
     removing the exact file it is about to read, with the in-memory LRU
     cleared before every call so the disk path actually runs every time
     (a second real process would have its own empty in-memory cache, so
     this is what "two processes" looks like from source_frame's own view).

"before" reruns each hammer against a verbatim copy of the pre-fix code
(pasted below from the original file, not reconstructed from memory) so the
comparison is apples to apples on the same machine, same clip, same load.
"after" calls the real, currently shipped functions with no monkeypatching.
"""
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

STUDIO = Path(__file__).resolve().parent.parent  # content/studio
sys.path.insert(0, str(STUDIO))
import server as S  # noqa: E402

CLIP = "A001_09011336_C002.MOV"
WIDTH = 160
AUTOROTATE = True
DURATION_S = float(3.0)


# --------------------------------------------------------------------------
# 1. _prune_cache: verbatim pre-fix version vs. the real, fixed one.
# --------------------------------------------------------------------------

def prune_cache_before(kind, max_files):
    """Copied verbatim from server.py before this lane's fix."""
    d = S.CACHE / kind
    files = sorted(d.glob("*"), key=lambda p: p.stat().st_mtime)
    for p in files[:-max_files]:
        try:
            p.unlink()
        except OSError:
            pass


def hammer_prune(prune_fn, seconds, errors, tag):
    kind = "w2d_race_prune"
    d = S.CACHE / kind
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for i in range(400):
        (d / f"f{i}.bin").write_bytes(b"x")

    stop = threading.Event()

    def deleter():
        # The "other process": deletes files out of the same folder as fast
        # as it can, exactly what a concurrent prune (or a second server's
        # own prune) does to this one's listing.
        while not stop.is_set():
            for p in list(d.glob("*"))[:20]:
                try:
                    p.unlink()
                except OSError:
                    pass

    def refiller():
        # Keeps the folder non-empty so the pruner under test has something
        # to list on every pass, instead of racing an empty directory.
        n = 0
        while not stop.is_set():
            try:
                (d / f"r{n}.bin").write_bytes(b"x")
            except OSError:
                pass
            n += 1

    threads = [threading.Thread(target=deleter), threading.Thread(target=refiller)]
    for t in threads:
        t.start()

    end = time.time() + seconds
    calls = 0
    while time.time() < end:
        try:
            prune_fn(kind, 50)
            calls += 1
        except Exception as exc:                              # noqa: BLE001
            errors.append((tag, type(exc).__name__, str(exc)))
    stop.set()
    for t in threads:
        t.join()
    shutil.rmtree(d, ignore_errors=True)
    return calls


# --------------------------------------------------------------------------
# 2. source_frame: verbatim pre-fix disk-read branch vs. the real, fixed one.
# --------------------------------------------------------------------------

def source_frame_before(clip, time_s, width, autorotate):
    """Copied verbatim from server.py before this lane's fix (the disk
    branch only; the in-memory LRU check ahead of it is left in place
    below, mirroring how the real function is structured).
    """
    info = S.clip_info(clip, autorotate)
    width, height = S._preview_dims(info, width)
    matrix = S.CG.source_matrix(info)
    mem_key = f"{clip}|{round(float(time_s), 4)}|{width}|{autorotate}|{matrix}"
    disk_key = S.hashlib.sha1(mem_key.encode()).hexdigest()
    disk_path = S._cache_path("src", disk_key, "rgb48")
    want = width * height * 3 * 2

    if disk_path.exists():
        S.os.utime(disk_path, None)
        data = disk_path.read_bytes()               # <-- can raise here
    else:
        vf = (f"scale={width}:{height}:flags=bilinear,setsar=1,"
              f"scale=in_color_matrix={matrix}:in_range={info['color_range']}"
              f":out_range=full,format=gbrp16le")
        args = ["ffmpeg", "-v", "error", "-y"]
        if not autorotate:
            args += ["-noautorotate"]
        args += ["-ss", str(time_s), "-i", str(S.clip_path(clip)),
                 "-vf", vf, "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "rgb48le", "-"]
        with S.FFMPEG_SLOTS:
            proc = S.subprocess.run(args, capture_output=True)
        if proc.returncode != 0 or len(proc.stdout) < want:
            raise S.StudioError("decode failed")
        data = proc.stdout[:want]
        disk_path.write_bytes(data)
    return data


def hammer_source_frame(read_fn, seconds, errors, tag):
    # Clean slate: a previous run's deleter thread can leave a corrupt,
    # truncated file behind (stopped mid write), which is not the race this
    # is testing, just leftover mess from testing it a moment ago.
    with S._src_mem_lock:
        S._src_mem.clear()
    for p in (S.CACHE / "src").glob("*"):
        p.unlink(missing_ok=True)
    # Warm one real disk cache entry with an actual ffmpeg decode.
    S.source_frame(CLIP, 1.0, WIDTH, AUTOROTATE)
    info = S.clip_info(CLIP, AUTOROTATE)
    width, _height = S._preview_dims(info, WIDTH)
    matrix = S.CG.source_matrix(info)
    mem_key = f"{CLIP}|{round(1.0, 4)}|{width}|{AUTOROTATE}|{matrix}"
    disk_key = S.hashlib.sha1(mem_key.encode()).hexdigest()
    disk_path = S._cache_path("src", disk_key, "rgb48")
    assert disk_path.exists(), "warm-up did not create the disk cache entry"
    good_bytes = disk_path.read_bytes()  # the real, valid cached frame

    stop = threading.Event()
    tmp_path = disk_path.with_suffix(disk_path.suffix + ".raceset")

    def deleter():
        # The "other process": deletes and instantly re-creates the exact
        # file the reader threads are about to read, so exists() and the
        # read below it disagree as often as physically possible. Recreated
        # through a write-then-rename, same as this lane's own upload and
        # segment writes: a plain write_bytes() straight to disk_path is not
        # atomic (it truncates before it writes), so a reader could catch it
        # mid-write and see a torn, zero-length file. That would be a second,
        # different bug (a torn cache write) and this test is isolating one
        # specific bug (a vanished file raising FileNotFoundError), so the
        # recreate step here has to be at least as atomic as production
        # writes are, or the two would be impossible to tell apart.
        while not stop.is_set():
            try:
                disk_path.unlink()
            except OSError:
                pass
            try:
                tmp_path.write_bytes(good_bytes)
                tmp_path.replace(disk_path)
            except OSError:
                pass

    t = threading.Thread(target=deleter)
    t.start()

    calls = [0]
    lock = threading.Lock()

    def reader():
        end = time.time() + seconds
        while time.time() < end:
            # Clear the in-memory LRU before every call: a second real
            # process sharing this cache folder has its own, always-cold
            # in-memory cache, so from source_frame's point of view every
            # one of its requests takes the disk branch, same as this does.
            with S._src_mem_lock:
                S._src_mem.clear()
            try:
                read_fn(CLIP, 1.0, WIDTH, AUTOROTATE)
                with lock:
                    calls[0] += 1
            except Exception as exc:                          # noqa: BLE001
                errors.append((tag, type(exc).__name__, str(exc)))

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for r in readers:
        r.start()
    for r in readers:
        r.join()
    stop.set()
    t.join()
    return calls[0]


def main():
    print(f"clip: {CLIP}")
    print()

    print("== 1. _prune_cache under a concurrent deleter ==")
    before_errors = []
    before_calls = hammer_prune(prune_cache_before, DURATION_S, before_errors, "before")
    print(f"before (pre-fix code): {before_calls} prune calls, "
          f"{len(before_errors)} raised")
    for tag, exc_type, msg in before_errors[:3]:
        print(f"  e.g. {exc_type}: {msg}")

    after_errors = []
    after_calls = hammer_prune(S._prune_cache, DURATION_S, after_errors, "after")
    print(f"after  (shipped code):  {after_calls} prune calls, "
          f"{len(after_errors)} raised")
    print()

    print("== 2. source_frame's disk cache read under a concurrent deleter ==")
    before_errors2 = []
    before_reads = hammer_source_frame(source_frame_before, DURATION_S,
                                       before_errors2, "before")
    print(f"before (pre-fix code): {before_reads} reads completed, "
          f"{len(before_errors2)} raised")
    for tag, exc_type, msg in before_errors2[:3]:
        print(f"  e.g. {exc_type}: {msg}")

    after_errors2 = []
    after_reads = hammer_source_frame(S.source_frame, DURATION_S,
                                      after_errors2, "after")
    print(f"after  (shipped code):  {after_reads} reads completed, "
          f"{len(after_errors2)} raised")
    from collections import Counter
    print("  after error types:", Counter(t for _, t, _ in after_errors2))
    for tag, exc_type, msg in after_errors2[:3]:
        print(f"  e.g. {exc_type}: {msg}")
    print()

    ok = (len(before_errors) > 0 and len(after_errors) == 0
          and len(before_errors2) > 0 and len(after_errors2) == 0)
    print("RESULT:", "PASS (bug reproduced before, gone after)" if ok
          else "INCONCLUSIVE (see counts above)")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
