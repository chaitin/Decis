"""Device selection: which device this machine can serve from, and which one it uses.

`engines/devices.py` is the one place that answers this, and it is written so the answer
can be tested without an accelerator: the probe is a parameter. Nothing here needs torch,
weights, or a GPU.

The second half of the same module -- "why is this machine serving from the CPU when it
has a GPU?" -- is tested the same way: `torch_build` is monkeypatched and `nvidia_smi` is
a fake subprocess, so the Windows case (an RTX card behind a CPU-only wheel, which is what
`docs/design-review.md` §2-D36 records) is asserted here without a Windows machine.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from types import SimpleNamespace

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


# --- the build torch was compiled with, and the driver underneath it -----------------
#
# `torch.cuda.is_available()` cannot separate "no GPU" from "a GPU this wheel cannot use",
# and that separation is the whole content of D36. So both sides are faked here: the torch
# module (a CPU-only wheel, a CUDA wheel, a broken backend) and `nvidia-smi` (a driver
# that answers, one that is missing, one that hangs).


def _fake_torch(*, cuda: str | None = None, devices: int = 0, name: str | None = None, boom: bool = False) -> object:
    """A `torch` module whose only interesting attributes are the CUDA ones."""

    class _Cuda:
        @staticmethod
        def device_count() -> int:
            if boom:
                raise RuntimeError("the driver refused the query")
            return devices

        @staticmethod
        def get_device_name(index: int) -> str:
            assert name is not None, "the fake only has a name when it has a device"
            return name

    return SimpleNamespace(
        __version__="2.14.0",
        version=SimpleNamespace(cuda=cuda, hip=None),
        cuda=_Cuda(),
    )


def _smi(stdout: str, returncode: int = 0) -> object:
    return SimpleNamespace(stdout=stdout, returncode=returncode)


def test_torch_build_reports_nothing_when_torch_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)
    assert devices.torch_build() is None


def test_torch_build_names_a_wheel_with_no_cuda_support(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows wheel from PyPI: `torch.version.cuda is None`, and that is the tell."""
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=None))
    build = devices.torch_build()
    assert build is not None
    assert build.cuda is None
    assert build.describe() == "2.14.0, built without CUDA support"


def test_torch_build_names_the_cuda_runtime_and_the_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda="13.0", devices=1, name="NVIDIA GeForce RTX 4070"))
    build = devices.torch_build()
    assert build is not None
    assert build.describe() == "2.14.0, CUDA 13.0, 1 CUDA device(s), 0: NVIDIA GeForce RTX 4070"


def test_torch_build_survives_a_backend_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`decis doctor` must answer on a broken install rather than traceback."""
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda="13.0", boom=True))
    build = devices.torch_build()
    assert build is not None
    assert build.cuda_devices == 0


def test_nvidia_gpus_parses_the_csv_the_driver_prints() -> None:
    calls: list[tuple] = []

    def run(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return _smi("NVIDIA GeForce RTX 4070, 552.22\nNVIDIA GeForce RTX 3060, 552.22\n")

    gpus = devices.nvidia_gpus(run)
    assert [gpu.name for gpu in gpus] == ["NVIDIA GeForce RTX 4070", "NVIDIA GeForce RTX 3060"]
    assert gpus[0].driver == "552.22"
    assert gpus[0].describe() == "NVIDIA GeForce RTX 4070 (driver 552.22)"
    # The query is one row per GPU, so the parse does not depend on nvidia-smi's table layout.
    assert list(calls[0][0][0]) == ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]
    assert calls[0][1]["timeout"] == devices._GPU_PROBE_TIMEOUT_S, "a hanging driver must not hang a load"


@pytest.mark.parametrize("failure", [FileNotFoundError("no nvidia-smi"), subprocess.TimeoutExpired("nvidia-smi", 5)])
def test_nvidia_gpus_is_empty_when_the_driver_cannot_answer(failure: Exception) -> None:
    def run(*args: object, **kwargs: object) -> object:
        raise failure

    assert devices.nvidia_gpus(run) == ()


def test_nvidia_gpus_is_empty_when_the_driver_reports_an_error() -> None:
    """A non-zero exit (no device, a driver this `--query-gpu` form predates) is no answer."""
    assert devices.nvidia_gpus(lambda *a, **k: _smi("", returncode=9)) == ()
    assert devices.nvidia_gpus(lambda *a, **k: _smi("\n")) == ()


def _cpu_only() -> devices.TorchBuild:
    return devices.TorchBuild(version="2.14.0", cuda=None, hip=None, cuda_devices=0, cuda_device_name=None)


def _rtx() -> tuple[devices.Gpu, ...]:
    return (devices.Gpu(name="NVIDIA GeForce RTX 4070", driver="552.22"),)


def test_the_advice_names_the_cpu_only_wheel_when_a_gpu_is_present() -> None:
    """D36, as the operator would read it: a named GPU, a named cause, a named command."""
    advice = devices.accelerator_advice("cpu", build=_cpu_only(), gpus=_rtx())
    assert advice is not None
    assert "NVIDIA GeForce RTX 4070 (driver 552.22)" in advice
    assert "built without CUDA" in advice
    assert "uv sync --all-extras" in advice


def test_a_cuda_build_that_sees_no_device_gets_the_driver_answer() -> None:
    """A CUDA wheel that still finds nothing is a driver problem, not a wheel problem."""
    build = devices.TorchBuild(version="2.14.0+cu130", cuda="13.0", hip=None, cuda_devices=0, cuda_device_name=None)
    advice = devices.accelerator_advice("cpu", build=build, gpus=_rtx())
    assert advice is not None
    assert "driver is older" in advice
    assert "uv sync" not in advice, "reinstalling the same CUDA wheel would not help"


def test_the_driver_is_asked_only_when_torch_has_no_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CUDA build has already answered the question; a second opinion costs a subprocess."""
    asked: list[str] = []

    def probe() -> tuple[devices.Gpu, ...]:
        asked.append("nvidia-smi")
        return _rtx()

    monkeypatch.setattr(devices, "nvidia_gpus", probe)
    assert devices.accelerator_advice("cpu", build=_cpu_only(), gpus=None) is not None
    assert asked == ["nvidia-smi"]
    build = devices.TorchBuild(version="2.14.0+cu130", cuda="13.0", hip=None, cuda_devices=0, cuda_device_name=None)
    assert devices.accelerator_advice("cpu", build=build, gpus=None) is None
    assert asked == ["nvidia-smi"], "a CUDA build cannot learn anything from the driver query"


def test_there_is_no_advice_when_there_is_no_gpu() -> None:
    """The ordinary CPU-only machine: a warning on every start is noise, not help."""
    assert devices.accelerator_advice("cpu", build=_cpu_only(), gpus=()) is None


def test_a_machine_without_torch_gets_no_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build=None` means "probe it", so the absent case has to be the probe saying nothing."""
    monkeypatch.setattr(devices, "torch_build", lambda: None)
    assert devices.accelerator_advice("cpu", gpus=_rtx()) is None


def test_a_pinned_cpu_or_a_loaded_accelerator_gets_no_advice() -> None:
    assert devices.accelerator_advice("cpu", pinned=True, build=_cpu_only(), gpus=_rtx()) is None
    assert devices.accelerator_advice("cuda", build=_cpu_only(), gpus=_rtx()) is None
    assert devices.accelerator_advice("mps", build=_cpu_only(), gpus=_rtx()) is None


def test_warn_if_accelerator_is_idle_logs_the_advice_once(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("decis.engines.test-idle-gpu")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        returned = devices.warn_if_accelerator_is_idle(
            "cpu", pinned=False, logger=logger, build=_cpu_only(), gpus=_rtx()
        )
    assert returned is not None
    assert [record.message for record in caplog.records] == [returned], "one line, the whole reason"


def test_warn_if_accelerator_is_idle_is_silent_when_there_is_nothing_to_say(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("decis.engines.test-quiet")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        assert devices.warn_if_accelerator_is_idle("mps", pinned=False, logger=logger, build=_cpu_only()) is None
    assert caplog.records == []
