"""A terminal's whole life, twice: retire what you have, then ask for it again and watch it go live.

The runnable half of guides/a-terminals-whole-life.md. It does only what a customer can do — every call is the public, signed API through
the SDK — in three phases you can run together or one at a time:

    retire    the terminals you name with --terminal are retired - never any other; the script
              waits until each is RETIRED and writes what it was (farm, broker, server, instruments — no secret) to a plan file
    request   every terminal in the plan is asked for again, given its credential, and watched
              until it is ACTIVE with fresh ticks
    all       retire, pause until you press Enter, then request

    python samples/terminal_lifecycle.py all --terminal <terminal id> \\
        --key-id mfxk_... --private-key-file C:\\secure\\mfx-dev.key --instruments EURUSD,XAUUSD

Broker logins and passwords are typed at a prompt. They are never an argument, an environment
variable or a file, so they reach no shell history, no process list and no CI log — and the plan
file holds nothing you would mind losing.
"""

from __future__ import annotations

import argparse
import os
import asyncio
import getpass
import json
import time
import uuid
from pathlib import Path
from typing import Any

from mfx import MfxApiError, MfxClient, Secret, SigningKey

BASE_URL = "https://api.dev.mfxhub.com/v1"
PLAN = Path("terminal-lifecycle-plan.json")
POLL_S = 5
#: Generous on purpose: a cold cell image pull and a first broker login are minutes, not seconds.
RETIRE_TIMEOUT_S = 15 * 60
ACTIVE_TIMEOUT_S = 20 * 60
LIVE = ("ProcessRunning", "BrokerConnected", "TicksFresh")

def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()



def shown(terminal: Any) -> dict[str, Any]:
    """The terminal as plain data, so optional fields read the same whether or not they were sent."""
    data: dict[str, Any] = terminal.to_dict()
    return data


def conditions(terminal: Any) -> dict[str, str]:
    return {name: c.get("status", "?") for name, c in shown(terminal).get("conditions", {}).items()}


def line(started: float, terminal: Any, note: str = "") -> None:
    data = shown(terminal)
    dots = " ".join(f"{name}={status}" for name, status in sorted(conditions(terminal).items()))
    elapsed = int(time.monotonic() - started)
    print(f"[{elapsed:>4}s] {data['broker']['label']:<14} {data['state']:<13} {dots} {note}".rstrip())


async def watch(client: MfxClient, ids: list[str], *, until: Any, timeout_s: int) -> list[Any]:
    """Poll until every terminal satisfies `until`, printing each change of state or condition."""
    started = time.monotonic()
    last: dict[str, Any] = {}
    done: dict[str, Any] = {}
    while len(done) < len(ids):
        if time.monotonic() - started > timeout_s:
            waiting = [i for i in ids if i not in done]
            raise SystemExit(f"FAILED: still waiting after {timeout_s}s for {waiting}")
        for terminal_id in ids:
            if terminal_id in done:
                continue
            terminal = await client.terminals.get(terminal_id)
            seen = (shown(terminal)["state"], tuple(sorted(conditions(terminal).items())))
            if last.get(terminal_id) != seen:
                last[terminal_id] = seen
                line(started, terminal)
            if until(terminal):
                done[terminal_id] = terminal
        await asyncio.sleep(POLL_S)
    return [done[i] for i in ids]


async def retire(client: MfxClient, names: list[str]) -> None:
    # Only the terminals named on the command line, and only those that are ACTIVE: a sample that
    # retired everything a tenant had would take its live terminals down with it.
    named = [await client.terminals.get(name) for name in names]
    active = [t for t in named if shown(t)["state"] == "ACTIVE"]
    if not active:
        raise SystemExit("None of the terminals named is ACTIVE, so there is nothing to retire.")
    plan = [
        {
            "was": str(t.id),
            "farm_id": shown(t)["farm"]["id"],
            "broker_id": shown(t)["broker"]["id"],
            "broker": shown(t)["broker"]["label"],
            "server_id": shown(t)["server"]["id"],
            "server": shown(t)["server"]["label"],
            "streaming": shown(t)["streaming"],
            "execution": shown(t)["execution"],
            "instruments": shown(t)["instruments"],
        }
        for t in active
    ]
    PLAN.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(f"Retiring {len(active)} terminal(s); what they were is in {PLAN}")

    for terminal in active:
        # DELETE is an intent: the answer is DRAINING, and only the cell's word makes it RETIRED.
        answer = await client.terminals.retire(terminal.id, idempotency_key=f"retire-{uuid.uuid4()}")
        print(f"  {shown(answer)['broker']['label']}: {shown(answer)['state']}")
    await watch(
        client,
        [str(t.id) for t in active],
        until=lambda t: shown(t)["state"] == "RETIRED",
        timeout_s=RETIRE_TIMEOUT_S,
    )
    print("PASS: every terminal drained on its cell and is RETIRED.")


async def request(client: MfxClient, instruments: list[str]) -> None:
    if not PLAN.exists():
        raise SystemExit(f"{PLAN} is missing: run `retire` first, or write it by hand.")
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    run = uuid.uuid4().hex[:8]
    ids: list[str] = []
    for index, entry in enumerate(plan):
        wanted = entry["instruments"] or instruments
        # The key is fixed for this run: if the call is retried, the same terminal comes back.
        terminal = await client.terminals.create(
            farm_id=entry["farm_id"],
            broker_id=entry["broker_id"],
            server_id=entry["server_id"],
            streaming=entry["streaming"],
            execution=entry["execution"],
            instruments=wanted,
            labels={"proof": run},
            idempotency_key=f"lifecycle-{run}-{index}",
        )
        print(f"{entry['broker']}: {terminal.id} {shown(terminal)['state']} watching {wanted}")

        login = int(input(f"  {entry['broker']} login number: ").strip())
        server = input(f"  {entry['broker']} server [{entry['server']}]: ").strip()
        await client.terminals.submit_credentials(
            terminal.id,
            login=login,
            password=Secret(getpass.getpass(f"  {entry['broker']} password: ")),
            server=server or entry["server"],
            idempotency_key=f"lifecycle-{run}-{index}-credential",
        )
        ids.append(str(terminal.id))

    def live(terminal: Any) -> bool:
        if shown(terminal)["state"] == "FAILED":
            raise SystemExit(f"FAILED: {shown(terminal)['broker']['label']} went FAILED")
        ok = conditions(terminal)
        return shown(terminal)["state"] == "ACTIVE" and all(ok.get(name) == "ok" for name in LIVE)

    await watch(client, ids, until=live, timeout_s=ACTIVE_TIMEOUT_S)
    print("PASS: every terminal is ACTIVE, connected to its broker, with fresh ticks.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("phase", choices=["retire", "request", "all"])
    parser.add_argument(
        "--terminal", action="append", default=[], help="a terminal to retire; give it once per terminal"
    )
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument(
        "--instruments",
        default="",
        help="comma-separated, in the broker's own names; used for a terminal that had none",
    )
    args = parser.parse_args()
    key = SigningKey(private_key(args))
    instruments = [name.strip() for name in args.instruments.split(",") if name.strip()]

    async with MfxClient(args.base_url, args.key_id, key) as client:
        try:
            if args.phase in ("retire", "all"):
                if not args.terminal:
                    raise SystemExit("name the terminals to retire: --terminal <id>, once per terminal")
                await retire(client, args.terminal)
            if args.phase == "all":
                input("Terminals are retired. Press Enter when the cell is ready for new ones... ")
            if args.phase in ("request", "all"):
                await request(client, instruments)
        except MfxApiError as refused:
            # The platform's own words: what was refused, why, and the request id to quote.
            raise SystemExit(f"REFUSED: {refused}") from None


if __name__ == "__main__":
    asyncio.run(main())
