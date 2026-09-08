#!/usr/bin/env python3
"""A loopback client for the SAM masking service (contract C3, C4).

`sam/server.py` is its own process, its own uv project, on its own port
(default 7560, loopback only). This module is the one place studio/server.py
talks to it, so every route that needs a mask goes through the same request
shapes, the same timeouts and the same "the service is not running" message.

Base URL, in order: an explicit `base_url` argument, then STUDIO_SAM_URL,
then http://127.0.0.1:7560. studio/server.py's --sam-url flag sets
STUDIO_SAM_URL before this module is asked for a client, the same pattern
--cache-dir and --data-dir already use elsewhere in this file.

Two exceptions, deliberately different:

  SamUnavailable   the service is not reachable at all (not started, wrong
                   port, connection refused, timed out). The message always
                   carries the exact command that starts it, because "not
                   running" is the common case during development and a
                   stack trace is not an instruction.
  SamError         the service answered, but with an error status: a bad
                   request, an image it could not read, a job id that does
                   not exist. Carries the HTTP status so a caller can tell a
                   404 (no such job) from a 400 (bad prompts) apart.

Nothing here knows about mattes, clips or the studio's own routes: it is a
plain HTTP client for the four C3 routes and nothing else, so a CLI or a test
can import it the same way studio/server.py does.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_SAM_URL = "http://127.0.0.1:7560"

# Shown in every SamUnavailable message. `--stub` starts the service with no
# model at all (contract C3), which is what a person or a test reaches for
# first; the real command differs only by dropping that flag.
START_COMMAND = (
    "uv run --project sam sam/server.py --stub   "
    "(drop --stub once weights are installed; see sam/README.md)"
)


class SamError(Exception):
    """The SAM service answered with an error status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class SamUnavailable(Exception):
    """The SAM service could not be reached at all."""


def _base_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("STUDIO_SAM_URL", "").strip()
    if env:
        return env.rstrip("/")
    return DEFAULT_SAM_URL


class SamClient:
    """One client per base URL. Cheap to construct; holds no connection."""

    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base = _base_url(base_url)
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"SamClient({self.base!r})"

    # -- transport -----------------------------------------------------

    def _call(self, method: str, path: str, body: dict | None = None,
              timeout: float | None = None):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            message = raw.decode("utf-8", "replace") if raw else str(exc)
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("error"):
                    message = str(parsed["error"])
            except (json.JSONDecodeError, TypeError):
                pass
            raise SamError(exc.code, message) from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            raise SamUnavailable(
                f"the SAM masking service is not reachable at {self.base} "
                f"({exc}). Start it with: {START_COMMAND}") from exc

    # -- C3 routes -------------------------------------------------------

    def health(self, timeout: float = 5.0) -> dict:
        """`GET /health` -> {ok, backend, model, loaded, busy, queue}."""
        return self._call("GET", "/health", timeout=timeout)

    def segment(self, image: str, prompts: dict, max_instances: int | None = None,
               timeout: float = 120.0) -> dict:
        """`POST /segment`, synchronous: one frame in, instances out.

        `image` is a path on the machine running the SAM service, which is
        this same machine (loopback only), so the studio's own cache path is
        handed straight through.
        """
        body = {"image": image, "prompts": prompts}
        if max_instances is not None:
            body["max_instances"] = int(max_instances)
        return self._call("POST", "/segment", body, timeout=timeout)

    def track(self, video: str, out_dir: str, fps: float | None = None,
              start_frame: int | None = None, end_frame: int | None = None,
              prompts: dict | None = None, pick: str | None = None,
              select=None, max_instances: int | None = None,
              steady: int | None = None, clip: str | None = None,
              clip_key: str | None = None, rotation=None, width: int | None = None,
              recipe: dict | None = None, rotation_probe_path: str | None = None,
              matte_ids: dict | None = None,
              timeout: float = 15.0) -> dict:
        """`POST /track` -> {job_id, matte_ids, mattes, state, total_frames,
        fps}, per M1's checkpoint. Answers as soon as the job is accepted and
        every matte slot it will produce is known (one per object slot: each
        `boxes` entry, all of `points` together, each `masks` entry, each
        `text` phrase times max_instances), not once tracking finishes. The
        timeout here is short on purpose: a slow response means the service
        itself is stuck, not that tracking is taking a while.

        Either `prompts` (a fresh detect) or `pick` + `select` (seed from a
        prior `segment()`'s own boxes: the instance the caller actually
        chose, not a repeat of the detector's guess). Give one, not both.
        `clip`, `clip_key`, `rotation`, `recipe` are stored verbatim in the
        matte's own index.json by the service (contract C2); studio hands
        them through rather than writing its own copy of that file.

        `rotation_probe_path` is separate from `clip` on purpose: `clip` is
        the bare display name the matte record keeps forever, while this is
        the real file on disk, only ever read (never stored) so the service
        can resolve `rotation="auto"` against the clip's own display-matrix
        tag (contract C3) when `clip` alone is not a path the service's own
        process can open (INTEGRATION-A).

        `matte_ids` is `{object_id: matte_id}` and is how a RESUME is asked
        for (checkpoint gap 12): the service normally derives a matte id
        from the recipe AND the frame range, so re-queueing only the missing
        tail of a cancelled track would otherwise write a second, different
        matte and orphan the frames already on disk. Naming the ids keeps
        the resumed frames landing in the same matte directory, next to the
        ones that survived, with that matte's own index carried forward.
        """
        body: dict = {"video": video, "out_dir": out_dir}
        if fps is not None:
            body["fps"] = fps
        if start_frame is not None:
            body["start_frame"] = int(start_frame)
        if end_frame is not None:
            body["end_frame"] = int(end_frame)
        if pick:
            body["pick"] = pick
            body["select"] = select if select is not None else "all"
        else:
            body["prompts"] = prompts or {}
            if select is not None:
                body["select"] = select
        if max_instances is not None:
            body["max_instances"] = int(max_instances)
        if steady is not None:
            body["steady"] = int(steady)
        if clip is not None:
            body["clip"] = clip
        if clip_key is not None:
            body["clip_key"] = clip_key
        if rotation is not None:
            body["rotation"] = rotation
        if width is not None:
            body["width"] = int(width)
        if recipe is not None:
            body["recipe"] = recipe
        if rotation_probe_path is not None:
            body["rotation_probe_path"] = rotation_probe_path
        if matte_ids:
            body["matte_ids"] = {str(k): str(v) for k, v in matte_ids.items()}
        return self._call("POST", "/track", body, timeout=timeout)

    def job(self, job_id: str, timeout: float = 10.0) -> dict:
        """`GET /jobs/<id>` -> {state, done_frames, total_frames, fps,
        elapsed_s, matte_ids, error}."""
        return self._call("GET", f"/jobs/{job_id}", timeout=timeout)

    def job_cancel(self, job_id: str, timeout: float = 10.0) -> dict:
        return self._call("POST", f"/jobs/{job_id}/cancel", timeout=timeout)

    def jobs(self, timeout: float = 10.0) -> dict:
        """`GET /jobs`, the service's own queue."""
        return self._call("GET", "/jobs", timeout=timeout)


_default: SamClient | None = None


def client(base_url: str | None = None) -> SamClient:
    """A shared client for the process, unless a caller wants its own base.

    studio/server.py calls this with no argument after --sam-url has set
    STUDIO_SAM_URL, so every route shares one client; a test that wants a
    second server on a different port constructs its own SamClient directly
    instead of touching this cache.
    """
    global _default
    if base_url:
        return SamClient(base_url)
    if _default is None:
        _default = SamClient()
    return _default


def reset_default() -> None:
    """Drop the cached client so a changed STUDIO_SAM_URL takes effect.

    Only needed by a test harness that flips the env var after import; a
    normal run sets STUDIO_SAM_URL once, at startup, before anything calls
    client().
    """
    global _default
    _default = None
