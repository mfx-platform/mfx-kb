"""Your first terminal: find your ids, request it, give it a credential, wait for ACTIVE, retire it.

The runnable half of guides/your-first-terminal.md.

    MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \\
    MFX_BROKER=Pepperstone MFX_SERVER=Pepperstone-Demo MFX_LOGIN=12345678 \\
    python samples/first_terminal.py

MFX_BROKER is your broker's name and MFX_SERVER the server name your broker gave you, exactly as it
appears in your trading platform's login window; the sample looks up their ids. MFX_FARM_ID is
optional: without it, the first farm your plan allows is used.

The broker password is typed at a prompt. It is never an argument, an environment variable or a
file, so it reaches no shell history, no process list and no CI log.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import uuid

import mfx
from mfx import MfxClient, SigningKey

BASE_URL = os.environ.get("MFX_BASE_URL", "https://api.dev.mfxhub.com/v1")
SETTLED = {"ACTIVE", "FAILED", "RETIRED"}


async def find_ids(client: MfxClient, broker: str, server: str) -> tuple[str, str, str, set[str]]:
    """The farm, broker and server ids for the names you know, and what the farm offers."""
    # A farm is where your terminals run; your plan says which you may use.
    wanted = os.environ.get("MFX_FARM_ID")
    farm = None
    async for candidate in client.farms.list():
        if not wanted or str(candidate.id) == wanted:
            farm = candidate
            break
    if farm is None:
        raise SystemExit("no farm found for your plan; ask support which one to use")
    offers = {str(usage) for usage in farm.usages}

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
    print(f"farm {farm.name} offers {sorted(offers)}")
    return str(farm.id), str(broker_row.id), str(server_row.id), offers


async def main() -> None:
    run = uuid.uuid4().hex[:8]  # one run, one set of idempotency keys: a retry repeats nothing
    async with MfxClient(
        BASE_URL, os.environ["MFX_KEY_ID"], SigningKey(os.environ["MFX_PRIVATE_KEY"])
    ) as client:
        farm_id, broker_id, server_id, offers = await find_ids(
            client, os.environ["MFX_BROKER"], os.environ["MFX_SERVER"]
        )

        # Ask only for what the farm offers: a farm that does not stream quotes refuses a
        # streaming terminal, and one that does not execute refuses an executing one.
        # The same key with the same body returns the same terminal, never a second one.
        terminal = await client.terminals.create(
            farm_id=farm_id,
            broker_id=broker_id,
            server_id=server_id,
            streaming="quote_streaming" in offers,
            execution="order_execution" in offers,
            instruments=["EURUSD"],  # the broker's own symbol names
            idempotency_key=f"first-terminal-{run}",
        )
        print(terminal.id, terminal.state)  # REQUESTED

        await client.terminals.submit_credentials(
            terminal.id,
            login=int(os.environ["MFX_LOGIN"]),
            password=mfx.Secret(getpass.getpass("Broker password: ")),
            server=os.environ["MFX_SERVER"],
            idempotency_key=f"first-credential-{run}",
        )

        # On the way to ACTIVE a health check can read "failed" before the first live tick
        # arrives; that is not an error. conditions say why if it stalls.
        while terminal.state not in SETTLED:
            await asyncio.sleep(5)
            terminal = await client.terminals.get(terminal.id)
            seen = terminal.conditions.to_dict() if terminal.conditions else {}
            print(terminal.state, {name: c.get("status", "?") for name, c in seen.items()})

        await client.terminals.retire(terminal.id, idempotency_key=f"first-retire-{run}")
        print("retired")
        if terminal.state != "ACTIVE":
            raise SystemExit(f"the terminal ended {terminal.state}, not ACTIVE")


if __name__ == "__main__":
    asyncio.run(main())
