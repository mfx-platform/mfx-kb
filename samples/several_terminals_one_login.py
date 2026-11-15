"""One more terminal on a broker login you already use, and how many of yours now share it.

The runnable half of guides/several-terminals-on-one-login.md. It does only what a customer can do:
every call is the public, signed API through the SDK.

    python samples/several_terminals_one_login.py <terminal id> --key-id mfxk_... --private-key-file <your key file>

`<terminal id>` is a terminal of yours that already uses the login. The new terminal is requested on
the same farm, broker and server. The login, the server and the password are typed at a prompt.
They are never an argument, an environment variable or a file, so they reach no shell history, no
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
from pathlib import Path
from typing import Any

from mfx import MfxApiError, MfxClient, NotFoundError, Secret, SigningKey

BASE_URL = "https://api.dev.mfxhub.com/v1"
POLL_S = 5
#: A first login is usually under two minutes; a cold cell can take longer.
TIMEOUT_S = 20 * 60
#: What shows a terminal is logged in. Ticks are not part of it: a terminal with no instruments,
#: or one whose market is closed, is logged in and quotes nothing.
LIVE = ("ProcessRunning", "BrokerConnected")
#: The two limits on terminals per login, as `details.reason` names them.
PLAN_LIMIT = "login_terminal_limit"
BROKER_LIMIT = "login_session_limit"
#: Where a server's sessions per login are final. `probed` is only what the server was seen to
#: hold, a lower bound: one more may well fit, so the sample still asks.
KNOWN_CAP = ("limit", "operator")
#: How long to wait for a terminal you retired to read RETIRED. A drain takes seconds; this is
#: only the point at which the sample stops waiting and says so.
RETIRE_WAIT_S = 120
RETIRE_POLL_S = 2
# DECISION: no settle time is waited here, after a terminal is retired or before one more is added.
# RETIRED is the platform's word that the terminal's broker session is gone, and the platform counts
# the sessions on a login; a pause guessed in a sample would hide a session the platform still
# counts wrong, and teach every reader a number nobody measured.


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


def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()


def terminal_labels() -> dict[str, str]:
    """Labels for every terminal this sample asks for: MFX_TERMINAL_LABELS, a JSON object, when set.

    Optional. Labels are yours to sort and find terminals by; nothing here needs one.
    """
    labels = json.loads(os.environ.get("MFX_TERMINAL_LABELS") or "{}")
    return {str(key): str(value) for key, value in labels.items()}


def field(answer: Any, name: str, default: Any = None) -> Any:
    """A field of the answer, whether or not this SDK version has a typed attribute for it."""
    data = answer.to_dict() if hasattr(answer, "to_dict") else dict(answer)
    return data.get(name, default)


def explain(refused: MfxApiError) -> str:
    """Which limit refused one more terminal on the login, in a sentence."""
    reason = refused.details.get("reason")
    if reason == PLAN_LIMIT:
        quota, in_use = refused.details.get("quota"), refused.details.get("in_use")
        return f"your plan allows {quota} terminals on one login and you have {in_use}"
    if reason == BROKER_LIMIT and refused.details.get("retry_after_s"):
        wait = refused.details["retry_after_s"]
        return f"the broker's limit on this server is being measured; try again in {wait} s"
    if reason == BROKER_LIMIT:
        return "the broker's limit of sessions on this login is reached"
    return str(refused)


async def known_cap(client: MfxClient, server_id: str) -> int | None:
    """The sessions one login may hold on this server, when that number is final; else None.

    GET /v1/usage lists every enabled server under `per_login.servers`."""
    try:
        usage = (await client.usage.get()).to_dict()
    except MfxApiError as refused:
        print(f"the server's limit could not be read ({refused}); asking anyway")
        return None
    for row in usage.get("per_login", {}).get("servers", []):
        if str(row["server"]["id"]) == str(server_id) and row.get("max_sessions_source") in KNOWN_CAP:
            return int(row["max_sessions"])
    return None


def skip(notice: str) -> None:
    """Say the sample was skipped, not failed: nothing is wrong, there is no room to show it."""
    print(notice)
    print(json.dumps({"outcome": "skipped", "notice": notice, "detail": None}))


async def add_terminal(client: MfxClient, like: Any) -> Any:
    """Request one more terminal on the same farm, broker and server as `like`."""
    return await client.terminals.create(
        farm_id=field(like, "farm")["id"],
        broker_id=field(like, "broker")["id"],
        server_id=field(like, "server")["id"],
        streaming=bool(field(like, "streaming", False)),
        labels=terminal_labels(),
        idempotency_key=f"same-login-{uuid.uuid4()}",
    )


async def submit_login(
    client: MfxClient, terminal_id: str, *, login: int, server: str, password: Secret
) -> Any:
    """Give the new terminal the same login. Each terminal holds its own copy of the credential."""
    return await client.terminals.submit_credentials(
        terminal_id,
        login=login,
        password=password,
        server=server,
        idempotency_key=f"same-login-credential-{uuid.uuid4()}",
    )


def live(terminal: Any) -> bool:
    """ACTIVE with the process up and the broker connected."""
    if str(terminal.state) != "ACTIVE":
        return False
    seen = terminal.conditions.to_dict() if terminal.conditions else {}
    return all(seen.get(name, {}).get("status") == "ok" for name in LIVE)


async def wait_until_live(client: MfxClient, terminal_id: str) -> str:
    """Poll until the terminal is logged in, failed or stopped, printing each change."""
    started = time.monotonic()
    last = None
    while time.monotonic() - started < TIMEOUT_S:
        terminal = await client.terminals.get(terminal_id)
        shown = (str(terminal.state), live(terminal))
        if shown != last:
            print(f"[{int(time.monotonic() - started):>4}s] {shown[0]} live={shown[1]}")
            last = shown
        if shown[0] in ("FAILED", "DRAINING", "RETIRED"):
            return shown[0]
        if shown[1]:
            return "ACTIVE"
        await asyncio.sleep(POLL_S)
    raise SystemExit(f"FAILED: not live after {TIMEOUT_S}s")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("terminal_id", help="a terminal of yours that already uses the login")
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    client = MfxClient(
        args.base_url, args.key_id, SigningKey(private_key(args))
    )
    first = await client.terminals.get(args.terminal_id)
    print(f"{args.terminal_id} is {first.state}; yours on its login: "
          f"{field(first, 'same_login_terminals')}")  # fmt: skip

    # One more terminal is one more session on the login. When the server's limit is final and
    # yours already fill it, the broker would refuse the login: there is nothing to show.
    cap = await known_cap(client, field(first, "server")["id"])
    in_use = field(first, "same_login_terminals") or 1
    if cap is not None and in_use + 1 > cap:
        skip(f"not added: this server allows {cap} sessions on one login and yours use {in_use}")
        return

    login = int(input("Broker login (the one that terminal already has): "))
    server = input("Broker server: ").strip()
    password = Secret(getpass.getpass("Password: "))

    second = await add_terminal(client, first)
    print(f"requested {second.id}: {second.state}")
    try:
        await submit_login(client, str(second.id), login=login, server=server, password=password)
    except MfxApiError as refused:
        # Nothing was stored. The terminal you requested has no credential; retire it.
        await client.terminals.retire(str(second.id), idempotency_key=f"retire-{uuid.uuid4()}")
        await wait_until_retired(client, str(second.id))
        raise SystemExit(f"REFUSED: {explain(refused)}") from refused

    state = await wait_until_live(client, str(second.id))
    for terminal_id in (args.terminal_id, str(second.id)):
        seen = await client.terminals.get(terminal_id)
        print(f"{terminal_id} {seen.state}; yours on its login: "
              f"{field(seen, 'same_login_terminals')}")  # fmt: skip
    print("PASS" if state == "ACTIVE" else f"FAILED: the new terminal is {state}; read its conditions")


if __name__ == "__main__":
    asyncio.run(main())
