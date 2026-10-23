"""Run the knowledge base's samples against a live MFX Hub environment, as a customer would.

Every guide quotes a sample, so a sample that stops working must fail a build instead of
misleading a reader. This runs each sample as its own process, with its own command line, and
fails on the first one that does not exit 0.

Configuration comes only from the environment, the same variables the samples document:

    MFX_BASE_URL       https://api.dev.mfxhub.com/v1 by default
    MFX_KEY_ID         an API key id
    MFX_PRIVATE_KEY    its private key
    MFX_TERMINAL_ID    an ACTIVE terminal the key's tenant owns
    MFX_BROKER_ID      that terminal's broker
    MFX_SERVER_ID      that terminal's server
    MFX_SYMBOL         one of its instruments, in the broker's own name

``--read-only`` (the default) runs the samples that change nothing: quotes, history, the live
account, a position size computed and not placed, and the webhook receiver's self-check. It never
creates anything, so a read-only key is enough: live prices follow a subscription that already
exists for the symbol (a standing one on the CI tenant), rather than creating one.
``--all`` adds the samples that create and retire terminals and log in to a broker; those also need
MFX_BROKER_LOGIN, MFX_BROKER_PASSWORD and MFX_BROKER_SERVER for a demo account.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
BASE_URL = "https://api.dev.mfxhub.com/v1"
NEEDED = ("MFX_KEY_ID", "MFX_PRIVATE_KEY", "MFX_TERMINAL_ID", "MFX_BROKER_ID", "MFX_SERVER_ID",
          "MFX_SYMBOL")  # fmt: skip


def read_only(env: dict[str, str]) -> list[tuple[str, list[str]]]:
    base = ["--base-url", env.get("MFX_BASE_URL", BASE_URL)]
    return [
        ("webhook_receiver.py", ["--self-check"]),
        ("prices_live.py", ["--server-id", env["MFX_SERVER_ID"], "--symbol", env["MFX_SYMBOL"],
                            "--seconds", "20", *base]),
        ("price_history.py", ["--broker-id", env["MFX_BROKER_ID"], "--server-id",
                              env["MFX_SERVER_ID"], "--symbol", env["MFX_SYMBOL"], *base]),
        ("account_live.py", ["--terminal", env["MFX_TERMINAL_ID"], "--seconds", "20", *base]),
        ("position_sizing.py", ["--terminal-id", env["MFX_TERMINAL_ID"], "--broker-id",
                                env["MFX_BROKER_ID"], "--symbol", env["MFX_SYMBOL"], "--risk",
                                "50", "--stop-distance", "0.0020", *base]),
    ]  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="also the samples that change things")
    parser.add_argument("--timeout", type=float, default=240, help="seconds a sample may take")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    missing = [name for name in NEEDED if not env.get(name)]
    if missing:
        sys.stderr.write(f"not set: {', '.join(missing)}\n")
        return 2
    runs = read_only(env)
    if args.all:
        sys.stderr.write("--all needs the demo broker account; not configured in this run\n")
        return 2
    failed = 0
    for name, sample_args in runs:
        started = time.monotonic()
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, never a shell
                [sys.executable, str(SAMPLES / name), *sample_args],
                env=env, timeout=args.timeout, capture_output=True, text=True, check=False,
            )  # fmt: skip
            code, tail = done.returncode, (done.stdout + done.stderr).strip().splitlines()[-3:]
        except subprocess.TimeoutExpired:
            code, tail = -1, [f"no answer in {args.timeout:g} s"]
        took = time.monotonic() - started
        verdict = "ok" if code == 0 else f"FAILED ({code})"
        sys.stdout.write(f"{name}: {verdict} in {took:.1f} s\n")
        for line in tail:
            sys.stdout.write(f"    {line}\n")
        failed += code != 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
