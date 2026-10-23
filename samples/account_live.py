"""Follow one terminal's account live: a snapshot, then the marks that move it.

The runnable half of guides/following-an-account-live.md. It subscribes to one terminal's account
channel, merges what arrives into a single current view, and prints a line whenever a number moves::

    python samples/account_live.py --key-id mfxk_... \\
        --private-key-file C:\\secure\\mfx-dev.key --terminal 0191f0c2-...

Your key needs the scope `acct:read`, and the terminal must be yours.

The point of the sample is the merge, so it is worth saying what it is *not* doing. It does not ask
for the account over REST and it does not poll: everything printed below arrived on the stream. It
does not rebuild its view from each message either - a marks message sets three numbers on one
position, which is what makes this affordable at a 250 ms refresh rate and with hundreds of
positions open.

`LiveAccount` applies the four rules in the guide for you. The loop below is what using it looks
like in full: apply every event, draw when something changed, and `resume` when it says you are
behind. If you are not using the SDK, the rules are in the guide and they are the whole protocol -
there is nothing else to implement.

Amounts arrive as decimal strings (`"10012.40"`), never floats, so they are printed as they came and
parsed with `Decimal` where they are compared. Do the same: a balance through a float is a balance
that can be wrong in the last place.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import asyncio
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mfx import Channel, LiveAccount, MfxClient, Resync, SigningKey

BASE_URL = "https://api.dev.mfxhub.com/v1"
#: What the guide calls stale: nothing at all for 15 s with positions open, three missed
#: keep-alives. The platform says so itself on the REST read; a client that draws its own screen
#: has to notice it on its own.
STALE_AFTER_S = 15.0
#: As `AccountSnapshotMessage` and `AccountMarksMessage` name them. `margin_free`, not
#: `free_margin`: the cell sends the latter and the platform renames it on the way through, so a
#: sample written from the cell's field names prints blanks for a figure that did arrive.
FIGURES = ("balance", "equity", "margin", "margin_free", "margin_level")

def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()



def amount(value: Any) -> Decimal | None:
    """A decimal string as a Decimal, or None if it was not one.

    Not `float(value)`: these are money. And not a crash either - a figure this sample does not
    recognise is a figure the platform added, which is a reason to print it as it came and carry on.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def figures_line(view: Any) -> str:
    shown = [f"{name}={view.figures[name]}" for name in FIGURES if name in view.figures]
    return "  ".join(shown)


def positions_line(view: Any) -> str:
    """Each open position's floating profit, worst first - which is the order anyone looks in."""
    rows = sorted(
        view.positions.values(),
        key=lambda row: amount(row.get("profit")) or Decimal(0),
    )
    return "  ".join(
        f"{row.get('symbol', '?')}#{row['ticket']} {row.get('side', '?')}"
        f" {row.get('volume', '?')} @ {row.get('price_current', '?')}"
        f" ({row.get('profit', '?')})"
        for row in rows
    )


class Freshness:
    """When the last message arrived, and whether we have already said it went quiet.

    Staleness cannot be noticed in the message loop, because the thing being noticed is the
    *absence* of a message - the loop is blocked on the socket at exactly the moment there is
    something to say. It takes its own timer, which is why this is a watchdog and not an `if`
    after the print.
    """

    def __init__(self) -> None:
        self.at = time.monotonic()
        self.said = False

    def arrived(self) -> None:
        self.at, self.said = time.monotonic(), False


async def watchdog(fresh: Freshness, live: LiveAccount) -> None:
    """Say so once when nothing has arrived for 15 s with positions open."""
    while True:
        await asyncio.sleep(1.0)
        quiet = time.monotonic() - fresh.at
        view = live.view
        if quiet > STALE_AFTER_S and not fresh.said and view is not None and view.positions:
            fresh.said = True
            print(f"! stale - nothing for {quiet:.0f}s with positions open; these are not live")


async def follow(
    client: MfxClient, terminal_id: str, seconds: float = 0, heard: list[int] | None = None
) -> int:
    """Follow until stopped, or for ``seconds``; the number of messages applied. ``heard``, when
    given, is kept up to date as messages arrive, so a caller that stops this early still knows."""
    channel = Channel.account(terminal_id)
    live = LiveAccount(terminal_id)
    started = time.monotonic()
    fresh = Freshness()
    applied = 0

    async with client.stream() as stream:
        await stream.subscribe(channel)
        print(f"following {terminal_id} on {channel} - the first snapshot arrives on subscribe")
        watch = asyncio.create_task(watchdog(fresh, live))
        try:
            async for event in stream:
                updated = live.apply(event)
                if updated is None:
                    if isinstance(event, Resync):
                        print("! the replay window passed; asking for a fresh snapshot")
                    if live.behind:
                        # Rule 3. The answer on this channel is a snapshot, never a replay, so this
                        # costs one message and puts the view back on its feet at once. Waiting is
                        # also correct: a snapshot comes within 60 s by itself.
                        print("! behind - a message could not be applied; resuming")
                        await stream.resume(channel)
                    continue

                # Any message counts, including a keep-alive carrying no marks at all. That is what
                # the keep-alive is for: it is how a still account proves it is still connected.
                fresh.arrived()
                applied += 1
                if heard is not None:
                    heard[0] = applied
                elapsed = int(time.monotonic() - started)
                kind = "snapshot" if updated.seq == updated.base else "marks   "
                print(f"[{elapsed:>5}s] {kind} seq={updated.seq} {figures_line(updated)}")
                if updated.positions:
                    print(f"          {positions_line(updated)}")
                if seconds and time.monotonic() - started >= seconds:
                    break
        finally:
            watch.cancel()
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--terminal", required=True, help="the terminal id to follow")
    parser.add_argument("--seconds", type=float, default=0, help="stop after this long; 0 never")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    key = SigningKey(private_key(args))

    async def run() -> int:
        async with MfxClient(args.base_url, args.key_id, key) as client:
            if not args.seconds:
                return await follow(client, args.terminal)
            # The snapshot arrives on subscribe, so a bounded run that heard nothing at all is a fault
            # worth a non-zero exit. An account with nothing open may send nothing after it, so the
            # run ends on time whether or not more arrives, keeping what it heard.
            heard = [0]
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(args.seconds):
                    return await follow(client, args.terminal, args.seconds, heard)
            return heard[0]

    try:
        heard = asyncio.run(run())
    except KeyboardInterrupt:
        print("\nstopped")
        return
    if args.seconds:
        print(f"{heard} message(s) in {args.seconds:g} s")
        raise SystemExit(0 if heard else 1)


if __name__ == "__main__":
    main()
