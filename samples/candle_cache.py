"""Keep price history cached, read only what is missing, and wait when told to - the runnable half
of guides/limits-on-price-history.md.

Three things to take away:

1. **A closed bar never changes.** Once you hold it, never ask for it again. This sample keeps the
   closed bars it has read in a small file, and the second read asks only for the bars after the
   newest one it holds.
2. **`source` says what an answer cost.** `store` was answered by MFX Hub's candle store and cost
   your terminals nothing. `terminal` (or `store+terminal`) went to one of your terminals, and those
   reads are limited per terminal per minute.
3. **A `429 terminal_reads_limit` means "next minute".** The SDK already waits `Retry-After` and
   asks again a few times. If it still raises `RateLimitError`, wait `retry_after_s` yourself and
   carry on; never retry in a tight loop.

Run it against dev:

    python samples/candle_cache.py --key-id mfxk_... --private-key-file key.txt \
        --broker-id <uuid> --symbol EURUSD.sd --timeframe M5 --hours 4

It only reads. Nothing is placed and nothing is subscribed. The cache file is written in your
temporary folder unless you name one with --cache-file.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mfx import MfxClient, SigningKey
from mfx.errors import RateLimitError

BASE_URL = "https://api.dev.mfxhub.com/v1"
#: The most bars one call returns.
PAGE = 1000
#: How long each timeframe's bar lasts; a bar is closed once its open time plus this has passed.
#: MN1 is left out: a month has no fixed length, so this sample does not cache monthly bars.
BAR = {
    "M1": dt.timedelta(minutes=1), "M5": dt.timedelta(minutes=5),
    "M15": dt.timedelta(minutes=15), "M30": dt.timedelta(minutes=30),
    "H1": dt.timedelta(hours=1), "H4": dt.timedelta(hours=4), "D1": dt.timedelta(days=1),
    "W1": dt.timedelta(weeks=1),
}  # fmt: skip
#: How many times this sample waits on its own after the SDK's retries are spent.
OWN_WAITS = 2


def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()


class BarCache:
    """Closed bars kept on disk, one series per (broker, server, symbol, timeframe)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.series: dict[str, dict[str, dict[str, Any]]] = {}
        if path.exists():
            self.series = json.loads(path.read_text(encoding="utf-8"))

    def bars(self, name: str) -> dict[str, dict[str, Any]]:
        return self.series.setdefault(name, {})

    def newest(self, name: str) -> dt.datetime | None:
        held = self.series.get(name)
        return max(dt.datetime.fromisoformat(ts) for ts in held) if held else None

    def save(self) -> None:
        self.path.write_text(json.dumps(self.series), encoding="utf-8")


async def candles_waiting_when_told(client: MfxClient, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - the SDK's CandleList
    """`client.md.candles`, waiting `Retry-After` when a terminal's reads for the minute are spent.

    The SDK has already waited and asked again a few times before it raises. A read that is still
    refused is told when the next minute starts: wait that long, then ask once more.
    """
    for wait in range(OWN_WAITS + 1):
        try:
            return await client.md.candles(*args, **kwargs)
        except RateLimitError as refused:
            if refused.code != "terminal_reads_limit" or wait == OWN_WAITS:
                raise
            seconds = refused.retry_after_s or 1
            limit = refused.details.get("limit")
            print(f"  this terminal's {limit} reads a minute are spent; waiting {seconds:g} s")
            await asyncio.sleep(seconds)
    raise AssertionError("unreachable")  # pragma: no cover


async def fill(  # noqa: PLR0913 - the read's own arguments
    client: MfxClient,
    cache: BarCache,
    name: str,
    broker_id: str,
    symbol: str,
    timeframe: str,
    *,
    start: dt.datetime,
    now: dt.datetime,
    server_id: str | None,
) -> list[str]:
    """Bring the cached series up to now, reading only what it does not hold. Answers the
    `source` of each read made, so you can see what each cost."""
    held = cache.bars(name)
    newest = cache.newest(name)
    # Only the range after the newest closed bar we hold: one small read, not the whole history.
    begin = newest + BAR[timeframe] if newest is not None else start
    sources: list[str] = []
    to = now
    while begin <= to:
        page = await candles_waiting_when_told(
            client, broker_id, symbol, timeframe, from_=begin, to=to, server_id=server_id
        )
        sources.append(str(page.source))
        for bar in page.items:
            if bar.ts + BAR[timeframe] <= now:  # closed: keep it; the forming bar is not kept
                held[bar.ts.isoformat()] = {"o": bar.o, "h": bar.h, "l": bar.l, "c": bar.c,
                                            "v": bar.v}  # fmt: skip
        if len(page.items) < PAGE:
            break
        to = page.items[0].ts - dt.timedelta(seconds=1)
    cache.save()
    return sources


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--broker-id", required=True)
    parser.add_argument("--symbol", required=True, help="the broker's own name, e.g. EURUSD.sd")
    parser.add_argument("--timeframe", default="M5", choices=sorted(BAR))
    parser.add_argument("--hours", type=float, default=4, help="how far back the first fill goes")
    parser.add_argument("--server-id", default=None, help="needed with terminals on several servers")
    parser.add_argument("--cache-file", type=Path, default=None)
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    key = SigningKey(private_key(args))
    path = args.cache_file or Path(tempfile.gettempdir()) / "mfx-candle-cache.json"
    cache = BarCache(path)
    name = f"{args.broker_id}|{args.server_id or ''}|{args.symbol}|{args.timeframe}"

    async with MfxClient(args.base_url, args.key_id, key) as client:
        now = dt.datetime.now(dt.UTC)
        start = now - dt.timedelta(hours=args.hours)
        first = await fill(client, cache, name, args.broker_id, args.symbol, args.timeframe,
                           start=start, now=now, server_id=args.server_id)  # fmt: skip
        held = len(cache.bars(name))
        print(f"{args.symbol} {args.timeframe}: {held} closed bars cached in {path}")
        print(f"  reads made: {len(first)}, answered by {', '.join(first) or 'nothing new'}")

        # A moment later: the cache already holds every closed bar, so the next read asks only
        # for the bars after the newest one - normally the bar still forming, and nothing else.
        later = dt.datetime.now(dt.UTC)
        second = await fill(client, cache, name, args.broker_id, args.symbol, args.timeframe,
                            start=start, now=later, server_id=args.server_id)  # fmt: skip
        print(f"  then: {len(second)} read for only what is new ({', '.join(second) or 'none'});"
              f" {len(cache.bars(name))} closed bars held")  # fmt: skip
        print("  follow new bars on the candles channel (md.candle_closed) instead of polling")


if __name__ == "__main__":
    asyncio.run(main())
