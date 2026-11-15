"""A position from open to close, changed on the way: the runnable half of guides/changing-a-position.md.

It finds your farm, broker and server by name, asks for a terminal, logs it in, buys twice the
smallest volume, sets a stop loss and a take profit, closes half, puts a trailing stop on the rest,
takes the trailing stop off, closes the position, and retires the terminal. Use a **demo** account:
the orders are real on whatever account you log in to.

    MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \\
    MFX_BROKER=Pepperstone MFX_SERVER=Pepperstone-Demo MFX_LOGIN=12345678 \\
    python samples/position_changes.py --symbol EURUSD --confirm

Without ``--confirm`` it stops after the quote and opens nothing. Whatever happens, it closes the
position it opened and retires the terminal before it ends, so a run leaves nothing behind. The
broker password is typed at a prompt: never an argument, an environment variable or a file.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import time
import uuid
from collections.abc import Callable
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import mfx
from mfx import ExecutionResult, MfxClient, NotFoundError, SigningKey

BASE_URL = os.environ.get("MFX_BASE_URL", "https://api.dev.mfxhub.com/v1")
#: How long to wait for a terminal you retired to read RETIRED. A drain takes seconds; this is
#: only the point at which the sample stops waiting and says so.
RETIRE_WAIT_S = 120
RETIRE_POLL_S = 2
#: How far the stop loss and take profit sit from the price: wide, so neither is hit while it runs.
AWAY = Decimal("0.02")
#: How long to wait for the positions list to show a change (it is a snapshot of the account).
LIST_WAIT_S = 30


async def wait_until_retired(
    client: MfxClient,
    terminal_id: uuid.UUID | str,
    *,
    wait_s: float = RETIRE_WAIT_S,
    poll_s: float = RETIRE_POLL_S,
) -> bool:
    """Wait until a terminal you retired reads RETIRED, or is gone. False if not within ``wait_s``.

    Retiring is accepted at once and the terminal reads DRAINING; it keeps its broker session until
    its cell has stopped it and it reads RETIRED. On a login whose server allows only so many
    sessions, wait for RETIRED before you start another terminal on that login.
    """
    deadline = time.monotonic() + wait_s
    while True:
        try:
            terminal = await client.terminals.get(terminal_id)
        except NotFoundError:
            return True  # nothing is left to hold a session
        if terminal.state == "RETIRED":
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll_s)


def terminal_labels() -> dict[str, str]:
    """Labels for every terminal this sample asks for: MFX_TERMINAL_LABELS, a JSON object, when set.

    Optional. Labels are yours to sort and find terminals by; nothing here needs one.
    """
    labels = json.loads(os.environ.get("MFX_TERMINAL_LABELS") or "{}")
    return {str(key): str(value) for key, value in labels.items()}


async def find_ids(client: MfxClient, broker: str, server: str) -> tuple[str, str, str, set[str]]:
    """The farm, broker and server ids for the names you know, and what the farm offers."""
    wanted = os.environ.get("MFX_FARM_ID")
    farm = None
    async for candidate in client.farms.list():
        offers = {str(usage) for usage in candidate.usages}
        if str(candidate.id) == wanted or (not wanted and "order_execution" in offers):
            farm = candidate
            break
    if farm is None:
        raise SystemExit("no farm on your plan executes orders; ask support which one to use")
    broker_row = None
    async for row in client.catalog.all_brokers():
        if broker.lower() in {row.code.lower(), row.display_name.lower()}:
            broker_row = row
            break
    if broker_row is None:
        raise SystemExit(f"no broker called {broker!r}; client.catalog.all_brokers() lists them")
    servers = await client.catalog.servers(broker_row.id)
    server_row = next((s for s in servers if s.server_name.lower() == server.lower()), None)
    if server_row is None or not server_row.enabled:
        names = ", ".join(s.server_name for s in servers if s.enabled)
        raise SystemExit(f"server {server!r} is not available; {broker_row.display_name} offers {names}")
    print(f"farm {farm.name}, broker {broker_row.display_name}, server {server_row.server_name}")
    return str(farm.id), str(broker_row.id), str(server_row.id), offers


def on_tick(price: Decimal, spec: Any) -> str:
    """A price the broker accepts: down onto the symbol's tick, written with its digits."""
    digits = int(spec.digits)
    tick = Decimal(str(getattr(spec, "tick_size", None) or getattr(spec, "point", None)
                       or Decimal(1).scaleb(-digits)))  # fmt: skip
    snapped = (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    return format(snapped.quantize(Decimal(1).scaleb(-digits)), "f")


async def final(client: MfxClient, result: ExecutionResult) -> ExecutionResult:
    """The command's final result. Execution never raises, so every step waits and reads state."""
    return await client.execution.wait_for(result.command_id) if result.command_id else result


def check(step: str, result: ExecutionResult) -> None:
    print(f"{step}: {result.state}")
    if str(result.state) != "SUCCEEDED":
        raise SystemExit(f"{step} did not succeed: {result.reason_text or result.detail}")


async def position(client: MfxClient, terminal_id: uuid.UUID | str, ticket: int) -> Any:
    """The position by its ticket, from the account's latest snapshot; None once it is closed."""
    rows = (await client.accounts.positions(terminal_id)).items
    return next((p for p in rows if p.ticket == ticket), None)


async def until(
    client: MfxClient, terminal_id: uuid.UUID | str, ticket: int, seen: Callable[[Any], Any], what: str
) -> Any:
    """Wait for the position to show a change: the list is read from a snapshot of the account."""
    for _ in range(LIST_WAIT_S):
        row = await position(client, terminal_id, ticket)
        if seen(row):
            return row
        await asyncio.sleep(1)
    raise SystemExit(f"position {ticket}: {what} not seen after {LIST_WAIT_S} s")



async def priced(
    client: MfxClient, broker_id: str, symbol: str, server_id: str
) -> tuple[Any, str | None]:
    """The symbol's quote, and the account type it was asked under. A server may list one symbol
    under several account types; a quote that names none is then refused as ambiguous, so it is
    asked again under each listing's account type until one answers."""
    try:
        return await client.catalog.quote(broker_id, symbol, server_id=server_id), None
    except mfx.MfxApiError as refused:
        if refused.code != "ambiguous_symbol":
            raise
        last = refused
    cursor = None
    while True:
        page = await client.catalog.symbols(broker_id, server_id=server_id, cursor=cursor, limit=200)
        for listed in (s for s in page.items if s.symbol == symbol and s.account_type):
            try:
                quote = await client.catalog.quote(broker_id, symbol, server_id=server_id,
                                                   account_type=listed.account_type)  # fmt: skip
            except mfx.MfxApiError as refused:
                last = refused
                continue
            return quote, listed.account_type
        cursor = page.next_cursor
        if not cursor:
            raise last

async def main(symbol: str, confirm: bool) -> None:
    run = uuid.uuid4().hex[:8]  # one run, one set of idempotency keys: a retry repeats nothing
    mark = f"mfx-sample-{run}"  # on the order's comment, so this run finds its own position
    async with MfxClient(
        BASE_URL, os.environ["MFX_KEY_ID"], SigningKey(os.environ["MFX_PRIVATE_KEY"])
    ) as client:
        farm_id, broker_id, server_id, offers = await find_ids(
            client, os.environ["MFX_BROKER"], os.environ["MFX_SERVER"]
        )
        execution = client.execution

        # 1. A terminal for your account, as in your first order.
        terminal = await client.terminals.create(
            farm_id=farm_id,
            broker_id=broker_id,
            server_id=server_id,
            streaming="quote_streaming" in offers,
            execution="order_execution" in offers,
            instruments=[symbol],
            labels=terminal_labels(),
            idempotency_key=f"position-terminal-{run}",
        )
        try:
            await client.terminals.submit_credentials(
                terminal.id,
                login=int(os.environ["MFX_LOGIN"]),
                password=mfx.Secret(getpass.getpass("Broker password: ")),
                server=os.environ["MFX_SERVER"],
                idempotency_key=f"position-credential-{run}",
            )
            while not mfx.is_usable(terminal := await client.terminals.get(terminal.id)):
                if terminal.state in ("FAILED", "RETIRED"):
                    raise SystemExit(f"the terminal ended {terminal.state}: read its conditions")
                print(terminal.state)
                await asyncio.sleep(5)
            print(f"terminal {terminal.id} is ACTIVE")

            # 2. A quote and the symbol's rules.
            quote, account_type = await priced(client, broker_id, symbol, server_id)
            spec = await client.catalog.symbol(broker_id, symbol, server_id=server_id,
                                   account_type=account_type)
            print(f"{symbol} bid {quote.bid} ask {quote.ask}, {quote.age_s:.1f} s old")
            if not confirm:
                print("not opening a position: pass --confirm to open, change and close one")
                return

            # 3. Buy twice the smallest volume, so half of it can be closed on its own.
            half = Decimal(str(spec.volume_min))
            opened = await final(
                client,
                await execution.place_order(
                    terminal.id,
                    symbol=symbol,
                    side="buy",
                    volume=str(half * 2),
                    comment=mark,
                    idempotency_key=f"position-buy-{run}",
                ),
            )
            check(f"buy {half * 2}", opened)
            ticket = opened.tickets.position
            if ticket is None:
                raise SystemExit("the order filled but named no position")
            print(f"position {ticket} opened at {opened.exec_price}")

            # 4. A stop loss and a take profit, each 2 % from the price.
            price = Decimal(str(opened.exec_price or quote.ask))
            sl, tp = on_tick(price * (1 - AWAY), spec), on_tick(price * (1 + AWAY), spec)
            changed = await execution.modify_position(
                terminal.id, ticket, sl=sl, tp=tp, idempotency_key=f"position-sltp-{run}"
            )
            check(f"stop loss {sl}, take profit {tp}", await final(client, changed))

            # 5. Close half. The rest stays open under the same ticket.
            partial = await execution.close_position_partial(
                terminal.id, ticket, volume=str(half), idempotency_key=f"position-half-{run}"
            )
            check(f"close {half}", await final(client, partial))
            await until(
                client, terminal.id, ticket,
                lambda row: row is not None and Decimal(row.volume) == half, f"volume {half}",
            )  # fmt: skip
            print(f"position {ticket} now holds {half}")

            # 6. A trailing stop: the platform moves the stop loss up behind the best price.
            distance = on_tick(price * AWAY, spec)
            trail = await execution.set_trailing_stop(
                terminal.id, ticket, distance=distance, idempotency_key=f"position-trail-{run}"
            )
            check(f"trailing stop {distance}", await final(client, trail))
            row = await until(
                client, terminal.id, ticket,
                lambda row: row is not None and row.trailing_stop, "a trailing stop",
            )  # fmt: skip
            print(f"trailing stop {row.trailing_stop.distance}, {row.trailing_stop.state}")
            untrail = await execution.clear_trailing_stop(
                terminal.id, ticket, idempotency_key=f"position-untrail-{run}"
            )
            check("trailing stop removed", await final(client, untrail))

            # 7. Close what is left.
            closed = await execution.close_position(
                terminal.id, ticket, idempotency_key=f"position-close-{run}"
            )
            check("close", await final(client, closed))
            await until(client, terminal.id, ticket, lambda row: row is None, "the close")
            print(f"position {ticket} is closed")
        finally:
            # 8. Leave nothing behind: any position of this run still open (found by its comment,
            # so even one whose outcome was unknown) is closed, then the terminal is retired.
            try:
                rows = (await client.accounts.positions(terminal.id)).items if confirm else []
            except mfx.MfxError as exc:  # a terminal that never went live has nothing open
                print(f"could not read positions on the way out: {exc}")
                rows = []
            for left in rows:
                if left.comment == mark:
                    done = await final(
                        client,
                        await execution.close_position(
                            terminal.id,
                            left.ticket,
                            idempotency_key=f"position-cleanup-{left.ticket}-{run}",
                        ),
                    )
                    print(f"position {left.ticket} closed on the way out: {done.state}")
            await client.terminals.retire(terminal.id, idempotency_key=f"position-retire-{run}")
            if await wait_until_retired(client, terminal.id):
                print("terminal retired")
            else:
                print(f"not RETIRED after {RETIRE_WAIT_S} s: it may still hold its broker session")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default="EURUSD", help="the broker's own symbol name")
    parser.add_argument("--confirm", action="store_true", help="open the position (demo accounts)")
    args = parser.parse_args()
    asyncio.run(main(args.symbol, args.confirm))
