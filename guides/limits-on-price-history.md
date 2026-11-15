---
title: "Limits on price history, and how to stay clear of them"
topic: market-data
order: 30
summary: "Which history reads are cheap and which are not, what is limited and why, and the habits that keep you clear of every limit."
---

# Limits on price history, and how to stay clear of them

> The runnable half of this page is [`samples/candle_cache.py`](../samples/candle_cache.py)
> ([TypeScript](../samples/ts/candle_cache.ts)), which keeps bars cached, reads only what is missing
> and waits when it is told to. It places nothing and subscribes to nothing.

Reading bars is cheap when MFX Hub already holds them and expensive when it does not. This page says
which is which, what MFX Hub limits and why, and the four habits that keep a chart or a strategy well
clear of every limit.

## Where an answer comes from

Every answer to `GET /v1/md/candles` says where its bars came from, in `source`:

| `source` | What it means | What it cost |
|---|---|---|
| `store` | MFX Hub's candle store held the whole window | nothing on your terminals; never limited per terminal |
| `terminal` | one of your ACTIVE terminals on the server read the bars from the broker | one read on that terminal |
| `store+terminal` | closed bars from the store, the rest (usually the bar still forming) from a terminal | one read on that terminal |

The store keeps the closed bars of the series you read: the first time you read a symbol and
timeframe, MFX Hub starts keeping it, and from then on the closed bars it vouches for are answered
from the store. `complete_from` and `complete_to` on a store answer say the span it holds whole.
Ticks are not stored: every tick read goes to a terminal (see [reading price
history](reading-price-history.md)).

## What is limited, and why

A read that reaches a terminal makes MetaTrader on that terminal load the history you asked for. A
read years deep loads years of history. A terminal flooded with such reads spends its time loading
the past instead of serving your prices and orders, and can stop answering altogether. So reads that
reach a terminal are limited in two ways:

| Limit | Past it |
|---|---|
| **Reads a minute, per terminal.** Each terminal serves a set number of reads a minute that the store could not answer | `429` with code `terminal_reads_limit`. `Retry-After` is the seconds to the start of the next minute; `details.limit` is the reads a minute and `details.terminal_id` the terminal |
| **Reads at once, per server.** A few of your reads through terminals on one server run at a time; the rest wait up to two seconds for a turn | `429` with code `reads_busy`, `Retry-After` 1 |

Reads the store answers whole are never counted against either. Neither is anything MFX Hub reads on
its own to keep the store full. Ordinary use, a chart opened, a strategy warmed up, a gap filled,
never comes near the per-terminal limit; it is there for a client that asks the same terminal for
deep history over and over.

## Four habits

**1. Keep closed bars once you have them.** A closed bar never changes. Store it on your side and do
not ask for it again. The sample keeps them in a small file:

```python
class BarCache:
    """Closed bars kept on disk, one series per (broker, server, symbol, timeframe)."""
```

Keep only closed bars. The bar still forming changes with every tick, and its close is the latest
price, not a final one.

**2. Read only the range you are missing.** With bars cached, the next read starts just after the
newest one you hold. That is one small read, usually answered by the store, instead of the whole
window again:

```python
    # Only the range after the newest closed bar we hold: one small read, not the whole history.
    begin = newest + BAR[timeframe] if newest is not None else start
```

**3. Follow new bars on the stream instead of polling.** Subscribe to a symbol with the timeframes
you want, and each closed bar arrives once as `md.candle_closed` on the `candles` channel for that
server, symbol and timeframe. Its `prev_ts` is the open time of the bar before it: when that is not
the newest bar you hold, you missed one, and a single read from your newest bar fills the gap. See
[following prices live](following-prices-live.md) for subscribing and the stream.

**4. When you are told to wait, wait.** The SDKs already do the first part: a read refused with `429`
is asked again after the server's own `Retry-After`, a few times. If it is still refused, the SDK
raises `RateLimitError` with the code. Wait `retry_after_s` and carry on; never retry in a tight
loop, which only spends the next minute too:

```python
        except RateLimitError as refused:
            if refused.code != "terminal_reads_limit" or wait == OWN_WAITS:
                raise
            seconds = refused.retry_after_s or 1
```

## Back-filling many symbols

A new chart wall or a strategy warming up across dozens of symbols and timeframes needs history it
has never read, which the store does not hold yet, so every first read goes to a terminal.

- **Spread it out.** Fill a few series a minute rather than all at once. The reads a minute are per
  terminal, so a back-fill spread over a few minutes never meets the limit.
- **Ask for what you will show.** A one-minute chart rarely needs years of one-minute bars. Read the
  span on screen first, and older pages only when someone scrolls back.
- **Read each series once.** After the first fill, the store holds the series and habit 2 keeps every
  later read small.

## When it does not answer

| What you get | What it means | What to do |
|---|---|---|
| `429 terminal_reads_limit` | the terminal that would serve the read has served its reads for this minute | wait `Retry-After` (to the next minute); keep bars cached and read only what is missing |
| `429 reads_busy` | several of your reads through terminals on that server were already running | ask again after `Retry-After`; spread reads out |
| `200` with `Retry-After` | the store is still repairing a gap in the window; `complete_to` says how far it is whole | use the bars up to `complete_to`, and ask again after `Retry-After` for the rest |

Everything else a history read can answer is in [reading price history](reading-price-history.md)
and [when a call fails](when-a-call-fails.md).
