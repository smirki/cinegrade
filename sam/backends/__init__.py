"""Which backend the service runs, and the order `auto` tries them in.

Acceptance A2, the founder's ruling: "if the mlx or pytorch or any variant of
it doesn't work, please use cpu." So `auto` walks MLX, then PyTorch on the Mac
GPU, then PyTorch on the CPU, and every failure is logged with its reason and
kept for `/health` so nobody has to guess why the slow one is running.

Nothing here ever falls back to the stub. A synthetic ellipse that looks like
a matte but is not one would be worse than no matte at all: the service says
it has no backend, and the routes answer 503 with the reasons.
"""

from __future__ import annotations

import gc

from .base import (Backend, BackendError, BackendUnavailable, Cancelled,
                   Instance, ObjectSlot, TrackedMask, mask_area, mask_box,
                   normalize_prompts, plan_objects, split_mask_score)

__all__ = [
    "AUTO_ORDER", "Backend", "BackendError", "BackendUnavailable", "Cancelled",
    "Instance", "KNOWN", "ObjectSlot", "TrackedMask", "load_backend",
    "make_backend", "mask_area", "mask_box", "normalize_prompts",
    "plan_objects", "split_mask_score",
]

AUTO_ORDER = ("mlx", "torch-mps", "torch-cpu")
KNOWN = ("auto", "mlx", "torch-mps", "torch-cpu", "stub")


def make_backend(name: str, log=print, outer_lock_held: bool = True, **kwargs) -> Backend:
    """Construct one backend by name. Constructing never loads weights.

    `duty_cycle_fraction` (quiet mode, sam/throttle.py) is the one kwarg every
    backend takes, because sharing the machine is not an MLX property: the
    torch paths and the stub honour the same sleep, which is what lets the
    timing be tested with no weights at all.
    """
    duty_cycle_fraction = kwargs.get("duty_cycle_fraction") or 1.0
    if name == "stub":
        from .stub import StubBackend
        return StubBackend(delay_ms=kwargs.get("delay_ms", 0.0),
                           chunk_frames=kwargs.get("chunk_frames") or 48,
                           duty_cycle_fraction=duty_cycle_fraction,
                           log=log)
    if name == "mlx":
        from .mlx_backend import MlxBackend
        return MlxBackend(log=log, **kwargs)
    if name in ("torch-mps", "torch-cpu"):
        from .torch_adapter import TorchAdapter
        device = "mps" if name == "torch-mps" else "cpu"
        return TorchAdapter(device=device, outer_lock_held=outer_lock_held,
                            chunk_frames=kwargs.get("chunk_frames") or 48,
                            duty_cycle_fraction=duty_cycle_fraction,
                            log=log)
    raise ValueError(f"unknown backend {name!r}; known: {', '.join(KNOWN)}")


def load_backend(requested: str, log=print, outer_lock_held: bool = True,
                 **kwargs) -> tuple[Backend | None, list[dict]]:
    """Load `requested`, or walk AUTO_ORDER when it is "auto".

    Returns the loaded backend (or None when nothing loaded) and the list of
    failures, each `{"backend": name, "error": reason}`, in the order tried.
    """
    errors: list[dict] = []
    order = AUTO_ORDER if requested == "auto" else (requested,)
    for name in order:
        try:
            backend = make_backend(name, log=log, outer_lock_held=outer_lock_held, **kwargs)
        except Exception as exc:                               # noqa: BLE001
            errors.append({"backend": name, "error": f"{type(exc).__name__}: {exc}"})
            log(f"[backend] {name} could not be constructed: {exc}")
            continue
        try:
            backend.load()
        except Exception as exc:                               # noqa: BLE001
            errors.append({"backend": name, "error": f"{type(exc).__name__}: {exc}"})
            log(f"[backend] {name} did not load: {exc}")
            try:
                backend.close()
            except Exception:                                  # noqa: BLE001
                pass
            # A failed load leaves its weights in the traceback's frames until
            # the next collection, and the next name in AUTO_ORDER is about to
            # load a second model on a 16 GB machine. Drop the reference and
            # collect before that happens (round 1 finding 26).
            del backend
            del exc
            gc.collect()
            continue
        log(f"[backend] {backend.name} is loaded"
            + (f" ({backend.model})" if backend.model else ""))
        return backend, errors
    return None, errors
