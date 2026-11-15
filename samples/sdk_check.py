"""Check that the Python SDK keeps the platform's promises against a real MFX Hub, with your key.

Every MFX Hub SDK passes a shared test suite before it releases, against a fake hub that can be
told how to answer. A handful of its rules a real hub shows on its own, and those are written as
live cases (``samples/suite/live.json``, from the suite's ``live/cases.json``). This sample runs
each of them through the SDK against your account:

- your signature is accepted, on a plain read and on a read with a query string;
- the suite's published test key is refused (a real hub accepts nothing it signs);
- a terminal you do not have is ``NotFoundError``, 404 ``not_found``;
- a list read one item a page follows the cursor and finds your terminal;
- a quote's bid and ask are exact decimals, never floats;
- a float volume is refused before anything is sent;
- ticks arrive with ``seq`` rising and none missing (skipped when the market is closed; not run
  when you have no subscription for the symbol, as ticks are streamed only for one);
- with ``--orders``: an order sent twice with one idempotency key is one order.

    MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" python samples/sdk_check.py \\
        --terminal-id <uuid> --broker-id <uuid> --server-id <uuid> --symbol EURUSD

The terminal must be logged in. Without ``--orders`` nothing is placed. ``--orders --confirm`` runs
only the cases that place an order: use a **demo** account, as it buys the symbol's smallest volume
once and closes it. On a closed market (a quote over a minute old, or no tick to quote at all) it
places nothing and ends skipped. A quote the hub cannot give for want of a tick (``503``,
``no_tick``) skips that case: there is no price to judge, which is not a wrong one. ``--json`` ends with one line the platform's checks read:
``{"outcome": "passed" | "amber" | "failed", "notice", "detail"}``, where ``detail.cases`` has
each case's outcome. Any case failed: failed. Any skipped: amber. A case not run for want of
something only you can set up (a subscription) is named in the notice and changes nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mfx import AuthError, Channel, MfxClient, NotFoundError, StreamMessage
from mfx.errors import MfxApiError

BASE_URL = os.environ.get("MFX_BASE_URL", "https://api.dev.mfxhub.com/v1")
SUITE = Path(__file__).resolve().parent / "suite"
#: How long a case that reads ticks waits for them before it says the market is not quoting.
TICK_WAIT_S = 30.0
#: How long an order's command may take to become final before the sample stops waiting.
ORDER_WAIT_S = 30.0
#: A quote older than this says the market is closed: ``--orders`` then places nothing.
STALE_QUOTE_S = 60.0
KINDS: dict[str, type[Exception]] = {"AuthError": AuthError, "NotFoundError": NotFoundError}


class Skip(Exception):
    """The case cannot be judged here: the market is not quoting, or orders were not allowed."""


class NotRun(Exception):
    """The case needs something this account has not set up, such as a subscription."""


def private_key() -> str:
    key = os.environ.get("MFX_PRIVATE_KEY", "").strip()
    if not key:
        raise SystemExit("set MFX_PRIVATE_KEY to your API private key")
    return key


def value(raw: Any, run: dict[str, str]) -> Any:  # noqa: ANN401 - a case's JSON value
    """A case value as Python: a run value, a float to be refused, or the value itself."""
    if isinstance(raw, dict) and "$run" in raw:
        return run[raw["$run"]]
    if isinstance(raw, dict) and "$float" in raw:
        return float(raw["$float"])
    if isinstance(raw, dict) and "$decimal" in raw:
        return str(raw["$decimal"])
    if isinstance(raw, list):
        return [value(item, run) for item in raw]
    return raw


async def call(client: MfxClient, op: str, a: dict[str, Any]) -> dict[str, Any]:
    """Make one call through the SDK and say what came back, in the suite's names."""
    if op == "terminals.get":
        terminal = await client.terminals.get(a["terminal_id"])
        return {"id": str(terminal.id), "state": str(terminal.state)}
    if op == "terminals.page":
        page = await client.terminals.page(limit=a.get("limit"))
        return {"count": len(page), "next_cursor": page.next_cursor}
    if op == "terminals.list":
        return {"ids": [str(t.id) async for t in client.terminals.list(limit=a.get("limit"))]}
    if op == "catalog.quote":
        try:
            quote = await quoted(client, a["broker_id"], a["symbol"], a.get("server_id"))
        except MfxApiError as error:
            if not no_tick(error):
                raise
            # A closed market sends no tick, and a symbol no terminal watches then has no price to
            # give (guides/when-a-call-fails.md): there is nothing to judge, which is not a wrong one.
            raise Skip("no price to read now: the terminal saw no tick in time (no_tick)") from error
        return {"bid": quote.bid, "ask": quote.ask, "age_s": quote.age_s, "state": str(quote.state)}
    if op == "execution.place_order":
        terminal_id = a.pop("terminal_id")
        result = await client.execution.place_order(terminal_id, **a)
        return {
            "ok": result.ok,
            "state": str(result.state) if result.state else None,
            "command_id": result.command_id,
            "duplicate": result.duplicate,
            "position": result.tickets.position,
        }
    if op == "stream.read":
        return await read_ticks(client, a["channels"], a["count"])
    raise AssertionError(f"this sample does not know the operation {op!r}")


def no_tick(error: MfxApiError) -> bool:
    """The hub had no tick to price the symbol with: ``503 upstream_error``, ``details.reason`` no_tick."""
    return error.code == "upstream_error" and error.details.get("reason") == "no_tick"


#: How many listings of the instrument a quote is asked of before giving up.
TRIES = 6


def is_variant(name: str, base: str) -> bool:
    """`EURUSD`, `EURUSD.sd` and `EURUSDm` are the same instrument; `EURUSD2` and `EURUSDX` too
    long a suffix to be sure of are not (as samples/price_check.py)."""
    upper, base = name.upper(), base.upper()
    if upper == base:
        return True
    rest = upper[len(base):]
    return upper.startswith(base) and 0 < len(rest) <= 4 and not rest[0].isdigit()


async def quoted(client: MfxClient, broker_id: str, symbol: str, server_id: str | None) -> Any:  # noqa: ANN401
    """The symbol's quote, from a listing that has one to give.

    A server may list one instrument under several names and account types (``EURUSD`` and
    ``EURUSD.sd``). A quote that names none is refused as ambiguous, and a listing no terminal
    watches has no tick to price itself with (``503``, ``no_tick``): the price is then asked of
    every listing of the instrument, the name as given first, until one answers. The case judges
    the SDK's decimals, not which listing a terminal happens to watch."""
    try:
        return await client.catalog.quote(broker_id, symbol, server_id=server_id)
    except MfxApiError as refused:
        if refused.code != "ambiguous_symbol" and not no_tick(refused):
            raise
        last = refused
    listed: list[Any] = []
    cursor = None
    while True:
        page = await client.catalog.symbols(broker_id, server_id=server_id, cursor=cursor, limit=200)
        listed += [s for s in page.items if is_variant(s.symbol, symbol)]
        cursor = page.next_cursor
        if not cursor:
            break
    listed.sort(key=lambda s: (s.symbol != symbol, len(s.symbol)))
    for one in listed[:TRIES]:
        try:
            return await client.catalog.quote(broker_id, one.symbol, server_id=server_id,
                                              account_type=one.account_type or None)  # fmt: skip
        except MfxApiError as refused:
            last = refused
    raise last


async def read_ticks(client: MfxClient, channels: list[str], count: int) -> dict[str, Any]:
    """Read ``count`` messages from the channels; Skip when they do not come in time, NotRun when
    there is no subscription to stream them."""
    for channel in channels:
        _, server_id, symbol = channel.split(":", 2)
        found = [s async for s in client.md.all_subscriptions()
                 if str(s.server.id) == server_id and s.symbol == symbol]  # fmt: skip
        if not found:
            raise NotRun(f"no subscription for {symbol} on that server, so no ticks are streamed")
    seqs: list[int | None] = []
    async with client.stream() as stream:
        for channel in channels:
            await stream.subscribe(channel)

        async def read() -> None:
            async for event in stream:
                if isinstance(event, StreamMessage):
                    seqs.append(event.seq)
                    if len(seqs) >= count:
                        return

        try:
            await asyncio.wait_for(read(), timeout=TICK_WAIT_S)
        except TimeoutError:
            raise Skip(f"{len(seqs)} of {count} ticks in {TICK_WAIT_S:.0f} s: the market is not quoting") from None
        dropped = stream.stats.dropped
    return {"seqs": seqs, "dropped": dropped}


def exact(got: Any) -> bool:  # noqa: ANN401
    """An exact decimal above zero: a Decimal or a decimal string, never a float."""
    if isinstance(got, float) or not isinstance(got, (Decimal, str)):
        return False
    try:
        return Decimal(got) > 0
    except InvalidOperation:
        return False


def same(got: Any, want: Any) -> bool:  # noqa: ANN401
    return bool(str(got) == str(want)) if want is not None and got is not None else got == want


async def close_opened(client: MfxClient, results: list[dict[str, Any]], terminal_id: str) -> None:
    """Close the position an order opened, once its command is final. Never leaves one open."""
    for result in results:
        if result.get("duplicate"):
            continue
        position = result.get("position")
        if position is None and result.get("command_id"):
            done = await client.execution.wait_for(result["command_id"], timeout_s=ORDER_WAIT_S)
            position = done.tickets.position
        if position is not None:
            closed = await client.execution.close_position(
                terminal_id, position, idempotency_key=f"sdk-check-close-{uuid.uuid4().hex[:12]}"
            )
            if closed.command_id:
                await client.execution.wait_for(closed.command_id, timeout_s=ORDER_WAIT_S)
            print(f"  closed position {position}")


async def judge(case: dict[str, Any], client: MfxClient, test_key: MfxClient,
                run: dict[str, str], confirm: bool) -> tuple[str, str | None]:  # fmt: skip
    """One case: passed, failed or skipped, and why when it is not passed."""
    observe = case["observe"]
    op = case["call"]["op"]
    args = {name: value(raw, run) for name, raw in case["call"]["args"].items()}
    who = test_key if case.get("client") == "test_key" else client
    if "raises" in observe:
        try:
            got = await call(who, op, args)
        except MfxApiError as error:
            if not isinstance(error, KINDS[observe["raises"]]):
                return "failed", f"raised {type(error).__name__} {error.status} {error.code}"
            wrong = {k: getattr(error, k) for k, v in observe.get("fields", {}).items()
                     if getattr(error, k) != v}  # fmt: skip
            return ("failed", f"raised with {wrong}") if wrong else ("passed", None)
        return "failed", f"returned {got}, the case wants {observe['raises']}"
    if "refuses" in observe:
        try:
            got = await call(who, op, args)
        except (TypeError, ValueError):
            return "passed", None
        return "failed", f"sent and returned {got}, the case wants it refused before sending"
    if "repeat" in observe:
        if not confirm:
            raise Skip("places an order: pass --confirm, on a demo account")
        first = await call(who, op, dict(args))
        second = await call(who, op, dict(args))
        try:
            want = observe["repeat"]
            bad = [k for k, v in want.get("returns", {}).items() if not same(second[k], v)]
            bad += [k for k in want.get("same", []) if not same(second[k], first[k])]
            return ("failed", f"the second call differs on {bad}: {first} then {second}") if bad else ("passed", None)
        finally:
            await close_opened(client, [first, second], run["terminal_id"])
    got = await call(who, op, args)
    problems: list[str] = []
    for name, want in observe.get("returns", {}).items():
        if not same(got[name], value(want, run)):
            problems.append(f"{name} is {got[name]!r}, the case wants {value(want, run)!r}")
    for name, want in observe.get("includes", {}).items():
        if str(value(want, run)) not in [str(item) for item in got[name]]:
            problems.append(f"{name} does not include {value(want, run)}")
    problems += [f"{name} is {got[name]!r}, not an exact decimal above zero"
                 for name in observe.get("exact", []) if not exact(got[name])]  # fmt: skip
    if "ascending" in observe:
        seq = got[observe["ascending"]]
        if not seq or any(b is None or a is None or b <= a for a, b in zip(seq, seq[1:], strict=False)):
            problems.append(f"{observe['ascending']} {seq} does not rise")
    return ("failed", "; ".join(problems)) if problems else ("passed", None)


async def main(args: argparse.Namespace) -> int:
    every: list[dict[str, Any]] = json.loads((SUITE / "live.json").read_text(encoding="utf-8"))
    # The cases that place an order run apart, where orders are expected; the rest only read.
    cases = [c for c in every if ("repeat" in c["observe"]) == args.orders]
    vocabulary = json.loads((SUITE / "vocabulary.json").read_text(encoding="utf-8"))
    version = (SUITE / "VERSION").read_text(encoding="utf-8").strip()
    seed = base64.b64encode(bytes.fromhex(vocabulary["key"]["seed_hex"])).decode()
    results: dict[str, str] = {}
    why: dict[str, str] = {}
    async with (
        MfxClient(args.base_url, args.key_id, private_key()) as client,
        MfxClient(args.base_url, vocabulary["key"]["id"], seed) as test_key,
    ):
        spec = await client.catalog.symbol(args.broker_id, args.symbol, server_id=args.server_id)
        if args.orders:
            # An order on a closed market would only be refused: say so and place nothing.
            try:
                quote = await quoted(client, args.broker_id, args.symbol, args.server_id)
                notice = (f"the last price is {float(quote.age_s):.0f} s old: the market looks closed"
                          if float(quote.age_s) > STALE_QUOTE_S else None)  # fmt: skip
            except MfxApiError as error:
                if not no_tick(error):
                    raise
                notice = "no price to read now (no_tick): the market looks closed"
            if notice is not None:
                print(f"sdk check: skipped - {notice}")
                if args.json:
                    detail = {"suite": version, "library": "python", "part": "orders", "cases": {}}
                    print(json.dumps({"outcome": "skipped", "notice": notice, "detail": detail}))
                return 0
        print(f"suite {version}: {len(cases)} live cases against {args.base_url}")
        for case in cases:
            run = {
                "terminal_id": args.terminal_id, "broker_id": args.broker_id,
                "server_id": args.server_id, "symbol": args.symbol,
                "md_channel": Channel.md(args.server_id, args.symbol),
                "min_volume": str(spec.volume_min), "unused_id": str(uuid.uuid4()),
                "new_key": f"sdk-check-{uuid.uuid4().hex[:16]}",
            }  # fmt: skip
            try:
                outcome, reason = await judge(case, client, test_key, run, args.confirm)
            except Skip as skipped:
                outcome, reason = "skipped", str(skipped)
            except NotRun as missing:
                outcome, reason = "not_run", str(missing)
            except MfxApiError as error:
                outcome, reason = "failed", f"{type(error).__name__} {error.status} {error.code}"
            results[case["id"]] = outcome
            if reason:
                why[case["id"]] = reason
            print(f"{case['id']}: {outcome}" + (f" - {reason}" if reason else ""))
    failed = [i for i, o in results.items() if o == "failed"]
    skipped = [i for i, o in results.items() if o == "skipped"]
    not_run = [i for i, o in results.items() if o == "not_run"]
    outcome = "failed" if failed else "amber" if skipped else "passed"
    said = [f"{len(failed)} of {len(results)} failed: {', '.join(failed)}" if failed else "",
            f"{len(skipped)} skipped: {', '.join(skipped)}" if skipped else "",
            f"{len(not_run)} not run: {', '.join(not_run)}" if not_run else ""]  # fmt: skip
    notice = "; ".join(part for part in said if part) or None
    for case_id in failed:  # last, so a short tail of the output still says why
        print(f"  {case_id}: {why[case_id]}")
    print(f"sdk check: {outcome}" + (f" - {notice}" if notice else ""))
    if args.json:
        part = "orders" if args.orders else "reads"
        detail = {"suite": version, "library": "python", "part": part, "cases": results, "why": why}
        print(json.dumps({"outcome": outcome, "notice": notice, "detail": detail}))
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--terminal-id", required=True, help="a terminal that is logged in")
    parser.add_argument("--broker-id", required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--symbol", required=True, help="the broker's own symbol name")
    parser.add_argument("--orders", action="store_true", help="run only the cases that place an order")
    parser.add_argument("--confirm", action="store_true", help="allow the one order (demo accounts)")
    parser.add_argument("--json", action="store_true", help="end with the check's result line")
    parser.add_argument("--base-url", default=BASE_URL)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
