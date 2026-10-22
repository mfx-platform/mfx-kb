"""Change a terminal's broker password: submit the new one, and watch the terminal log in with it.

The runnable half of guides/changing-a-terminals-password.md. It does only what a customer can do:
every call is the public, signed API through the SDK.

    python samples/change_password.py <terminal id> --key-id mfxk_... --private-key-file <your key file>

The login, the server and the new password are typed at a prompt. They are never an argument, an
environment variable or a file, so they reach no shell history, no process list and no CI log.
"""

from __future__ import annotations

import argparse
import os
import asyncio
import getpass
import time
import uuid
from pathlib import Path
from typing import Any

from mfx import MfxApiError, MfxClient, Secret, SigningKey

BASE_URL = "https://api.dev.mfxhub.com/v1"
POLL_S = 5
#: A restart or a fresh login is usually under two minutes; a cold cell can take longer.
TIMEOUT_S = 20 * 60
#: How long a restart has to show itself before a live terminal is taken as already restarted.
RESTART_SEEN_S = 120
#: What shows a terminal is logged in. Ticks are not part of it: a terminal with no instruments,
#: or one whose market is closed, is logged in and quotes nothing.
LIVE = ("ProcessRunning", "BrokerConnected")

def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()



def field(answer: Any, name: str, default: Any = None) -> Any:
    """A field of the answer, whether or not this SDK version has a typed attribute for it."""
    data = answer.to_dict() if hasattr(answer, "to_dict") else dict(answer)
    return data.get(name, default)


async def change_password(
    client: MfxClient, terminal_id: str, *, login: int, server: str, password: Secret
) -> Any:
    """Submit the same login with its new password. The second submission is the rotation."""
    try:
        return await client.terminals.submit_credentials(
            terminal_id,
            login=login,
            password=password,
            server=server,
            idempotency_key=f"change-password-{uuid.uuid4()}",
        )
    except MfxApiError as refused:
        # 409 conflict: another login is another account, so it needs its own terminal.
        # 409 state_error: the terminal is draining or retired.
        raise SystemExit(f"REFUSED: {refused}") from refused


def live(terminal: Any) -> bool:
    """ACTIVE with the process up and the broker connected."""
    if str(terminal.state) != "ACTIVE":
        return False
    seen = terminal.conditions.to_dict() if terminal.conditions else {}
    return all(seen.get(name, {}).get("status") == "ok" for name in LIVE)


async def wait_until_live(client: MfxClient, terminal_id: str, *, restarting: bool) -> str:
    """Poll until the terminal is live on the new password, printing each change.

    A restart keeps the terminal `ACTIVE` while its cell stops and starts it, so the state alone
    cannot show it finished. `restarting` waits to see it stop being live first, for up to
    `RESTART_SEEN_S`; a restart quicker than the poll is taken as done.
    """
    started = time.monotonic()
    last = None
    seen_down = not restarting
    while time.monotonic() - started < TIMEOUT_S:
        terminal = await client.terminals.get(terminal_id)
        now_live = live(terminal)
        shown = (str(terminal.state), now_live)
        if shown != last:
            print(f"[{int(time.monotonic() - started):>4}s] {shown[0]} live={now_live}")
            last = shown
        if str(terminal.state) in ("FAILED", "RETIRED"):
            return str(terminal.state)
        seen_down = seen_down or not now_live or time.monotonic() - started > RESTART_SEEN_S
        if now_live and seen_down:
            return "ACTIVE"
        await asyncio.sleep(POLL_S)
    raise SystemExit(f"FAILED: not live after {TIMEOUT_S}s")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("terminal_id")
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    client = MfxClient(
        args.base_url, args.key_id, SigningKey(private_key(args))
    )
    before = await client.terminals.get(args.terminal_id)
    print(f"terminal {args.terminal_id} is {before.state}")

    login = int(input("Broker login (the one this terminal already has): "))
    server = input("Broker server: ").strip()
    password = Secret(getpass.getpass("New password: "))

    answer = await change_password(
        client, args.terminal_id, login=login, server=server, password=password
    )
    takes_effect = field(answer, "takes_effect", "first_login")
    print(f"accepted: rotated={field(answer, 'rotated', False)} takes_effect={takes_effect}")

    if takes_effect == "next_login":
        why = field(answer, "not_applied_reason")
        print("Stored. The terminal uses the new password the next time it logs in.")
        if why:
            print(f"It was not restarted now: {why}")
        return
    state = await wait_until_live(client, args.terminal_id, restarting=takes_effect == "restart")
    print("PASS" if state == "ACTIVE" else f"FAILED: the terminal is {state}; read its conditions")


if __name__ == "__main__":
    asyncio.run(main())
