"""Follow a symbol's prices live, and know when they stop — the runnable half of
guides/following-prices-live.md.

Three things to take away:

1. **Two steps, in this order.** A *subscription* is your standing request for a symbol on a broker's
   server. A *channel* on the stream is where its ticks arrive. Open the channel without the
   subscription and it answers `not_subscribed`.
2. **A quiet channel proves nothing.** A market that is shut, a terminal that is gone and a symbol
   the broker stopped quoting all look the same from the channel: nothing arrives. The subscription
   says which it is, in its `state`, and tells you the moment it changes with two events.
3. **One of your terminals must be watching the symbol.** The prices are your broker's, read by your
   own terminals. A subscription asks for them to be streamed to you; it does not put the symbol on a
   terminal.

Run it against dev:

    python samples/prices_live.py --key-id mfxk_... --private-key-file key.txt \
        --server-id <uuid> --symbol EURUSD.sd --seconds 30

Add --subscribe to create the subscription first (your key needs `md:write` for that; following one
that exists needs only `md:read`).
"""

from __future__ import annotations

import argparse
import os
import asyncio
import uuid
from pathlib import Path

from mfx import (
    Channel,
    ChannelRefused,
    MfxClient,
    Resync,
    SigningKey,
    SubscriptionDegraded,
    SubscriptionRecovered,
    subscription_event,
)

BASE_URL = "https://api.dev.mfxhub.com/v1"

#: What each reason means for you. The reason is the platform's own word and new ones can appear:
#: treat one you do not know as "not delivering" and read the subscription.
WHAT_TO_DO = {
    "symbol_removed": "your broker no longer lists the symbol; unsubscribe",
    "subscription_refused": "your broker refused the symbol on your terminals; check its name",
    "terminal_disconnected": "you have no ACTIVE terminal on this server; bring one back",
    "report_stale": "no terminal of yours is reporting the symbol; is it in a terminal's instruments?",
    "no_ticks_in_session": "the market is open and the broker is not quoting; nothing to do but wait",
}

def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()



async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    parser.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    parser.add_argument("--server-id", required=True, help="the broker server the symbol is on")
    parser.add_argument("--symbol", required=True, help="the broker's own name, e.g. EURUSD.sd")
    parser.add_argument("--seconds", type=float, default=30, help="how long to follow")
    parser.add_argument("--subscribe", action="store_true", help="create the subscription first")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    key = SigningKey(private_key(args))

    async with MfxClient(args.base_url, args.key_id, key) as client:
        # -- step one: the subscription -------------------------------------------------------------
        if args.subscribe:
            # Subscribing again with the same idempotency key returns the same subscription. It
            # outlives this script: it is yours until you unsubscribe, and it counts against your plan.
            await client.md.subscribe(
                args.server_id, args.symbol, idempotency_key=f"prices-{uuid.uuid4()}"
            )
        mine = [
            s
            async for s in client.md.all_subscriptions()
            if str(s.server.id) == args.server_id and s.symbol == args.symbol
        ]
        if not mine:
            print(f"no subscription for {args.symbol} on that server; run again with --subscribe")
            return
        subscription = mine[0]
        # `pending` until the first tick; `streaming`; or `degraded`, with the reason and the moment.
        print(f"{args.symbol}: {subscription.state.value}")
        if subscription.degraded_reason:
            reason = str(subscription.degraded_reason)
            print(f"  degraded since {subscription.degraded_since}: {reason}")
            print(f"  -> {WHAT_TO_DO.get(reason, 'not delivering; the reason is new to this sample')}")

        # -- step two: the channel, and the events that say when it stops -----------------------------
        ticks = 0
        last = None
        async with client.stream() as stream:
            await stream.subscribe(Channel.md(args.server_id, args.symbol))
            await stream.subscribe(Channel.EVENTS)

            async def follow() -> None:
                nonlocal ticks, last
                async for message in stream:
                    if isinstance(message, ChannelRefused):
                        # About one channel only; the rest of the stream carries on.
                        print(f"refused {message.channel}: {message.code} {message.message}")
                        continue
                    if isinstance(message, Resync):
                        continue  # events older than the replay window: read state over REST
                    health = subscription_event(message)
                    if isinstance(health, SubscriptionDegraded):
                        print(f"DEGRADED {health.symbol} since {health.since}: {health.reason}")
                        print(f"  last tick {health.last_tick_at}")
                        print(f"  -> {WHAT_TO_DO.get(health.reason, 'not delivering')}")
                    elif isinstance(health, SubscriptionRecovered):
                        gap = (health.recovered_at - health.since).total_seconds()
                        print(f"RECOVERED {health.symbol} after {gap:.0f} s of {health.reason}")
                    elif message.channel.startswith("md:"):
                        ticks += 1
                        last = message.data
                        if ticks <= 3:
                            # bid and ask are decimal strings: Decimal() takes them exactly.
                            print(f"  tick bid {last['bid']} ask {last['ask']}")

            try:
                await asyncio.wait_for(follow(), timeout=args.seconds)
            except TimeoutError:
                pass

        print(f"{ticks} ticks in {args.seconds:g} s", end="")
        print(f"; last bid {last['bid']} ask {last['ask']}" if last else "")
        if not ticks:
            # Silence is not a diagnosis. The subscription is.
            now = [
                s
                async for s in client.md.all_subscriptions()
                if str(s.server.id) == args.server_id and s.symbol == args.symbol
            ]
            state = now[0].state.value if now else "gone"
            print(f"no tick arrived. The subscription reads {state}: a shut market is not degraded")


if __name__ == "__main__":
    asyncio.run(main())
