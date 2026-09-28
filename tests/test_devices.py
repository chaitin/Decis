"""Device selection: which device this machine can serve from, and which one it uses.

`engines/devices.py` is the one place that answers this, and it is written so the answer
can be tested without an accelerator: the probe is a parameter. Nothing here needs torch,
weights, or a GPU.
"""

from __future__ import annotations

import sys

import pytest

from decis.engines import devices
from decis.errors import EngineUnavailableError


def test_accelerators_are_reported_best_first_with_cpu_last() -> None:
    """The order is the whole decision, and `cpu` is a fallback rather than a preference."""
    assert devices.available_devices(lambda: ("cuda", "mps")) == ("cuda", "mps", "cpu")
    assert devices.available_devices(lambda: ()) == ("cpu",)
    assert set(devices.ACCELERATOR_ORDER) == {"cuda", "xpu", "npu", "mps"}
    assert "cpu" not in devices.ACCELERATOR_ORDER


def test_best_device_is_the_first_accelerator_the_machine_reports() -> None:
    assert devices.best_device(lambda: ("npu",)) == "npu"
    assert devices.best_device(lambda: ("mps",)) == "mps"
    assert devices.best_device(lambda: ()) == "cpu", "a machine with no accelerator still serves"


def test_every_accelerator_is_a_device_decis_accepts() -> None:
    """Detection and `DECIS_DEVICE` must not disagree: both are checked against `DEVICES`."""
    assert set(devices.ACCELERATOR_ORDER) <= devices.DEVICES
    assert "cpu" in devices.DEVICES


@pytest.mark.parametrize("name", [*sorted(devices.DEVICES), None])
def test_documented_devices_pass_the_check(name: str | None) -> None:
    assert devices.requested_device(name) == name


def test_a_misspelled_device_names_the_variable() -> None:
    """Handing `"gpu"` to `torch.device` fails deep inside an engine and never mentions
    `DECIS_DEVICE`, which is the one thing the reader has to change."""
    with pytest.raises(EngineUnavailableError, match="DECIS_DEVICE='gpu' is not a device"):
        devices.requested_device("gpu")


def test_the_probe_reports_nothing_when_torch_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`decis models` runs in images with no engine extra, so this must not import them."""
    monkeypatch.setitem(sys.modules, "torch", None)
    assert devices._torch_accelerators() == ()


def test_the_probe_asks_each_accelerator_torch_exposes(monkeypatch: pytest.MonkeyPatch) -> None:
    """One `is_available()` per candidate, in the order the module documents."""

    class _Backend:
        @staticmethod
        def is_available() -> bool:
            return True

    class _Module:
        is_available = staticmethod(lambda: True)

    class _Backends:
        mps = _Backend()

    class _Torch:
        cuda = _Module()
        xpu = _Module()
        backends = _Backends()

        def __getattr__(self, name: str) -> object:
            # On a real Ascend install `torch.npu` appears when the plugin is imported,
            # which is exactly what the probe relies on.
            if name == "npu" and "torch_npu" in sys.modules:
                return _Module()
            raise AttributeError(name)

    monkeypatch.setitem(sys.modules, "torch", _Torch())
    monkeypatch.setitem(sys.modules, "torch_npu", _Module())
    assert devices._torch_accelerators() == ("cuda", "xpu", "npu", "mps")

    monkeypatch.delitem(sys.modules, "torch_npu", raising=False)
    assert devices._torch_accelerators() == ("cuda", "xpu", "mps"), "no plugin, no NPU"
