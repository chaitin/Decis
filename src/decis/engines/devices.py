"""Which device this machine can serve from, and which one to use.

This is the only place that asks the hardware anything. `DTYPE_DEFAULTS` -- "which
precision for this engine on this device" -- stays in `registry.py`, because that is
engine policy; here is only the device half of the pair, so a dtype table and a device
list cannot disagree about what the device names are.

`torch` is imported inside the probe rather than at module scope. `decis models` and
`GET /v1/models` must work in an image that installed no engine extra, and they must
never pay for a multi-gigabyte import to answer a question about device preference
(AGENTS.md §6). Nothing here is imported until an engine actually loads.
"""

from __future__ import annotations

from collections.abc import Callable

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
