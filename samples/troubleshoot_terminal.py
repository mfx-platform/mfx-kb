"""Find out why a terminal is not doing what you expect: its state, its conditions in the broker's own
words, its MetaTrader journal, and any planned maintenance. The runnable half of
guides/troubleshooting-a-terminal.md.

Three reads, in the order to make them:

1. **The terminal.** `state` says where it is in its life, `server_kind` whether its server is a
   demo or a live one, and each condition that is not ok says why in `reason`. When the broker
   refused a login, `broker_words` is the broker's own sentence and `refusal_kind` sorts it
   (`invalid_account` for a wrong login or password, `other` for the rest).
2. **The journal.** MetaTrader's own log, newest line first: read live while the terminal runs,
   and kept for 24 hours after it stops, fails or is retired (`from` is `last_words` then).
3. **Maintenance.** A window in progress suspends some calls for you; `client.maintenance.list()`
   says which, and until when.

Run it against dev:

    python samples/troubleshoot_terminal.py --key-id mfxk_... --private-key-file key.txt \
        --terminal-id <uuid>

It only reads. `--json` ends with one line the platform's checks read:
{"outcome": "passed" | "failed", "notice", "detail"}. It passes when every read was answered as
documented, whatever the terminal's state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mfx import MfxClient
from mfx.errors import MfxApiError

BASE_URL = "https://api.dev.mfxhub.com/v1"
#: How many journal lines to show; the platform serves 1 to 200.
LINES = 20


def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()


def not_ok(terminal: Any) -> dict[str, dict[str, Any]]:  # noqa: ANN401
    """Each condition that is not ok, as the platform sent it."""
    shown = terminal.to_dict().get("conditions") or {}
    return {name: c for name, c in shown.items() if c.get("status") not in (None, "ok")}


async def journal(client: MfxClient, terminal_id: str) -> tuple[str, dict[str, Any]]:
    """The journal's last lines. A terminal that is not running and kept no lines is a documented
    409 not_running, not a failure of this read."""
    try:
        read = await client.terminals.journal(terminal_id, lines=LINES)
    except MfxApiError as refused:
        reason = refused.details.get("reason")
        if refused.status == 409 and reason == "not_running":
            print("journal: the terminal is not running and kept no lines")
            return "passed", {"journal": "not_running"}
        print(f"journal: {refused.status} {refused.code}, reason {reason}")
        return "failed", {"journal": f"{refused.status} {refused.code}"}
    print(f"journal ({read.from_.value}, {len(read.lines)} lines):")
    for line in read.lines:
        print(f"  {line.ts:%Y-%m-%d %H:%M:%S}  {line.source:<10}  {line.message}")
    return "passed", {"journal": read.from_.value, "lines": len(read.lines)}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--terminal-id", required=True)
    parser.add_argument("--json", action="store_true", help="end with the check's result line")
    parser.add_argument("--base-url", default=os.environ.get("MFX_BASE_URL", BASE_URL))
    args = parser.parse_args()

    async with MfxClient(args.base_url, args.key_id, private_key(args)) as client:
        terminal = await client.terminals.get(args.terminal_id)
        kind = terminal.to_dict().get("server_kind") or "not known yet"
        print(f"terminal {terminal.id}: {terminal.state.value}, server {kind}")
        for name, condition in not_ok(terminal).items():
            print(f"  {name}: {condition.get('status')} - {condition.get('reason')}")
            if condition.get("broker_words"):
                print(f"    the broker said ({condition.get('refusal_kind')}): "
                      f"{condition['broker_words']}")  # fmt: skip

        outcome, detail = await journal(client, args.terminal_id)

        windows = [window async for window in client.maintenance.list()]
        for window in windows:
            suspended = ", ".join(s.value for s in window.suspended) or "nothing"
            print(f"maintenance {window.label}: {window.state.value}, {window.starts_at:%H:%M} to "
                  f"{window.ends_at:%H:%M} UTC, suspends {suspended}")  # fmt: skip
        if not windows:
            print("maintenance: none scheduled or in progress")
    detail = {**detail, "state": terminal.state.value, "maintenance": len(windows)}
    print(f"troubleshoot: {outcome}")
    if args.json:
        print(json.dumps({"outcome": outcome, "notice": None, "detail": detail}))
    return 1 if outcome == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
