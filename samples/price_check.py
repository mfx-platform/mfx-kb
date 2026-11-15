"""Read one live price and judge it: bid, ask and spread. The runnable half of the "Is this price
real?" section of guides/following-prices-live.md.

Three things to take away:

1. **Use the broker's own name.** Brokers add a suffix of their own (`EURUSD.sd`, `BTCUSD.lv`).
   This sample finds the symbol in your server's catalogue rather than guessing it.
2. **A price has two sides.** The ask is never below the bid. When the two are equal the feed may
   not be a real quote; when a side is missing, there is nothing to trade on.
3. **A closed market still answers.** Outside its trading session a quote comes back with
   `state` `closed_session`, not an error. The symbol's `trade_sessions` say when it opens again,
   on the broker's clock.

Run it against dev:

    python samples/price_check.py --key-id mfxk_... --private-key-file key.txt \
        --broker-id <uuid> --server-id <uuid> --kind forex

`--kind crypto` checks BTCUSD, then ETHUSD. It only reads. `--json` ends with one line the
platform's checks read: {"outcome": "passed" | "skipped" | "amber" | "failed", "notice", "detail"}.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mfx import MfxApiError, MfxClient

BASE_URL = "https://api.dev.mfxhub.com/v1"
#: Tried in this order; the first the server lists is used.
CANDIDATES = {"forex": ("EURUSD", "GBPUSD", "USDJPY"), "crypto": ("BTCUSD", "ETHUSD")}
#: How many catalogue entries to ask before giving up: one symbol may be listed under several
#: account types, and not every one has a live price.
TRIES = 6
#: A price older than this, while its market is open, cannot be called live.
FRESH_S = 60.0
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()


def is_variant(name: str, base: str) -> bool:
    """`EURUSD`, `EURUSD.sd` and `EURUSDm` are the same instrument; `EURUSD2` and `EURUSDX` too
    long a suffix to be sure of are not."""
    upper = name.upper()
    if upper == base:
        return True
    rest = upper[len(base):]
    return upper.startswith(base) and 0 < len(rest) <= 4 and not rest[0].isdigit()


async def catalogue(client: MfxClient, broker_id: str, server_id: str) -> list[Any]:
    """Every symbol the server lists, a page at a time."""
    found: list[Any] = []
    cursor = None
    while True:
        page = await client.catalog.symbols(broker_id, server_id=server_id, cursor=cursor,
                                            limit=200)  # fmt: skip
        found.extend(page.items)
        cursor = page.next_cursor
        if not cursor:
            return found


def ranked(symbols: list[Any], bases: tuple[str, ...]) -> list[Any]:
    """Every listing of the bases, in the bases' order and each one's plainest name first; a symbol
    listed under several account types appears once for each."""
    found: list[Any] = []
    for base in bases:
        found += sorted((s for s in symbols if is_variant(s.symbol, base)), key=lambda s: len(s.symbol))
    return found


def pick(symbols: list[Any], bases: tuple[str, ...]) -> Any | None:
    """The first base the server lists, its plainest name first."""
    found = ranked(symbols, bases)
    return found[0] if found else None


async def first_quote(client: MfxClient, args: argparse.Namespace,
                      listed: list[Any]) -> tuple[Any, Any]:  # fmt: skip
    """The first listing with a tradable price, else the first with any price. Raises the last
    refusal when none answers."""
    fallback, refused = None, None
    for symbol in listed[:TRIES]:
        try:
            quote = await client.catalog.quote(args.broker_id, symbol.symbol,
                                               server_id=args.server_id,
                                               account_type=account_type(symbol))  # fmt: skip
        except MfxApiError as error:
            refused = error
            continue
        if quote.state.value != "trade_disabled":
            return symbol, quote
        fallback = fallback or (symbol, quote)
    if fallback is not None:
        return fallback
    assert refused is not None
    raise refused


def account_type(symbol: Any) -> str | None:  # noqa: ANN401
    """The account type the catalogue lists this symbol under: a server may list one symbol under
    several, and a quote that does not name one is refused as ambiguous."""
    value = getattr(symbol, "account_type", None)
    return value if isinstance(value, str) and value else None


def side(raw: str | None) -> Decimal | None:
    try:
        value = Decimal(raw) if raw not in (None, "") else None
    except InvalidOperation:
        return None
    return value if value is not None and value > 0 else None


def broker_offset(quote: Any) -> dt.timedelta:
    """The broker's clock minus UTC, from one quote: `broker_time` is `tick_time` on that clock."""
    broker = dt.datetime.fromisoformat(quote.broker_time)
    utc = quote.tick_time.astimezone(dt.UTC).replace(tzinfo=None)
    return dt.timedelta(minutes=round((broker - utc).total_seconds() / 60))


def next_open(sessions: list[Any] | None, offset: dt.timedelta, now: dt.datetime) -> str | None:
    """When the symbol next opens, in UTC, from its sessions on the broker's clock."""
    if not sessions:
        return None
    broker_now = now.replace(tzinfo=None) + offset
    starts = []
    for ahead in range(8):
        day = (broker_now + dt.timedelta(days=ahead)).date()
        for s in sessions:
            if DAYS[day.weekday()] != s.day.value:
                continue
            hh, mm = (int(x) for x in s.from_.split(":")[:2])
            start = dt.datetime.combine(day, dt.time(hh, mm))
            if start > broker_now:
                starts.append(start)
    if not starts:
        return None
    return (min(starts) - offset).replace(tzinfo=dt.UTC).isoformat().replace("+00:00", "Z")


def judge(kind: str, symbol: Any, quote: Any, now: dt.datetime) -> tuple[str, str | None, dict]:
    """The outcome, the one-line notice and what was seen."""
    bid, ask = side(quote.bid), side(quote.ask)
    detail = {"symbol": quote.symbol, "bid": quote.bid, "ask": quote.ask,
              "spread": str(ask - bid) if bid is not None and ask is not None else None,
              "age_s": quote.age_s, "state": quote.state.value}  # fmt: skip
    if quote.state.value == "closed_session":
        opens = next_open(symbol.trade_sessions, broker_offset(quote), now)
        detail["next_open"] = opens
        return "skipped", f"{kind} closed until {opens or 'its next session'}", detail
    if bid is None or ask is None:
        return "failed", "a side of the price is missing", detail
    if ask < bid:
        return "failed", f"the ask {ask} is below the bid {bid}", detail
    if ask == bid:
        return "amber", "bid and ask are the same: the feed may not be a real quote", detail
    # DECISION: a stale price while the market is open is amber ("could not be verified now"),
    # not failed: the answer is right, the market has simply not moved for a while.
    if quote.age_s > FRESH_S:
        return "amber", f"the newest price is {quote.age_s:.0f} s old", detail
    return "passed", None, detail


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--broker-id", required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--kind", choices=sorted(CANDIDATES), default="forex")
    parser.add_argument("--json", action="store_true", help="end with the check's result line")
    parser.add_argument("--base-url", default=os.environ.get("MFX_BASE_URL", BASE_URL))
    args = parser.parse_args()

    async with MfxClient(args.base_url, args.key_id, private_key(args)) as client:
        symbols = await catalogue(client, args.broker_id, args.server_id)
        listed = ranked(symbols, CANDIDATES[args.kind])
        if not listed:
            outcome, notice, detail = "skipped", f"no {args.kind} on this server", {}
        else:
            try:
                symbol, quote = await first_quote(client, args, listed)
            except MfxApiError as error:
                if error.code != "upstream_error" or error.details.get("reason") != "no_tick":
                    raise
                # DECISION: no tick in time is amber ("could not be verified now"), like a stale
                # price: a slow weekend crypto feed may not tick within the hub's wait, and a closed
                # market sends none to a symbol no terminal watches. Nothing wrong was answered.
                tried = [s.symbol for s in listed[:TRIES]]
                outcome, notice = "amber", f"no {args.kind} price now: no tick in time from {', '.join(tried)}"
                detail = {"reason": "no_tick", "tried": tried}
            else:
                outcome, notice, detail = judge(args.kind, symbol, quote, dt.datetime.now(dt.UTC))
                print(f"{quote.symbol}: bid {quote.bid}  ask {quote.ask}  spread {detail['spread']}  "
                      f"{quote.age_s:.1f} s old, {quote.state.value}")  # fmt: skip
    print(f"{args.kind}: {outcome}" + (f" - {notice}" if notice else ""))
    if args.json:
        print(json.dumps({"outcome": outcome, "notice": notice, "detail": detail}))
    return 1 if outcome == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
