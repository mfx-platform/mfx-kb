"""Size a position from a risk budget, exactly — the runnable half of guides/sizing-a-position.md.

The one thing to take away: **do the arithmetic in `Decimal` and never in `float`.** A size is
computed, not typed — risk budget over stop distance — and in binary floating point `0.07 * 3` is
`0.21000000000000002`. MFX takes decimals on the execution path as strings for that reason,
and the SDK refuses a `float` rather than quietly sending one:

    >>> await client.execution.place_order(..., volume=0.07 * 3, ...)
    TypeError: volume must be a Decimal, a str or an int, not a float: a computed float carries
    binary error (0.07 * 3 is 0.21000000000000002) ...

Run it against dev:

    python samples/position_sizing.py --key-id mfxk_... --private-key-file key.txt \
        --terminal-id <uuid> --broker-id <uuid> --symbol EURUSD.sd \
        --risk 50 --stop-distance 0.00150

Nothing is placed unless you pass --place. Without it the sample prints the size it would send, which
is the half worth reading.
"""

from __future__ import annotations

import argparse
import os
import asyncio
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from mfx import MfxClient, SigningKey

BASE_URL = "https://api.dev.mfxhub.com/v1"

def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()



def lots_for(
    *, risk: Decimal, stop_distance: Decimal, contract_size: Decimal, volume_step: Decimal
) -> Decimal:
    """How many lots put `risk` of the account currency behind `stop_distance`, on this symbol.

    Rounded **down** to the symbol's own step: rounding up would risk more than the budget, which is
    the one direction a sizing rule must never take on its own. `quantize` keeps the result exact —
    the same sum in `float` is where 0.21 becomes 0.21000000000000002.
    """
    if stop_distance <= 0 or contract_size <= 0 or volume_step <= 0:
        raise ValueError("stop distance, contract size and volume step must all be above zero")
    raw = risk / (stop_distance * contract_size)
    return raw.quantize(volume_step, rounding=ROUND_DOWN)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--terminal-id", required=True)
    parser.add_argument("--broker-id", required=True)
    parser.add_argument("--symbol", required=True, help="the broker's own name, e.g. EURUSD.sd")
    parser.add_argument("--risk", required=True, help="account currency to put at risk, e.g. 50")
    parser.add_argument("--stop-distance", required=True, help="price distance to the stop")
    parser.add_argument("--side", default="buy", choices=("buy", "sell"))
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--place", action="store_true", help="actually place the order")
    args = parser.parse_args()

    # Decimal(str) from the start: Decimal(0.1) would carry the float's error into the Decimal.
    risk = Decimal(args.risk)
    stop_distance = Decimal(args.stop_distance)
    key = SigningKey(private_key(args))

    async with MfxClient(args.base_url, args.key_id, key) as client:
        # The broker's own rules for this symbol. Every decimal it returns is a string, so
        # Decimal() takes them exactly.
        symbol = await client.catalog.symbol(args.broker_id, args.symbol)
        contract_size = Decimal(symbol.contract_size)
        volume_step = Decimal(symbol.volume_step)
        volume_min = Decimal(symbol.volume_min)

        volume = lots_for(
            risk=risk,
            stop_distance=stop_distance,
            contract_size=contract_size,
            volume_step=volume_step,
        )
        print(f"{args.symbol}: step {volume_step}, contract {contract_size}, min {volume_min}")
        print(f"{risk} at risk over {stop_distance} -> {volume} lots")

        if volume < volume_min:
            print(f"refusing: {volume} is below the symbol's minimum of {volume_min}")
            return
        if not args.place:
            print("not placed (pass --place). The volume above is exactly what would be sent.")
            return

        stop = None
        result = await client.execution.place_order(
            args.terminal_id,
            symbol=args.symbol,
            side=args.side,
            volume=volume,  # a Decimal: the SDK sends "0.21", never 0.21000000000000002
            sl=stop,
            idempotency_key=f"sizing-{uuid.uuid4()}",
        )
        # Every execution call returns a result and never raises: a timeout, a refusal and a broker
        # rejection are all outcomes of trying to trade.
        print(f"{result.state} ok={result.ok} command={result.command_id}")
        if not result.ok:
            print(f"  reason={result.reason} retryable={result.retryable}")


if __name__ == "__main__":
    asyncio.run(main())
