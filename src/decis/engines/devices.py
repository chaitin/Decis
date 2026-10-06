"""Which device this machine can serve from, and which one to use.

This is the only place that asks the hardware anything. `DTYPE_DEFAULTS` -- "which
precision for this engine on this device" -- stays in `registry.py`, because that is
engine policy; here is only the device half of the pair, so a dtype table and a device
list cannot disagree about what the device names are.

It also owns the second half of the same question, the one `torch` cannot answer: **why**
a machine with a GPU is serving from the CPU. `torch.cuda.is_available()` is `False` both
on a machine with no NVIDIA GPU and on a machine whose GPU is sitting behind a CPU-only
PyTorch wheel, and the two need opposite advice. So when (and only when) a load lands on
`cpu`, the driver is asked directly -- `nvidia-smi`, which ships with the NVIDIA driver on
Windows and Linux -- and the difference becomes a warning an operator can act on
(`docs/design-review.md` §2-D36).

`torch` is imported inside each probe rather than at module scope. `decis models` and
`GET /v1/models` must work in an image that installed no engine extra, and they must
never pay for a multi-gigabyte import to answer a question about device preference
(AGENTS.md §6). `decis doctor` imports this module on purpose and says what it finds.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import EngineUnavailableError

#: The device names `DECIS_DEVICE` may carry -- `torch`'s own, and the vocabulary the keys
#: of `registry.DTYPE_DEFAULTS` are written in. Declared once so a misspelling is refused
#: by name, instead of surfacing as `torch.device("gpu")` failing somewhere inside an
#: engine's `load()` with a message that never mentions the variable.
#:
#: `xpu` (Intel) and `npu` (Ascend) are here because `torch` exposes them under these names
#: and a server that detects them must also accept them explicitly. Decis has been measured
#: on `cpu` and `mps` only; the other two are detected but unmeasured, so they fall back to
#: `fp32` through `registry.default_dtype`.
DEVICES = frozenset({"cpu", "cuda", "mps", "xpu", "npu"})

#: The order `available_devices` reports accelerators in. `cpu` is absent on purpose: it is
#: the fallback, not a preference, and the one device that is always present.
#:
#: Discrete accelerators come first, Metal last. This order is the whole decision, and it
#: matters: with `DECIS_DEVICE` unset, `kev-0.8b` used to load on `cpu` whatever the machine
#: had, so an M3 Pro served every request on the CPU -- measured at 3,299 ms per 3-question
#: request against 215 ms on `mps`. Recorded in `docs/design-review.md §2-D24`.
ACCELERATOR_ORDER = ("cuda", "xpu", "npu", "mps")

#: The driver query behind `nvidia_gpus`. One CSV row per GPU, so the parse does not depend
#: on the table layout `nvidia-smi` prints when it is given no arguments.
_NVIDIA_SMI = ("nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader")

#: A driver query is diagnostics, never a dependency of starting: a machine whose
#: `nvidia-smi` hangs must not hang the load behind it.
_GPU_PROBE_TIMEOUT_S = 5.0


def _torch_accelerators() -> tuple[str, ...]:
    """Which accelerators `torch` reports as usable here, best first, `cpu` excluded.

    Every probe is defensive. This runs before an engine can report anything, so a backend
    that raises, or a `torch` built without that backend, has to mean "not available"
    rather than "the service failed to start".
    """
    try:
        import torch
    except ImportError:
        return ()

    found: list[str] = []
    for device in ACCELERATOR_ORDER:
        if device == "npu":
            # `torch.npu` exists only once the Ascend plugin has been imported; `torch`
            # alone never exposes it, so importing it *is* the probe.
            try:
                import torch_npu  # noqa: F401
            except ImportError:
                continue
        try:
            if device == "mps":
                backend = getattr(torch.backends, "mps", None)
                usable = backend is not None and bool(backend.is_available())
            else:
                module = getattr(torch, device, None)
                usable = module is not None and bool(module.is_available())
        except Exception:  # an accelerator that cannot be probed is not usable
            usable = False
        if usable:
            found.append(device)
    return tuple(found)


def available_devices(probe: Callable[[], tuple[str, ...]] = _torch_accelerators) -> tuple[str, ...]:
    """Every device this machine can serve from, best first, `cpu` always last.

    `probe` is a parameter so the weight-free suite can exercise the ordering without an
    accelerator (and without `torch` installed at all).
    """
    return (*probe(), "cpu")


def best_device(probe: Callable[[], tuple[str, ...]] = _torch_accelerators) -> str:
    """The device to load on when `DECIS_DEVICE` names none."""
    return available_devices(probe)[0]


def requested_device(name: str | None) -> str | None:
    """`DECIS_DEVICE`, checked, or `None` when the caller pinned nothing.

    A typo is refused by name, instead of being handed to `torch.device` and failing inside
    an engine's `load()` with a message that never mentions the variable. Both engines call
    this: Laya's own `Agent` then picks the device when the answer is `None` (upstream's
    order is upstream's fact), while `kev-0.8b` asks `best_device()`.
    """
    if name is None or name in DEVICES:
        return name
    raise EngineUnavailableError(f"DECIS_DEVICE={name!r} is not a device. Use one of: {', '.join(sorted(DEVICES))}.")


@dataclass(frozen=True)
class Gpu:
    """One GPU the *driver* knows about, asked of the driver rather than of `torch`."""

    name: str
    driver: str = ""

    def describe(self) -> str:
        return f"{self.name} (driver {self.driver})" if self.driver else self.name


@dataclass(frozen=True)
class TorchBuild:
    """What the installed PyTorch was built to accelerate, before any engine loads.

    Only the CUDA half is read. `torch` exposes nothing comparable for Metal or XPU -- the
    usable-device question for those is `available_devices`, which cannot tell a CPU-only
    wheel from a machine with no GPU. That asymmetry is the whole reason this dataclass
    exists: `torch.version.cuda is None` is the one fact that names the Windows failure
    mode, where PyPI's wheel has no CUDA support and therefore no GPU, whatever the box.
    """

    version: str
    #: `torch.version.cuda`: the CUDA runtime this wheel was compiled against, or `None`
    #: for a build with no CUDA support at all. This is the field that separates "this
    #: machine has no NVIDIA GPU" from "this PyTorch cannot use the NVIDIA GPU it has".
    cuda: str | None
    hip: str | None
    cuda_devices: int
    cuda_device_name: str | None

    def describe(self) -> str:
        if self.cuda:
            runtime = f"CUDA {self.cuda}"
        elif self.hip:
            runtime = f"ROCm {self.hip}"
        else:
            # No CUDA in the build at all, so the device count is a constant zero and
            # printing it would read as a fact about the machine rather than the wheel.
            return f"{self.version}, built without CUDA support"
        if not self.cuda_devices:
            found = "0 CUDA devices"
        elif self.cuda_device_name:
            found = f"{self.cuda_devices} CUDA device(s), 0: {self.cuda_device_name}"
        else:
            found = f"{self.cuda_devices} CUDA device(s)"
        return f"{self.version}, {runtime}, {found}"


def torch_build() -> TorchBuild | None:
    """What the installed `torch` can accelerate, or `None` when it is not installed.

    Every read is defensive for the same reason the device probe is: this runs in
    `decis doctor`, which has to answer on a machine where torch is absent, broken, or
    built by a vendor.
    """
    try:
        import torch
    except Exception:
        return None
    version = getattr(torch, "version", None)
    devices, name = 0, None
    try:
        devices = int(torch.cuda.device_count())
        if devices:
            name = str(torch.cuda.get_device_name(0))
    except Exception:
        devices, name = 0, None
    return TorchBuild(
        version=str(getattr(torch, "__version__", "unknown")),
        cuda=getattr(version, "cuda", None),
        hip=getattr(version, "hip", None),
        cuda_devices=devices,
        cuda_device_name=name,
    )


def nvidia_gpus(run: Callable[..., Any] = subprocess.run) -> tuple[Gpu, ...]:
    """NVIDIA GPUs the driver reports, or nothing when there is no driver to ask.

    Deliberately independent of `torch`. This is the fallback used *after* torch has said
    it has no accelerator, so asking torch here would repeat the question that just got the
    wrong answer -- a CPU-only wheel is blind to the GPU. `nvidia-smi` is the cheapest way
    to tell the two cases apart, and it ships with the driver on Windows and Linux. A
    machine without it (no NVIDIA hardware, or an AMD/Intel GPU) gets `()`, and the caller
    then says nothing rather than guessing.

    `run` is a parameter so the weight-free suite can exercise the parse without a GPU.
    """
    try:
        completed = run(
            list(_NVIDIA_SMI),
            capture_output=True,
            text=True,
            timeout=_GPU_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        # No such executable, no permission, a timeout, or an argument this driver's
        # `nvidia-smi` does not understand. None of them is a reason to fail a load.
        return ()
    gpus: list[Gpu] = []
    for line in str(getattr(completed, "stdout", "") or "").splitlines():
        name, _, driver = line.partition(",")
        if name.strip():
            gpus.append(Gpu(name=name.strip(), driver=driver.strip()))
    return tuple(gpus)


def accelerator_advice(
    chosen: str,
    *,
    pinned: bool = False,
    build: TorchBuild | None = None,
    gpus: Sequence[Gpu] | None = None,
) -> str | None:
    """Why this process is not using a GPU that is sitting right there, or `None`.

    Three answers, and the silence matters as much as the text:

    * loaded on an accelerator, or `DECIS_DEVICE` pinned the CPU: nothing to say -- the
      operator either got what they asked for or does not need telling;
    * loaded on `cpu`, torch has no CUDA support, and the driver reports a GPU: the case
      this function exists for. PyPI's Windows wheel is CPU-only (its metadata declares no
      CUDA dependency at all), so a Windows host with an RTX card serves from the CPU
      while nothing anywhere in the process can name the reason;
    * loaded on `cpu`, no GPU reported: the ordinary CPU-only machine. Saying "no GPU
      found" on every start is noise, so the caller is told nothing.

    `build` and `gpus` are parameters so the weight-free suite can pin both sides of the
    comparison without a GPU or a driver.
    """
    if chosen != "cpu" or pinned:
        return None
    build = torch_build() if build is None else build
    if build is None:
        return None
    if gpus is None:
        # A build with CUDA in it has already been asked whether it can see a device; only
        # a build without CUDA needs the driver's second opinion.
        gpus = () if build.cuda else nvidia_gpus()
    if not gpus:
        return None
    found = ", ".join(gpu.describe() for gpu in gpus)
    if build.cuda:
        return (
            f"{found} present, but torch {build.version} (built for CUDA {build.cuda}) reports no usable "
            "device: the NVIDIA driver is older than that CUDA runtime, or the device is not visible in "
            "this process. Update the driver, or set DECIS_DEVICE=cpu to make the CPU choice explicit."
        )
    return (
        f"{found} present, but torch {build.version} was built without CUDA (torch.version.cuda is None), "
        "so no engine here can use it. Install a CUDA build for this platform: on Windows PyPI publishes a "
        "CPU-only wheel, and `uv sync --all-extras` takes the CUDA one from the index pyproject.toml "
        "configures. Set DECIS_DEVICE=cpu to make the CPU choice explicit."
    )


def warn_if_accelerator_is_idle(
    chosen: str,
    *,
    pinned: bool,
    logger: logging.Logger,
    build: TorchBuild | None = None,
    gpus: Sequence[Gpu] | None = None,
) -> str | None:
    """Log `accelerator_advice` where the load happened, and return it for the tests.

    Warn-only on purpose. A machine with no accelerator is the normal case and must not
    fail to start, and a GPU this build cannot use is a deployment mistake, not a reason to
    refuse to serve: the service works, it is just an order of magnitude slower than the
    hardware allows.
    """
    advice = accelerator_advice(chosen, pinned=pinned, build=build, gpus=gpus)
    if advice:
        logger.warning("%s", advice)
    return advice
