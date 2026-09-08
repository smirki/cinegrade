"""Tests for sam/backends/torch_backend.py (contract C7, lane M1b).

Two tiers:

- Always-on: pure logic (device resolution, fraction<->pixel conversion,
  the machine-wide lock helpers) that needs neither the checkpoint nor a
  GPU/CPU model load.
- Real-model tests: gated behind `SAM_TORCH_TEST_REAL=1` (and skipped if
  `transformers`/`torch` are not installed, or the checkpoint is not
  reachable) because they download / hold a multi-GB model and this
  machine is memory bound; they are not meant for routine CI, only for a
  deliberate local or orchestrator smoke run. They take the machine-wide
  model lock the same way the backend itself does, so they are safe to
  run alongside other lanes loading a SAM model on this machine, just
  slow (retries every 20s while another lane holds it).
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from backends.torch_backend import (
    TorchBackend,
    _frac_box_to_pixels,
    _frac_points_to_pixels,
    _resolve_device,
    acquire_model_lock,
    release_model_lock,
)


def test_frac_points_to_pixels():
    points = [{"x": 0.5, "y": 0.25, "label": 1}, {"x": 0.1, "y": 0.9, "label": 0}]
    xy, labels = _frac_points_to_pixels(points, width=200, height=100)
    assert xy == [[100.0, 25.0], [20.0, 90.0]]
    assert labels == [1, 0]


def test_frac_points_to_pixels_default_label_is_positive():
    xy, labels = _frac_points_to_pixels([{"x": 0.0, "y": 0.0}], width=10, height=10)
    assert labels == [1]


def test_frac_box_to_pixels():
    box = _frac_box_to_pixels([0.1, 0.2, 0.6, 0.8], width=100, height=50)
    assert box == [10.0, 10.0, 60.0, 40.0]


def test_resolve_device_cpu_forced():
    assert _resolve_device("cpu") == "cpu"


def test_backend_name_before_load_is_torch_cpu_placeholder():
    backend = TorchBackend(device="cpu")
    assert backend.name == "torch-cpu"
    assert backend.device is None


def test_model_lock_roundtrip(tmp_path, monkeypatch):
    import backends.torch_backend as tb

    lock_path = tmp_path / "sam-model.lock"
    monkeypatch.setattr(tb, "_MODEL_LOCK_DIR", lock_path)
    acquire_model_lock(poll_s=0.01)
    assert lock_path.is_dir()
    release_model_lock()
    assert not lock_path.exists()


def test_model_lock_blocks_second_caller(tmp_path, monkeypatch):
    import threading
    import time

    import backends.torch_backend as tb

    lock_path = tmp_path / "sam-model.lock"
    monkeypatch.setattr(tb, "_MODEL_LOCK_DIR", lock_path)
    acquire_model_lock(poll_s=0.01)

    acquired_at = []

    def waiter():
        acquire_model_lock(poll_s=0.02)
        acquired_at.append(time.perf_counter())
        release_model_lock()

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    assert t.is_alive()  # still blocked while we hold the lock
    release_model_lock()
    t.join(timeout=2)
    assert not t.is_alive()
    assert acquired_at


def _real_model_available() -> bool:
    if os.environ.get("SAM_TORCH_TEST_REAL") != "1":
        return False
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


requires_real_model = pytest.mark.skipif(
    not _real_model_available(),
    reason="set SAM_TORCH_TEST_REAL=1 with torch+transformers installed to run the real-weights smoke test",
)


@requires_real_model
def test_segment_text_smoke():
    """A tiny synthetic image, one text prompt. Proves load() -> segment()
    -> close() round trips against the real checkpoint without asserting
    on mask content (that is the checkpoint's job, not this test's)."""
    backend = TorchBackend(device="cpu")
    backend.load()
    try:
        image = (np.random.rand(64, 64, 3) * 255).astype(np.uint8)
        instances = backend.segment(image, {"text": "person"}, max_instances=4)
        assert isinstance(instances, list)
        for inst in instances:
            assert set(inst.keys()) >= {"id", "score", "box", "area", "mask"}
            assert inst["mask"].dtype == np.float32
            assert inst["mask"].shape == (64, 64)
    finally:
        backend.close()


@requires_real_model
def test_segment_point_smoke():
    backend = TorchBackend(device="cpu")
    backend.load()
    try:
        image = (np.random.rand(64, 64, 3) * 255).astype(np.uint8)
        instances = backend.segment(image, {"points": [{"x": 0.5, "y": 0.5, "label": 1}]}, max_instances=1)
        assert len(instances) == 1
        assert instances[0]["mask"].shape == (64, 64)
    finally:
        backend.close()
