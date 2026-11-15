"""A pending order that fills: the second runnable half of guides/pending-orders.md.

It finds your farm, broker and server by name, asks for a terminal, logs it in, opens the stream,
places a buy stop just above the market, and waits on the stream for the broker to fill it: an
``order.closed`` with reason ``filled`` names the position, and a ``position.opened`` names the
order it came from. It then closes that position and retires the terminal. Use a **demo** account:
the order is real on whatever account you log in to.

    MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \\
    MFX_BROKER=Pepperstone MFX_SERVER=Pepperstone-Demo MFX_LOGIN=12345678 \\
    python samples/pending_fill.py --symbol EURUSD --confirm

Without ``--confirm`` it stops after the quote and places nothing. A stop fills only when the price
reaches it: if the market does not move up to it within the wait, the sample cancels it and says
so, which is not a failure. Whatever happens, it cancels the order, closes the position it opened
and retires the terminal before it ends. The broker password is typed at a prompt: never an
argument, an environment variable or a file.

The last line it prints is its result, as JSON: ``{"outcome": "passed"}`` when the order filled and
the position closed, ``{"outcome": "amber", "notice": ...}`` when the market was closed or did not
reach the stop.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import time
import uuid
from decimal import ROUND_CEILING, Decimal
from typing import Any

import mfx
from mfx import Channel, MfxClient, NotFoundError, OrderClosed, PositionOpened, SigningKey

BASE_URL = os.environ.get("MFX_BASE_URL", "https://api.dev.mfxhub.com/v1")
RETIRE_WAIT_S = 120
RETIRE_POLL_S = 2
#: How long to wait for the stop to fill. A live market moves a few points in seconds; this is only
#: the point at which the sample stops waiting, cancels the order and says so.
FILL_WAIT_S = 90
#: A quote older than this says the market is closed: nothing would fill.
STALE_QUOTE_S = 60
#: Points above the broker's stop level: the closest a buy stop may sit, plus a little.
EXTRA_POINTS = 2


async def wait_until_retired(client: MfxClient, terminal_id: uuid.UUID | str) -> bool:
    """Wait until a terminal you retired reads RETIRED, or is gone. False if not in time."""
    deadline = time.monotonic() + RETIRE_WAIT_S
    while True:
        try:
            terminal = await client.terminals.get(terminal_id)
        except NotFoundError:
            return True
        if terminal.state == "RETIRED":
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(RETIRE_POLL_S)


def terminal_labels() -> dict[str, str]:
    """Labels for every terminal this sample asks for: MFX_TERMINAL_LABELS, a JSON object, when set."""
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


def stop_above(ask: Decimal, spec: Any) -> str:
    """The closest buy stop the broker takes, up onto the symbol's tick, written with its digits:
    the ask, plus the broker's stop level, plus a couple of points."""
    digits = int(spec.digits)
    point = Decimal(str(spec.point or Decimal(1).scaleb(-digits)))
    tick = Decimal(str(getattr(spec, "tick_size", None) or point))
    level = int(getattr(spec, "stops_level", 0) or 0)
    price = ask + point * (level + EXTRA_POINTS)
    snapped = (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    return format(snapped.quantize(Decimal(1).scaleb(-digits)), "f")


async def priced(client: MfxClient, broker_id: str, symbol: str, server_id: str) -> tuple[Any, str | None]:
    """The symbol's quote, and the account type it was asked under (see pending_orders.py)."""
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


async def filled(stream: mfx.MfxStream, terminal_id: str, ticket: int) -> tuple[int, int] | None:
    """Wait on the stream for the order to fill. The position ticket, as both events name it, and
    the order ticket the position names; None when the order closed without filling.

    Both events arrive: ``order.closed`` (reason ``filled``, with ``position_ticket``) and
    ``position.opened`` (with ``order_ticket``). Either may come first."""
    position_from_order: int | None = None
    position_opened: tuple[int, int] | None = None
    async for message in stream:
        if not isinstance(message, mfx.StreamMessage):
            continue
        event = mfx.event_of(message)
        if isinstance(event, OrderClosed) and str(event.terminal_id) == terminal_id:
            if event.ticket != ticket:
                continue
            print(f"order.closed {event.ticket}: {event.reason}, position {event.position_ticket}")
            if event.reason != "filled":
                return None
            position_from_order = event.position_ticket
        elif isinstance(event, PositionOpened) and str(event.terminal_id) == terminal_id:
            if event.order_ticket != ticket:
                continue
            print(f"position.opened {event.ticket} at {event.open_price}, from order {event.order_ticket}")
            position_opened = (event.ticket, event.order_ticket)
        if position_from_order is not None and position_opened is not None:
            if position_opened[0] != position_from_order:
                raise SystemExit(
                    f"the order names position {position_from_order}, the position {position_opened[0]}"
                )
            return position_opened
    return None


def result(outcome: str, notice: str | None = None, **detail: Any) -> None:
    """The last line: what a runner reads as this sample's outcome."""
    print(json.dumps({"outcome": outcome, "notice": notice, "detail": detail or None}))


async def main(symbol: str, confirm: bool) -> None:
    run = uuid.uuid4().hex[:8]  # one run, one set of idempotency keys: a retry repeats nothing
    mark = f"mfx-sample-{run}"  # on the order's comment, so this run finds its own order
    async with MfxClient(
        BASE_URL, os.environ["MFX_KEY_ID"], SigningKey(os.environ["MFX_PRIVATE_KEY"])
    ) as client:
        farm_id, broker_id, server_id, offers = await find_ids(
            client, os.environ["MFX_BROKER"], os.environ["MFX_SERVER"]
        )
        terminal = await client.terminals.create(
            farm_id=farm_id,
            broker_id=broker_id,
            server_id=server_id,
            streaming="quote_streaming" in offers,
            execution="order_execution" in offers,
            instruments=[symbol],
            labels=terminal_labels(),
            idempotency_key=f"fill-terminal-{run}",
        )
        try:
            await client.terminals.submit_credentials(
                terminal.id,
                login=int(os.environ["MFX_LOGIN"]),
                password=mfx.Secret(getpass.getpass("Broker password: ")),
                server=os.environ["MFX_SERVER"],
                idempotency_key=f"fill-credential-{run}",
            )
            while not mfx.is_usable(terminal := await client.terminals.get(terminal.id)):
                if terminal.state in ("FAILED", "RETIRED"):
                    raise SystemExit(f"the terminal ended {terminal.state}: read its conditions")
                print(terminal.state)
                await asyncio.sleep(5)
            print(f"terminal {terminal.id} is ACTIVE")

            quote, account_type = await priced(client, broker_id, symbol, server_id)
            spec = await client.catalog.symbol(broker_id, symbol, server_id=server_id,
                                               account_type=account_type)  # fmt: skip
            print(f"{symbol} bid {quote.bid} ask {quote.ask}, {quote.age_s:.1f} s old")
            if not confirm:
                print("not placing an order: pass --confirm to place a stop and wait for its fill")
                return
            if quote.age_s > STALE_QUOTE_S:
                result("amber", f"the last price is {quote.age_s:.0f} s old: the market looks closed")
                return

            # Open the stream first, so the fill cannot happen before anyone is listening.
            async with client.stream() as stream:
                await stream.subscribe(Channel.EVENTS)

                # A buy stop just above the ask: it fills as soon as the price rises to it.
                price = stop_above(Decimal(quote.ask), spec)
                placed = await client.execution.place_order(
                    terminal.id,
                    symbol=symbol,
                    side="buy",
                    order_type="stop",
                    price=price,
                    volume=str(spec.volume_min),
                    comment=mark,
                    idempotency_key=f"fill-place-{run}",
                )
                if placed.command_id:
                    placed = await client.execution.wait_for(placed.command_id)
                ticket = placed.tickets.order
                if str(placed.state) != "SUCCEEDED" or ticket is None:
                    raise SystemExit(f"the order was not placed: {placed.reason_text or placed.detail}")
                print(f"buy stop {ticket} placed at {price}")

                try:
                    tickets = await asyncio.wait_for(
                        filled(stream, str(terminal.id), ticket), timeout=FILL_WAIT_S
                    )
                except TimeoutError:
                    tickets = None
            if tickets is None:
                result("amber", f"the stop did not fill within {FILL_WAIT_S} s", order=ticket)
                return  # the way out cancels it

            position_ticket, _ = tickets
            closed = await client.execution.close_position(
                terminal.id, position_ticket, idempotency_key=f"fill-close-{run}"
            )
            if closed.command_id:
                closed = await client.execution.wait_for(closed.command_id)
            print(f"close {closed.state}")
            if str(closed.state) != "SUCCEEDED":
                raise SystemExit(f"the position was not closed: {closed.reason_text or closed.detail}")
            result("passed", order=ticket, position=position_ticket)
        finally:
            # Leave nothing behind: this run's order still pending, or its position still open
            # (each found by the comment), then the terminal.
            if confirm:
                await tidy(client, str(terminal.id), mark, run)
            await client.terminals.retire(terminal.id, idempotency_key=f"fill-retire-{run}")
            if await wait_until_retired(client, terminal.id):
                print("terminal retired")
            else:
                print(f"not RETIRED after {RETIRE_WAIT_S} s: it may still hold its broker session")


async def tidy(client: MfxClient, terminal_id: str, mark: str, run: str) -> None:
    """Cancel this run's pending order and close its position, if either is still there."""
    try:
        orders = (await client.accounts.pending_orders(terminal_id)).items
        positions = (await client.accounts.positions(terminal_id)).items
    except mfx.MfxError as exc:  # a terminal that never went live has nothing to read
        print(f"could not read the account on the way out: {exc}")
        return
    for order in (o for o in orders if o.comment == mark):
        gone = await client.execution.cancel_order(
            terminal_id, order.ticket, idempotency_key=f"fill-cleanup-order-{order.ticket}-{run}"
        )
        if gone.command_id:
            gone = await client.execution.wait_for(gone.command_id)
        print(f"order {order.ticket} cancelled on the way out: {gone.state}")
    for position in (p for p in positions if p.comment == mark):
        gone = await client.execution.close_position(
            terminal_id, position.ticket,
            idempotency_key=f"fill-cleanup-position-{position.ticket}-{run}",
        )  # fmt: skip
        if gone.command_id:
            gone = await client.execution.wait_for(gone.command_id)
        print(f"position {position.ticket} closed on the way out: {gone.state}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default="EURUSD", help="the broker's own symbol name")
    parser.add_argument("--confirm", action="store_true", help="place the order (demo accounts)")
    args = parser.parse_args()
    asyncio.run(main(args.symbol, args.confirm))
