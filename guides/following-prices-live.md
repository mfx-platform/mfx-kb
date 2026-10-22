# Following prices live

> The runnable half of this page is [`samples/prices_live.py`](../samples/prices_live.py), which
> follows a symbol on dev and prints what it hears.

Prices in MFX Hub are your broker's, read by your own terminals and streamed to you tick by tick.
This page is how to ask for them, and how to know when they have stopped.

## Two steps, in this order

```python
await client.md.subscribe(server_id, "EURUSD.sd", idempotency_key=key)

async with client.stream() as stream:
    await stream.subscribe(Channel.md(server_id, "EURUSD.sd"))
    async for message in stream:
        ...
```

1. **The subscription** is your standing request for one symbol on one broker server. It is created
   once and it stays: closing a connection does not drop it, and a reconnect does not need to make it
   again. It counts against your plan until you unsubscribe.
2. **The channel** `md:{server_id}:{symbol}` on the stream is where the ticks arrive.

Open the channel without the subscription and it answers `not_subscribed`. Nothing else on your
stream is affected; the refusal is about that one channel.

`symbol` is the broker's own name, suffix and all: `EURUSD.sd` is not `EURUSD`. Read the names from
`client.catalog.symbols`.

**One of your terminals must be watching the symbol.** A subscription asks for a symbol's ticks to
be streamed to you. It does not put the symbol on a terminal. If no ACTIVE terminal of yours on that
server has the symbol among its instruments, there is nothing to stream, and the subscription tells
you so (below) about three minutes later.

## A tick

Each message on the channel is one tick: `bid` and `ask` as decimal strings, exactly as your broker
sent them. Build a `Decimal` from them; see [Sizing a position](sizing-a-position.md) for why.

You get every tick, once. If several of your terminals watch the same symbol you still get each
tick once: see [Several terminals on one login](several-terminals-on-one-login.md).

## A quiet channel proves nothing

When no tick arrives, the channel cannot tell you why. A shut market, a terminal that is gone and a
symbol your broker stopped quoting all look the same: silence. **The subscription can tell you.**

Its `state` is one of:

| State | Means |
|---|---|
| `pending` | created, no tick yet |
| `streaming` | delivering |
| `degraded` | the symbol cannot tick, or should be ticking and is not. `degraded_reason` says why and `degraded_since` says from when |

And you are told the moment it changes, on the `events` channel:

```python
from mfx import SubscriptionDegraded, SubscriptionRecovered, subscription_event

await stream.subscribe(Channel.EVENTS)
async for message in stream:
    health = subscription_event(message)
    if isinstance(health, SubscriptionDegraded):
        pause(health.symbol, why=health.reason)
    elif isinstance(health, SubscriptionRecovered):
        resume(health.symbol)
```

`subscription_event` returns `None` for every other message, so you can ask it of each one you
read. Each change is sent once: one event when a subscription becomes degraded, one when it
recovers.

| Reason | What happened | What to do |
|---|---|---|
| `symbol_removed` | your broker no longer lists the symbol | unsubscribe |
| `subscription_refused` | your broker refused the symbol on your terminals | check the name against the catalogue |
| `terminal_disconnected` | you have no ACTIVE terminal on the server | bring a terminal back |
| `report_stale` | a terminal is ACTIVE and none is reporting the symbol | check the symbol is among a terminal's instruments |
| `no_ticks_in_session` | the market is open by your broker's own hours, and it has stopped quoting | wait; it recovers by itself |

The reason is the platform's own word and the list can grow. Treat one you do not know as "not
delivering" and read the subscription.

### What is not a degradation

- **A shut market.** Silence counts only while your broker's own hours say the symbol is trading.
  Over a weekend the subscription stays `streaming` and no event is sent.
- **A restart.** A terminal that restarts, or is updated, is back within a few minutes and nothing
  is sent.

### Three things worth knowing

- **A degraded subscription keeps its channel.** Ticks that do arrive are delivered, and you do not
  subscribe again when it recovers.
- **A recovery carries the whole gap**: `reason`, `since` and `recovered_at`. You do not need to
  have kept the first event.
- **The two events survive a short disconnect.** When the stream reconnects it replays the events
  you missed. After a long one it tells you it cannot (a `Resync` for `events`), and then you list
  your subscriptions: the state is always there.

How soon you hear: a lost terminal three to four minutes after it went, a market that stopped
quoting five to seven minutes after its last tick.

## After a disconnect

The stream reconnects and your channels are asked for again. Prices are not replayed: the channel
carries what is happening now, and the next tick you get is the newest one. If you need the ticks
you missed, read them: see [Reading price history](reading-price-history.md).

## Scopes

Following a subscription that exists, and hearing its two events, needs `md:read`. Creating or
removing one needs `md:write`.
