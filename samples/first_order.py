"""Your first quote and your first order, from nothing: the runnable half of guides/your-first-order.md.

It finds your farm, broker and server by name, asks for a terminal, logs it in, reads a quote,
buys the smallest volume the symbol allows, closes that position, and retires the terminal. Use a
**demo** account: the order is real on whatever account you log in to.

    MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \\
    MFX_BROKER=Pepperstone MFX_SERVER=Pepperstone-Demo MFX_LOGIN=12345678 \\
    python samples/first_order.py --symbol EURUSD --confirm

Without ``--confirm`` it stops after the quote and places nothing. The broker password is typed at
a prompt: never an argument, an environment variable or a file, so it reaches no shell history, no
process list and no CI log.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import time
import uuid
from typing import Any

import mfx
from mfx import MfxClient, NotFoundError, SigningKey

BASE_URL = os.environ.get("MFX_BASE_URL", "https://api.dev.mfxhub.com/v1")
#: How long to wait for a terminal you retired to read RETIRED. A drain takes seconds; this is
#: only the point at which the sample stops waiting and says so.
RETIRE_WAIT_S = 120
RETIRE_POLL_S = 2


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
    # A farm is where your terminals run; your plan says which you may use. Take one that can
    # execute orders, or the one named in MFX_FARM_ID.
    wanted = os.environ.get("MFX_FARM_ID")
    farm = None
    async for candidate in client.farms.list():
        offers = {str(usage) for usage in candidate.usages}
        if str(candidate.id) == wanted or (not wanted and "order_execution" in offers):
            farm = candidate
            break
    if farm is None:
        raise SystemExit("no farm on your plan executes orders; ask support which one to use")

    # The broker by its name (code or display name), then its server by the name your broker
    # gave you, exactly as it appears in your trading platform's login window.
    broker_row = None
    async for row in client.catalog.all_brokers():
        if broker.lower() in {row.code.lower(), row.display_name.lower()}:
            broker_row = row
            break
    if broker_row is None:
        raise SystemExit(f"no broker called {broker!r}; client.catalog.all_brokers() lists them")
    servers = await client.catalog.servers(broker_row.id)  # one page; iterate it or read .items
    server_row = next((s for s in servers if s.server_name.lower() == server.lower()), None)
    if server_row is None or not server_row.enabled:
        names = ", ".join(s.server_name for s in servers if s.enabled)
        raise SystemExit(f"server {server!r} is not available; {broker_row.display_name} offers {names}")
    print(f"farm {farm.name}, broker {broker_row.display_name}, server {server_row.server_name}")
    return str(farm.id), str(broker_row.id), str(server_row.id), offers



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
    async with MfxClient(
        BASE_URL, os.environ["MFX_KEY_ID"], SigningKey(os.environ["MFX_PRIVATE_KEY"])
    ) as client:
        farm_id, broker_id, server_id, offers = await find_ids(
            client, os.environ["MFX_BROKER"], os.environ["MFX_SERVER"]
        )

        # 1. A terminal for your account, asking only for what the farm offers.
        terminal = await client.terminals.create(
            farm_id=farm_id,
            broker_id=broker_id,
            server_id=server_id,
            streaming="quote_streaming" in offers,
            execution="order_execution" in offers,
            instruments=[symbol],  # the broker's own symbol names
            labels=terminal_labels(),
            idempotency_key=f"first-order-terminal-{run}",
        )
        try:
            await client.terminals.submit_credentials(
                terminal.id,
                login=int(os.environ["MFX_LOGIN"]),
                password=mfx.Secret(getpass.getpass("Broker password: ")),
                server=os.environ["MFX_SERVER"],
                idempotency_key=f"first-order-credential-{run}",
            )
            # Usable means ACTIVE. On the way, a health check can read "failed" before the first
            # live tick arrives; that is not an error.
            while not mfx.is_usable(terminal := await client.terminals.get(terminal.id)):
                if terminal.state in ("FAILED", "RETIRED"):
                    raise SystemExit(f"the terminal ended {terminal.state}: read its conditions")
                print(terminal.state)
                await asyncio.sleep(5)
            print(f"terminal {terminal.id} is ACTIVE")

            # 2. A quote, always with its age. broker_time is the broker's clock, not UTC.
            quote, account_type = await priced(client, broker_id, symbol, server_id)
            print(f"{symbol} bid {quote.bid} ask {quote.ask}, {quote.age_s:.1f} s old")
            if not confirm:
                print("not placing an order: pass --confirm to buy the smallest volume and close it")
                return

            # 3. The smallest volume the symbol allows, as a decimal string.
            spec = await client.catalog.symbol(broker_id, symbol, server_id=server_id,
                                   account_type=account_type)
            volume = str(spec.volume_min)
            placed = await client.execution.place_order(
                terminal.id,
                symbol=symbol,
                side="buy",
                volume=volume,
                idempotency_key=f"first-order-buy-{run}",
            )
            # Execution never raises: every outcome is a result. Wait until it is final.
            done = await client.execution.wait_for(placed.command_id) if placed.command_id else placed
            print(f"order {done.state}: {done.exec_volume} at {done.exec_price} ({done.comment_raw})")
            if str(done.state) != "SUCCEEDED" or done.tickets.position is None:
                raise SystemExit(f"the order did not fill: {done.reason_text or done.detail}")

            # 4. Close the position the order opened, by its ticket.
            closed = await client.execution.close_position(
                terminal.id, done.tickets.position, idempotency_key=f"first-order-close-{run}"
            )
            closed = await client.execution.wait_for(closed.command_id) if closed.command_id else closed
            print(f"close {closed.state} at {closed.exec_price}")
        finally:
            # 5. Retire the terminal, whatever happened above.
            await client.terminals.retire(terminal.id, idempotency_key=f"first-order-retire-{run}")
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
