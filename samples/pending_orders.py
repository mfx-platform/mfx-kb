"""A pending order from start to end: the runnable half of guides/pending-orders.md.

It finds your farm, broker and server by name, asks for a terminal, logs it in, places a buy limit
well below the market (so it does not fill), sees it in your pending orders, moves its price,
cancels it, sees it gone, and retires the terminal. Use a **demo** account: the order is real on
whatever account you log in to.

    MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \\
    MFX_BROKER=Pepperstone MFX_SERVER=Pepperstone-Demo MFX_LOGIN=12345678 \\
    python samples/pending_orders.py --symbol EURUSD --confirm

Without ``--confirm`` it stops after the quote and places nothing. Whatever happens, it cancels the
order it placed and retires the terminal before it ends, so a run leaves nothing behind. The broker
password is typed at a prompt: never an argument, an environment variable or a file.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import time
import uuid
from decimal import ROUND_FLOOR, Decimal

from typing import Any

import mfx
from mfx import MfxClient, NotFoundError, SigningKey

BASE_URL = os.environ.get("MFX_BASE_URL", "https://api.dev.mfxhub.com/v1")
#: How long to wait for a terminal you retired to read RETIRED. A drain takes seconds; this is
#: only the point at which the sample stops waiting and says so.
RETIRE_WAIT_S = 120
RETIRE_POLL_S = 2
#: How far below the bid the limit sits: far enough that it does not fill while the sample runs.
BELOW = Decimal("0.05")
#: How long to wait for the pending-order list to show a change (it is a snapshot of the account).
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


async def pending_tickets(
    client: MfxClient, terminal_id: uuid.UUID | str, comment: str | None = None
) -> set[int]:
    """The tickets of the account's pending orders, from its latest snapshot; only this run's when
    ``comment`` is given."""
    orders = (await client.accounts.pending_orders(terminal_id)).items
    return {o.ticket for o in orders if comment is None or o.comment == comment}


async def until(client: MfxClient, terminal_id: uuid.UUID | str, ticket: int, *, present: bool) -> None:
    """Wait for the order to appear in (or leave) the pending list: it is read from a snapshot."""
    for _ in range(LIST_WAIT_S):
        if (ticket in await pending_tickets(client, terminal_id)) is present:
            return
        await asyncio.sleep(1)
    where = "in" if present else "gone from"
    raise SystemExit(f"order {ticket} is not {where} the pending list after {LIST_WAIT_S} s")



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
    mark = f"mfx-sample-{run}"  # on the order's comment, so this run finds its own orders
    async with MfxClient(
        BASE_URL, os.environ["MFX_KEY_ID"], SigningKey(os.environ["MFX_PRIVATE_KEY"])
    ) as client:
        farm_id, broker_id, server_id, offers = await find_ids(
            client, os.environ["MFX_BROKER"], os.environ["MFX_SERVER"]
        )

        # 1. A terminal for your account, as in your first order.
        terminal = await client.terminals.create(
            farm_id=farm_id,
            broker_id=broker_id,
            server_id=server_id,
            streaming="quote_streaming" in offers,
            execution="order_execution" in offers,
            instruments=[symbol],
            labels=terminal_labels(),
            idempotency_key=f"pending-terminal-{run}",
        )
        try:
            await client.terminals.submit_credentials(
                terminal.id,
                login=int(os.environ["MFX_LOGIN"]),
                password=mfx.Secret(getpass.getpass("Broker password: ")),
                server=os.environ["MFX_SERVER"],
                idempotency_key=f"pending-credential-{run}",
            )
            while not mfx.is_usable(terminal := await client.terminals.get(terminal.id)):
                if terminal.state in ("FAILED", "RETIRED"):
                    raise SystemExit(f"the terminal ended {terminal.state}: read its conditions")
                print(terminal.state)
                await asyncio.sleep(5)
            print(f"terminal {terminal.id} is ACTIVE")

            # 2. A quote, and the symbol's rules: the smallest volume and its price steps.
            quote, account_type = await priced(client, broker_id, symbol, server_id)
            spec = await client.catalog.symbol(broker_id, symbol, server_id=server_id,
                                   account_type=account_type)
            print(f"{symbol} bid {quote.bid} ask {quote.ask}, {quote.age_s:.1f} s old")
            if not confirm:
                print("not placing an order: pass --confirm to place, move and cancel a limit")
                return

            # 3. A buy limit 5 % below the bid: it waits at the broker and does not fill.
            price = on_tick(Decimal(quote.bid) * (1 - BELOW), spec)
            placed = await client.execution.place_order(
                terminal.id,
                symbol=symbol,
                side="buy",
                order_type="limit",
                price=price,
                volume=str(spec.volume_min),
                comment=mark,
                idempotency_key=f"pending-place-{run}",
            )
            placed = await client.execution.wait_for(placed.command_id) if placed.command_id else placed
            ticket = placed.tickets.order
            if str(placed.state) != "SUCCEEDED" or ticket is None:
                raise SystemExit(f"the order was not placed: {placed.reason_text or placed.detail}")
            print(f"buy limit {ticket} placed at {price}")
            await until(client, terminal.id, ticket, present=True)
            print(f"order {ticket} is in your pending orders")

            # 4. Move its price a further 1 % down. Its ticket stays the same.
            moved_to = on_tick(Decimal(quote.bid) * (1 - BELOW - Decimal("0.01")), spec)
            moved = await client.execution.modify_order(
                terminal.id, ticket, price=moved_to, idempotency_key=f"pending-modify-{run}"
            )
            moved = await client.execution.wait_for(moved.command_id) if moved.command_id else moved
            print(f"modify {moved.state}: now at {moved_to}")
            if str(moved.state) != "SUCCEEDED":
                raise SystemExit(f"the order was not moved: {moved.reason_text or moved.detail}")

            # 5. Cancel it, and see it leave the list.
            cancelled = await client.execution.cancel_order(
                terminal.id, ticket, idempotency_key=f"pending-cancel-{run}"
            )
            cancelled = (
                await client.execution.wait_for(cancelled.command_id)
                if cancelled.command_id
                else cancelled
            )
            print(f"cancel {cancelled.state}")
            if str(cancelled.state) != "SUCCEEDED":
                raise SystemExit(f"the order was not cancelled: {cancelled.reason_text}")
            await until(client, terminal.id, ticket, present=False)
            print(f"order {ticket} is gone")
        finally:
            # 6. Leave nothing behind: any order of this run still pending (found by its comment,
            # so even one whose outcome was unknown) is cancelled, then the terminal is retired.
            try:
                left_over = await pending_tickets(client, terminal.id, mark) if confirm else set()
            except mfx.MfxError as exc:  # a terminal that never went live has no orders to read
                print(f"could not read pending orders on the way out: {exc}")
                left_over = set()
            if left_over:
                for left in sorted(left_over):
                    gone = await client.execution.cancel_order(
                        terminal.id, left, idempotency_key=f"pending-cleanup-{left}-{run}"
                    )
                    if gone.command_id:
                        gone = await client.execution.wait_for(gone.command_id)
                    print(f"order {left} cancelled on the way out: {gone.state}")
            await client.terminals.retire(terminal.id, idempotency_key=f"pending-retire-{run}")
            if await wait_until_retired(client, terminal.id):
                print("terminal retired")
            else:
                print(f"not RETIRED after {RETIRE_WAIT_S} s: it may still hold its broker session")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default="EURUSD", help="the broker's own symbol name")
    parser.add_argument("--confirm", action="store_true", help="place the order (demo accounts)")
    args = parser.parse_args()
    asyncio.run(main(args.symbol, args.confirm))
