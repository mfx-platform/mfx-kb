# Reading price history

> The runnable half of this page is [`samples/price_history.py`](../samples/price_history.py),
> which reads bars and ticks against dev and places nothing.

MFX Hub streams prices as they happen. This page is about the other direction: the bars and ticks
that already happened, for a chart you are backfilling or a strategy you are warming up.

## Where the history comes from

**From your own broker, through your own terminal.** MFX Hub holds no broker account and keeps no
price store of its own. When you ask for history, one of your ACTIVE terminals on that broker's
server reads it from the server and the answer is passed to you. Three things follow.

- The bars and ticks are exactly the ones your broker shows your account. Two brokers disagree
  about the same minute, and so will two answers.
- How far back they go is the broker's to say: usually years of daily bars, months of one-minute
  bars, and weeks of ticks for the majors.
- **You need an ACTIVE terminal on the server.** Without one the answer is
  `409 no_terminal_on_server`. With terminals on more than one of the broker's servers, say which
  with `server_id`, or the answer is `409 ambiguous_symbol` and names them.

You do not need to have subscribed to the symbol, and it does not need to be one of the terminal's
instruments. A terminal can read the history of any symbol its server lists.

## Bars

```python
bars = await client.md.candles(broker_id, "EURUSD.sd", "M5", from_=start, to=end)
for bar in bars.items:
    print(bar.ts, bar.o, bar.h, bar.l, bar.c, bar.v)
```

| | |
|---|---|
| Timeframes | `M1` `M5` `M15` `M30` `H1` `H4` `D1` `W1` `MN1`. Any other is a `ValueError` before anything is sent |
| A bar | `ts` its open time, `o` `h` `l` `c` decimal strings, `v` the tick volume |
| Order | oldest first |
| At most | 1,000 bars a call |
| **A window that holds more** | answers with the **newest** 1,000. A chart asking for "up to now" gets the bars nearest now |
| The bar still forming | is included when the window reaches now. Its close is the latest price, not a final one |
| No bar in the window | an empty answer, not an error: a weekend, or a symbol that did not trade |

**To read further back, move `to` earlier.** Ask again with `to` just before the oldest bar you
were given. A page that comes back with fewer than 1,000 bars is the start of the window. The
sample's `bars_newest_first` does exactly this.

**`ts` is a true instant.** Brokers keep their own clock, often two or three hours ahead of UTC, and
a daily bar opens at the broker's midnight. `ts` says when that was in UTC, so a `D1` bar from such
a broker starts at 21:00 or 22:00 UTC. That is correct, and it is why two brokers' daily bars
differ.

Prices are strings for the same reason they are everywhere in MFX Hub: build a `Decimal` from them
and nothing is lost. See [Sizing a position](sizing-a-position.md).

## Ticks

```python
page = await client.md.ticks(broker_id, "EURUSD.sd", from_=since)
for tick in page.items:
    print(tick.ts, tick.bid, tick.ask, tick.last, tick.volume)
print(page.more)
```

| | |
|---|---|
| Direction | **forward** from `from_`: the first ticks at or after it, oldest first |
| `to` | optional. Ticks after it are left out; without it the read runs to the newest tick |
| At most | 1,000 ticks a call, which is also the default |
| `more` | `True` when the window holds ticks beyond the ones returned |
| A tick | `ts` to the millisecond, `bid` and `ask` decimal strings, `last` and `volume` or `None` |

Most FX brokers send no last price and no volume with a tick. They arrive as `None`, never as zero.

### Reading a whole window

To continue, ask again from the last tick's `ts`. There is one trap: **several ticks can share a
millisecond**, so the next answer begins with ticks you already hold. Skip everything at that
millisecond and you lose the ones you had not seen. Skip nothing and you count some twice.

`all_ticks` does the bookkeeping:

```python
async for tick in client.md.all_ticks(broker_id, "EURUSD.sd", from_=since, to=until):
    ...
```

Every tick of the window comes once, in order.

**Every call is a round trip to your terminal.** A busy symbol passes 100,000 ticks in a day, which
is a hundred calls. Tick history is for reading the past. To follow ticks as they happen, subscribe
to the stream: see [Following an account live](following-an-account-live.md) for how the stream
works.

## When it does not answer

| What you get | What it means |
|---|---|
| `404 not_found`, `details.reason` `symbol_unknown` | the server does not list that symbol. Names are the broker's own, suffix and all: read them from `client.catalog.symbols` |
| `409 no_terminal_on_server` | you have no ACTIVE terminal on that broker's server to read through |
| `409 ambiguous_symbol` | you have terminals on several of the broker's servers; pass `server_id` |
| `502` | the terminal did not answer in ten seconds, or could not read the history. The first read of a symbol's history can be slow while the terminal fetches it from the broker. Ask again |
| An empty answer | nothing in the window. Not an error |

The reads are counted in your call log as market-data calls. They need the `md:read` scope and no
more.

## From a script

The synchronous client has the same two reads, for a script with no event loop:

```python
from mfx import SyncMfxClient

with SyncMfxClient(base_url, key_id, key) as client:
    bars = client.candles(broker_id, "EURUSD.sd", "H1", from_=start, to=end)
    page = client.ticks(broker_id, "EURUSD.sd", from_=since)
```
