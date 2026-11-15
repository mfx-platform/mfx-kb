"""Ask for the price of a symbol whose market is closed now, and see the answer you get. The
runnable half of the "A market that is closed" section of guides/when-a-call-fails.md.

Three things to take away:

1. **A closed market's quote is not a price to act on.** A symbol one of your terminals watches
   answers with its last price and `state` `closed_session`. One that no terminal watches has no
   price to give: the terminal waits for a tick that a closed market does not send, and the call
   answers `503 upstream_error` with `details.reason` `no_tick`. Read `state`, and check the
   symbol's session when a quote says `no_tick`.
2. **An order is what a closed market refuses.** Sent outside the session it comes back with the
   reason `market_closed`, and nothing is placed.
3. **Sessions are on the broker's clock.** `trade_sessions` are broker time; a quote's
   `broker_time` is its `tick_time` on that clock, which gives you the difference to UTC.

Run it against dev:

    python samples/closed_market.py --key-id mfxk_... --private-key-file key.txt \
        --broker-id <uuid> --server-id <uuid>

It only reads. `--json` ends with one line the platform's checks read:
{"outcome": "passed" | "skipped" | "failed", "notice", "detail"}. Skipped when no listed symbol is
closed now.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from mfx import MfxClient
from mfx.errors import MfxApiError

BASE_URL = "https://api.dev.mfxhub.com/v1"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
#: Only a symbol this far from either end of a session is used, so a few minutes' error in the
#: clock difference cannot make an open market look closed.
MARGIN = dt.timedelta(minutes=30)
#: Asked first: the instruments terminals most often watch, so a quote has a price to give.
FIRST = ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD", "ETHUSD")
#: How many closed symbols are asked before giving up.
TRIES = 3


def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()


async def catalogue(client: MfxClient, broker_id: str, server_id: str) -> list[Any]:
    """Every symbol the server lists, 200 a page."""
    found: list[Any] = []
    cursor = None
    while True:
        page = await client.catalog.symbols(broker_id, server_id=server_id, cursor=cursor,
                                            limit=200)  # fmt: skip
        found.extend(page.items)
        cursor = page.next_cursor
        if not cursor:
            return found


def is_variant(name: str, base: str) -> bool:
    """`EURUSD`, `EURUSD.sd` and `EURUSDm` are the same instrument."""
    upper = name.upper()
    rest = upper[len(base):]
    return upper == base or (upper.startswith(base) and 0 < len(rest) <= 4 and not rest[0].isdigit())


def first_rank(symbol: Any) -> int:  # noqa: ANN401
    """Where a symbol comes in the order of asking: the usual instruments first."""
    for rank, base in enumerate(FIRST):
        if is_variant(symbol.symbol, base):
            return rank
    return len(FIRST)


def broker_offset(quote: Any) -> dt.timedelta:  # noqa: ANN401
    """The broker's clock minus UTC, from one quote: `broker_time` is `tick_time` on that clock."""
    broker = dt.datetime.fromisoformat(quote.broker_time)
    utc = quote.tick_time.astimezone(dt.UTC).replace(tzinfo=None)
    return dt.timedelta(minutes=round((broker - utc).total_seconds() / 60))


def _minutes(text: str) -> int:
    hh, mm = (int(x) for x in text.split(":")[:2])
    return hh * 60 + mm


def account_type(symbol: Any) -> str | None:  # noqa: ANN401
    """The account type the catalogue lists this symbol under: a server may list one symbol under
    several, and a quote that does not name one is refused as ambiguous."""
    value = getattr(symbol, "account_type", None)
    return value if isinstance(value, str) and value else None


def tradable(symbol: Any) -> bool:  # noqa: ANN401
    """False for a symbol the broker has disabled: its quote says `trade_disabled`, not closed."""
    mode = getattr(symbol, "trade_mode", None)
    return getattr(mode, "value", mode) != "disabled"


def surely_closed(sessions: list[Any] | None, broker_now: dt.datetime) -> bool:
    """True when no session is open within MARGIN of the broker's now, before or after."""
    if not sessions:
        return False  # no sessions known: nothing can be said
    for moment in (broker_now - MARGIN, broker_now, broker_now + MARGIN):
        day, minute = DAYS[moment.weekday()], moment.hour * 60 + moment.minute
        for s in sessions:
            if s.day.value == day and _minutes(s.from_) <= minute < _minutes(s.to):
                return False
    return True


async def clock(client: MfxClient, args: argparse.Namespace, symbols: list[Any]) -> Any | None:
    """A quote that carries the broker's clock: the first usual instrument that has a price."""
    for symbol in [s for s in symbols if first_rank(s) < len(FIRST)][:TRIES]:
        try:
            return await client.catalog.quote(args.broker_id, symbol.symbol,
                                              server_id=args.server_id,
                                              account_type=account_type(symbol))  # fmt: skip
        except MfxApiError:
            continue
    return None


def judge(name: str, answer: Any) -> tuple[str, str | None, dict[str, Any]]:  # noqa: ANN401
    """Passed on either documented answer for a closed market; anything else failed."""
    if isinstance(answer, MfxApiError):
        reason = answer.details.get("reason")
        detail = {"symbol": name, "status": answer.status, "code": answer.code, "reason": reason}
        if answer.status == 503 and answer.code == "upstream_error" and reason == "no_tick":
            return "passed", None, {**detail, "form": "no_tick"}
        return "failed", f"{name} answered {answer.status} {answer.code} ({reason})", detail
    detail = {"symbol": answer.symbol, "state": answer.state.value, "bid": answer.bid,
              "ask": answer.ask, "age_s": answer.age_s}  # fmt: skip
    if answer.state.value == "closed_session":
        return "passed", None, {**detail, "form": "closed_session"}
    return "failed", f"{name}'s sessions say closed, but its quote says {answer.state.value}", detail


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--broker-id", required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--json", action="store_true", help="end with the check's result line")
    parser.add_argument("--base-url", default=os.environ.get("MFX_BASE_URL", BASE_URL))
    args = parser.parse_args()

    detail: dict[str, Any] = {}
    async with MfxClient(args.base_url, args.key_id, private_key(args)) as client:
        symbols = sorted((s for s in await catalogue(client, args.broker_id, args.server_id)
                          if s.trade_sessions), key=first_rank)  # fmt: skip
        first = await clock(client, args, symbols)
        if first is None:
            outcome, notice = "skipped", "no usual instrument on this server has a price to time by"
        else:
            broker_now = dt.datetime.now(dt.UTC).replace(tzinfo=None) + broker_offset(first)
            # A symbol the broker has disabled answers `trade_disabled` whatever its sessions say,
            # which is a different lesson: only symbols open to trade at other times are asked.
            closed = [s for s in symbols
                      if tradable(s) and surely_closed(s.trade_sessions, broker_now)]  # fmt: skip
            if not closed:
                outcome, notice = "skipped", "no listed symbol is closed now"
            else:
                for symbol in closed[:TRIES]:
                    try:
                        answer: Any = await client.catalog.quote(
                            args.broker_id, symbol.symbol, server_id=args.server_id,
                            account_type=account_type(symbol),
                        )
                    except MfxApiError as refused:
                        answer = refused
                    outcome, notice, detail = judge(symbol.symbol, answer)
                    print(f"{symbol.symbol}: {outcome}" + (f" - {notice}" if notice else ""),
                          json.dumps(detail))  # fmt: skip
                    if outcome == "passed":
                        break
    print(f"closed market: {outcome}" + (f" - {notice}" if notice else ""))
    if args.json:
        print(json.dumps({"outcome": outcome, "notice": notice, "detail": detail}))
    return 1 if outcome == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
