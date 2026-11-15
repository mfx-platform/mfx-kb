"""Your first terminal: find your ids, request it, give it a credential, wait for ACTIVE, retire it.

The runnable half of guides/your-first-terminal.md.

    MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \\
    MFX_BROKER=Pepperstone MFX_SERVER=Pepperstone-Demo MFX_LOGIN=12345678 \\
    python samples/first_terminal.py

MFX_BROKER is your broker's name and MFX_SERVER the server name your broker gave you, exactly as it
appears in your trading platform's login window; the sample looks up their ids. MFX_FARM_ID is
optional: without it, the first farm your plan allows is used. MFX_SAME_LOGIN_TERMINAL_ID is
optional too: a terminal of yours that already uses MFX_LOGIN. With it, the sample first reads how
many sessions the server allows on one login, and stops, skipped, when yours already fill them.

The broker password is typed at a prompt. It is never an argument, an environment variable or a
file, so it reaches no shell history, no process list and no CI log.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import time
import uuid

import mfx
from mfx import MfxClient, NotFoundError, SigningKey

BASE_URL = os.environ.get("MFX_BASE_URL", "https://api.dev.mfxhub.com/v1")
SETTLED = {"ACTIVE", "FAILED", "RETIRED"}
#: Where a server's sessions per login are final. `probed` is only what the server was seen to
#: hold, a lower bound: one more may well fit, so the sample still asks.
KNOWN_CAP = ("limit", "operator")
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


async def no_room(client: MfxClient, server_id: str) -> str | None:
    """Why one more session on the login cannot fit, when the server's limit says so; else None.

    Only with MFX_SAME_LOGIN_TERMINAL_ID: without a terminal on the login, this is its first
    session. GET /v1/usage lists every enabled server's limit under `per_login.servers`."""
    other = os.environ.get("MFX_SAME_LOGIN_TERMINAL_ID")
    if not other:
        return None
    in_use = (await client.terminals.get(other)).to_dict().get("same_login_terminals") or 1
    try:
        usage = (await client.usage.get()).to_dict()
    except mfx.MfxApiError as refused:
        print(f"the server's limit could not be read ({refused}); asking anyway")
        return None
    for row in usage.get("per_login", {}).get("servers", []):
        if str(row["server"]["id"]) == server_id and row.get("max_sessions_source") in KNOWN_CAP:
            if in_use + 1 > int(row["max_sessions"]):
                return (f"not requested: this server allows {row['max_sessions']} sessions on one "
                        f"login and yours use {in_use}")  # fmt: skip
    return None


async def main() -> None:
    run = uuid.uuid4().hex[:8]  # one run, one set of idempotency keys: a retry repeats nothing
    async with MfxClient(
        BASE_URL, os.environ["MFX_KEY_ID"], SigningKey(os.environ["MFX_PRIVATE_KEY"])
    ) as client:
        farm_id, broker_id, server_id, offers = await find_ids(
            client, os.environ["MFX_BROKER"], os.environ["MFX_SERVER"]
        )
        # Said as skipped, not failed: nothing is wrong, the login has no session to spare.
        notice = await no_room(client, server_id)
        if notice is not None:
            print(notice)
            print(json.dumps({"outcome": "skipped", "notice": notice, "detail": None}))
            return

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
            labels=terminal_labels(),
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
        if await wait_until_retired(client, terminal.id):
            print("retired")
        else:
            print(f"not RETIRED after {RETIRE_WAIT_S} s: it may still hold its broker session")
        if terminal.state != "ACTIVE":
            raise SystemExit(f"the terminal ended {terminal.state}, not ACTIVE")


if __name__ == "__main__":
    asyncio.run(main())
