"""Read a symbol's past bars and past ticks — the runnable half of guides/reading-price-history.md.

Three things to take away:

1. **History comes through your own terminal.** MFX Hub holds no broker account and keeps no price
   store. The bars and ticks you are given are the ones your broker's server shows your account, read
   by one of your ACTIVE terminals when you ask. No terminal on the server, no history.
2. **Bars are read backwards from `to`, ticks forwards from `from_`.** A window that holds more than
   1,000 bars answers with the *newest* 1,000, so you page by moving `to` earlier. A tick read answers
   with the *first* ticks at or after `from_` and says whether there are `more`.
3. **Several ticks can share one millisecond.** Continue a tick read from the last tick's time and
   the next answer starts with ticks you already hold. `client.md.all_ticks` drops exactly those for
   you; do it by hand and you either lose ticks or double them.

Run it against dev:

    python samples/price_history.py --key-id mfxk_... --private-key-file key.txt \
        --broker-id <uuid> --symbol EURUSD.sd --timeframe M5 --hours 4 --tick-seconds 30

It only reads. Nothing is placed and nothing is subscribed.
"""

from __future__ import annotations

import argparse
import os
import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path

from mfx import MfxClient, SigningKey
from mfx.errors import MfxApiError

BASE_URL = "https://api.dev.mfxhub.com/v1"
#: The most bars one call returns. A wider window is read in pages, newest first.
PAGE = 1000

def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()



async def bars_newest_first(  # noqa: ANN201 - a list of the SDK's Candle
    client: MfxClient,
    broker_id: str,
    symbol: str,
    timeframe: str,
    *,
    start: dt.datetime,
    end: dt.datetime,
    server_id: str | None,
):
    """Every bar of the window, oldest first, read a page at a time from the newest end.

    A window holding more than a page answers with its newest bars. To get the ones before, ask
    again with `to` set just before the oldest bar you were given. A page that comes back short is
    the start of the window.
    """
    pages = []
    to = end
    while True:
        page = await client.md.candles(
            broker_id, symbol, timeframe, from_=start, to=to, server_id=server_id
        )
        if not page.items:
            break
        pages.append(page.items)
        if len(page.items) < PAGE:
            break
        to = page.items[0].ts - dt.timedelta(seconds=1)
        if to < start:
            break
    return [bar for page in reversed(pages) for bar in page]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--broker-id", required=True)
    parser.add_argument("--symbol", required=True, help="the broker's own name, e.g. EURUSD.sd")
    parser.add_argument("--timeframe", default="M5", help="M1 M5 M15 M30 H1 H4 D1 W1 MN1")
    parser.add_argument("--hours", type=float, default=4, help="how far back the bars go")
    parser.add_argument("--tick-seconds", type=float, default=30, help="how far back the ticks go")
    parser.add_argument("--server-id", default=None, help="needed with terminals on several servers")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    key = SigningKey(private_key(args))
    now = dt.datetime.now(dt.UTC)

    async with MfxClient(args.base_url, args.key_id, key) as client:
        # -- bars ---------------------------------------------------------------------------------
        bars = await bars_newest_first(
            client,
            args.broker_id,
            args.symbol,
            args.timeframe,
            start=now - dt.timedelta(hours=args.hours),
            end=now,
            server_id=args.server_id,
        )
        print(f"{args.symbol} {args.timeframe}: {len(bars)} bars over the last {args.hours:g} h")
        if bars:
            # o, h, l and c are decimal strings: Decimal() takes them exactly. `ts` is the bar's
            # open time as a true instant, whatever clock the broker keeps.
            high = max(Decimal(bar.h) for bar in bars)
            low = min(Decimal(bar.l) for bar in bars)
            print(f"  first {bars[0].ts:%Y-%m-%d %H:%M} UTC, last {bars[-1].ts:%Y-%m-%d %H:%M} UTC")
            print(f"  range {low} to {high}; last close {bars[-1].c}")
            print("  the last bar is still forming: its close is the latest price, not a final one")

        # -- ticks --------------------------------------------------------------------------------
        since = now - dt.timedelta(seconds=args.tick_seconds)
        one = await client.md.ticks(args.broker_id, args.symbol, from_=since, limit=5)
        print(f"first {len(one.items)} ticks since {since:%H:%M:%S} UTC (more: {one.more})")
        for tick in one.items:
            # `last` and `volume` are None where the broker sends none, as most FX brokers do.
            print(f"  {tick.ts:%H:%M:%S.%f} bid {tick.bid} ask {tick.ask} last {tick.last}")

        count = 0
        widest = Decimal(0)
        async for tick in client.md.all_ticks(
            args.broker_id, args.symbol, from_=since, server_id=args.server_id
        ):
            count += 1
            widest = max(widest, Decimal(tick.ask) - Decimal(tick.bid))
        print(f"{count} ticks in the last {args.tick_seconds:g} s; widest spread {widest}")

        # -- a symbol the server does not list ------------------------------------------------------
        try:
            await client.md.ticks(args.broker_id, "NOT-A-SYMBOL", from_=since, limit=1)
        except MfxApiError as refused:
            # 404 not_found, with details.reason = symbol_unknown: the terminal's server has no such
            # symbol. Symbol names are the broker's own; read them from client.catalog.symbols.
            print(f"an unlisted symbol: {refused.status} {refused.code}")


if __name__ == "__main__":
    asyncio.run(main())
