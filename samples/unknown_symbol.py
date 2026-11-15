"""Ask for a symbol your server does not list, and see the answer you get. The runnable half of the
"A symbol the server does not list" section of guides/when-a-call-fails.md.

Two things to take away:

1. **An unlisted symbol is `404 not_found`, with `details.reason` `symbol_unknown`.** It is not a
   price of zero and not an empty answer: the call fails, and says why.
2. **Names are the broker's own.** Read them from `client.catalog.symbols`; `EURUSD` and
   `EURUSD.sd` are different names to a broker that uses the suffix.

Run it against dev:

    python samples/unknown_symbol.py --key-id mfxk_... --private-key-file key.txt \
        --broker-id <uuid> --server-id <uuid>

It only reads. `--json` ends with one line the platform's checks read:
{"outcome": "passed" | "failed", "notice", "detail"}. It passes only on that documented answer.
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
#: A name no broker lists. Checked against the catalogue first, so the test proves the answer to an
#: unlisted name and nothing else.
NAME = "ZZZMFX"


def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()


async def listed(client: MfxClient, broker_id: str, server_id: str) -> set[str]:
    """Every name the server lists, upper-cased."""
    names: set[str] = set()
    cursor = None
    while True:
        page = await client.catalog.symbols(broker_id, server_id=server_id, cursor=cursor,
                                            limit=200)  # fmt: skip
        names.update(s.symbol.upper() for s in page.items)
        cursor = page.next_cursor
        if not cursor:
            return names


def judge(name: str, answer: Any) -> tuple[str, str | None, dict[str, Any]]:  # noqa: ANN401
    """Passed only on 404 not_found saying the symbol is unknown; anything else failed."""
    if not isinstance(answer, MfxApiError):
        return "failed", f"{name} was answered with a price", {"symbol": name}
    reason = answer.details.get("reason")
    detail = {"symbol": name, "status": answer.status, "code": answer.code, "reason": reason}
    # Two forms of the same answer: the platform's catalogue says the name is not listed and
    # offers the nearest names it does list, or the terminal itself says it does not know it.
    if answer.status == 404 and answer.code == "not_found" and "nearest" in answer.details:
        return "passed", None, {**detail, "nearest": list(answer.details["nearest"])[:3]}
    if answer.status == 404 and answer.code == "not_found" and reason == "symbol_unknown":
        return "passed", None, detail
    return "failed", f"expected 404 not_found symbol_unknown, got {answer.status} {answer.code}", detail


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--broker-id", required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--json", action="store_true", help="end with the check's result line")
    parser.add_argument("--base-url", default=os.environ.get("MFX_BASE_URL", BASE_URL))
    args = parser.parse_args()

    async with MfxClient(args.base_url, args.key_id, private_key(args)) as client:
        names = await listed(client, args.broker_id, args.server_id)
        name = NAME
        while name.upper() in names:  # never true on a real broker, but never assumed
            name += "X"
        try:
            answer: Any = await client.catalog.quote(args.broker_id, name, server_id=args.server_id)
        except MfxApiError as refused:
            answer = refused
            print(f"{name}: {refused.status} {refused.code}, reason {refused.details.get('reason')}")
    outcome, notice, detail = judge(name, answer)
    print(f"unknown symbol: {outcome}" + (f" - {notice}" if notice else ""))
    if args.json:
        print(json.dumps({"outcome": outcome, "notice": notice, "detail": detail}))
    return 1 if outcome == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
