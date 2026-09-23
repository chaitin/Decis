#!/usr/bin/env python3
"""Point the official TypeSafe SDK at Decis instead of `api.typesafe.ai`.

This is the compatibility argument in one file: the client library is used
**unmodified**, it talks to a real socket, and it parses Decis's responses into its
own typed objects. If Decis's wire format drifts from jev's, this script breaks --
which is the point, and why `tests/test_examples.py` runs it in CI.

Run it against a server you started (see `examples/README.md`)::

    uv sync --extra dev                 # dev includes typesafe-sdk
    uv run decis serve --host 127.0.0.1 --port 8000 --engine laya-multilingual
    uv run python examples/python_sdk.py

Against a real engine, nothing changes except the server's `--engine` flag. To point it
at the live TypeSafe API instead, set ``TYPESAFE_API_KEY``::

    TYPESAFE_API_KEY=<key> uv run python examples/python_sdk.py --base-url https://api.typesafe.ai

Environment variables, all optional:

===========================  =========================  ==========================
``DECIS_BASE_URL``           ``--base-url``             default ``http://127.0.0.1:8000``
``DECIS_API_KEY``            ``--api-key``              default ``local``
``DECIS_MODEL``              ``--model``                default ``jev-latest``
===========================  =========================  ==========================
"""

from __future__ import annotations

import argparse
import os
import sys

from typesafe_sdk import (
    Choice,
    Noul,
    Score,
    TypeSafeAPIError,
    TypeSafeAuthenticationError,
    TypeSafeClient,
)

SUPPORT_EMAIL = (
    "We were billed twice for March. The duplicate charge is on invoice #4411. "
    "Please refund it today or we will cancel our plan and move to a competitor."
)


def allow_localhost_through_proxies() -> None:
    """Make `http://127.0.0.1` work on a machine that has a proxy configured.

    Not a Decis concern, but it breaks this script with a message that gives no hint
    about the cause, so it is fixed here rather than left as a trap. `httpx` builds a
    `URLPattern` for every entry in `NO_PROXY`, and a bracketed IPv6 literal such as
    `[::1]` is not parseable as one -- the failure surfaces as
    `InvalidURL: Invalid port: ':1]'` from deep inside the client constructor.

    The same normalization happens in `tests/conftest.py:23` and
    `benchmarks/run.py:163`, for the same reason.
    """
    for variable in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(variable)
        if not value:
            continue
        entries = [entry for entry in value.split(",") if entry and not entry.startswith("[")]
        for loopback in ("127.0.0.1", "localhost", "::1"):
            if loopback not in entries:
                entries.append(loopback)
        os.environ[variable] = ",".join(entries)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.environ.get("DECIS_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.environ.get("DECIS_API_KEY", "local"))
    # `jev-latest` is the official SDK's own default model name, and Decis treats it as
    # "whatever engine this server runs" -- so a client that only changes the base URL
    # keeps working. `decis.requested_model` in the response records what was asked for.
    parser.add_argument("--model", default=os.environ.get("DECIS_MODEL", "jev-latest"))
    return parser.parse_args(argv)


def ask(client: TypeSafeClient, model: str) -> None:
    """One request carrying all three primitives."""
    response = client.system_one(
        state=SUPPORT_EMAIL,
        model=model,
        questions={
            "department": Choice(
                instructions="Which team should handle this?",
                criteria={
                    "billing": "invoices, payments, refunds",
                    "technical": "bugs, outages, system errors",
                    "sales": "pricing, new contracts",
                },
            ),
            "churn_risk": Noul(instructions="Does the user threaten to cancel or leave?"),
            "urgency": Score(
                instructions="How urgent is this?",
                criteria=["Can wait", "Soon", "Today", "Immediately"],
            ),
        },
    )

    print(f"model: {response.model}")
    print(f"request id: {response.request_id}")

    # The SDK already split the answers by type for us -- no dict juggling, and no
    # possibility of reading a `noul` as if it had probabilities.
    department = response.choices["department"]
    print(f"\nchoice  department = {department.choice!r}  (confidence {department.confidence:.3f})")
    for name, probability in department.probabilities.items():
        # The keys are your `criteria` keys, verbatim.
        print(f"          {name:<10} {probability:.4f}")

    # `noul` is a scalar. There is no `confidence` and no `probabilities` -- the
    # number itself is the uncertainty, so this is the whole answer.
    churn = response.nouls["churn_risk"]
    print(f"\nnoul    churn_risk = {churn.noul:.4f}  (P(true), and that is all there is)")

    urgency = response.scores["urgency"]
    print(f"\nscore   urgency    = {urgency.score:.4f}  (confidence {urgency.confidence:.3f})")
    # On the wire these keys are the strings "0", "1", ... because JSON object keys
    # always are. The SDK converts them back to int, which is why this loop can
    # index the legend directly.
    for level, probability in sorted(urgency.probabilities.items()):
        label = urgency.legend.get(level, "")
        print(f"          {level} {label:<13} {probability:.4f}")

    print(f"\nusage: {response.usage.input_tokens} in, {response.usage.output_tokens} out")


def ask_about_many(client: TypeSafeClient, model: str) -> None:
    """Several documents and questions in one call.

    A decision model does one forward pass per request, so a request that carries more
    work costs less per item than the same work split across requests. This is the
    within-request batching that Decis measures today.
    """
    tickets = {
        "t1": "App crashes on launch every time on Android 14.",
        "t2": "Feature request: please add a dark mode.",
        "t3": "Invoice shows a duplicate charge on invoice #4411.",
    }
    response = client.system_one(
        state=tickets,
        model=model,
        questions={
            "needs_engineer": Noul(
                instructions="Does this need an engineer rather than a product manager?",
                # These keys are `"true"`/`"false"`, not `"yes"`/`"no"`. Typing the
                # intuitive pair is easy, and the contract permits unknown properties,
                # so it silently drops the rubric instead of failing -- see D13 in
                # docs/design-review.md.
                criteria={
                    "true": "a defect, crash, error or data loss",
                    "false": "a preference, question or planned work",
                },
            ),
        },
    )
    # `state` was a mapping, but the question is asked once about the whole state,
    # not once per ticket.
    print(f"needs_engineer over all 3 tickets: {response.nouls['needs_engineer'].noul:.4f}")


def show_errors(base_url: str, model: str) -> None:
    """The SDK turns Decis's error bodies into typed exceptions.

    Worth running: it is how you find out that a *missing* credential is a
    permission error (403) while a *wrong* one is an authentication error (401).
    """
    print("\n--- error handling ---")
    try:
        with TypeSafeClient(api_key="definitely-not-the-key", base_url=base_url) as bad_client:
            bad_client.system_one(
                state="x",
                model=model,
                questions={"q": Noul(instructions="?")},
            )
    except TypeSafeAuthenticationError as error:
        print(f"wrong key  -> TypeSafeAuthenticationError (401): {error}")
    except TypeSafeAPIError as error:
        print(f"wrong key  -> {type(error).__name__}: {error}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    allow_localhost_through_proxies()
    print(f"talking to {args.base_url} (model {args.model!r})\n")

    try:
        with TypeSafeClient(api_key=args.api_key, base_url=args.base_url) as client:
            ask(client, args.model)
            print()
            ask_about_many(client, args.model)
            show_errors(args.base_url, args.model)
    except TypeSafeAPIError as error:
        # Everything the SDK raises for a non-2xx response derives from this, so a
        # single `except` is a complete client-side safety net.
        print(f"request failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
