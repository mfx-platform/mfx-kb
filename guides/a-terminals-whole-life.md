# A terminal's whole life

The sample this guide follows is
[`../samples/terminal_lifecycle.py`](../samples/terminal_lifecycle.py).

[Your first terminal](your-first-terminal.md) asks for one terminal and watches it go live. This
guide takes terminals you already have through the rest of their life — **retire them, then ask for
them again** — and shows what you will see at each step.

## What the states mean to you

| State | What is happening | What you do |
|---|---|---|
| `REQUESTED` | Accepted; waiting for room on a cell | Submit the credential |
| `PROVISIONING` | A cell is preparing the terminal's folder | Wait |
| `LOGGING_IN` | The terminal is logging in to your broker | Wait |
| `ACTIVE` | Logged in, connected, ticks flowing | Use it |
| `OFFLINE` | The cell running your terminal is down | Nothing — see [below](#when-a-terminal-goes-offline) |
| `DRAINING` | You retired it; its cell is stopping it | Wait |
| `RETIRED` | Stopped and removed from its cell | Nothing — it stays in your list as history |
| `FAILED` | The broker refused the login, or it did not finish in time | Read `conditions`, then [submit the credential again](changing-a-terminals-password.md), or retire it |

`state_since` tells you when it entered the state, and `state_expected_s` how long that state
usually takes — so you can tell "on time" from "longer than usual" without guessing.

## When a terminal goes `OFFLINE`

`OFFLINE` means the machine your terminal runs on — its **cell** — cannot run it right now. The
terminal itself is fine, and so is your credential.

**There is nothing for you to do.** MFX notices within minutes and fixes it: the terminal comes
back to `ACTIVE` on its own when its cell reports again, or MFX moves it to another cell in your
farm if the cell is not coming back. You do not retire it, you do not re-submit the credential, and
you do not ask for a replacement — a new terminal would be a new id and a new login for a problem
that is already being handled.

`offline_reason` says which of three things happened:

| `offline_reason` | What it means |
|---|---|
| `power_off` | The cell was turned off outside MFX |
| `vm_missing` | The cell's machine no longer exists, so the terminal is moved to another cell |
| `no_heartbeat` | The cell has not reported for three minutes |

While a terminal is `OFFLINE`, requests against it fail rather than wait. **Retry them** — that is
the right response, and it is what the platform expects:

```python
from mfx import is_usable

terminal = await client.terminals.get(terminal_id)
if not is_usable(terminal):
    # OFFLINE, or not live yet. Come back in a few seconds; do not re-create it.
    ...
```

`is_usable()` is true only for `ACTIVE`, so it stays right as the platform grows states. Checking
`state == "ACTIVE"` yourself works today and will keep working — it is just one more place to
remember.

## Retiring is an intent, not a deletion

```python
answer = await client.terminals.retire(terminal.id, idempotency_key="retire-42")
answer.state   # "DRAINING" — accepted, not finished
```

`DELETE` answers at once with the terminal **`DRAINING`**. It becomes `RETIRED` only when its cell
reports that the terminal has stopped, so poll until then. Three things worth knowing:

- Retiring a terminal that is already `DRAINING` or `RETIRED` is **not an error**; you get it back
  as it is. Retry freely.
- A terminal that is on its way to a cell (`PROVISIONING`, `LOGGING_IN`) answers **409** for now.
  Wait until it settles, then retire it.
- A `REQUESTED` or `FAILED` terminal has nothing running, so it is `RETIRED` immediately.

## Asking again

A retired terminal is never revived: you ask for a new one, and it gets a new id. Give it the
instruments you want ticks for, **in your broker's own names** (`EURUSD.sd`, not `EURUSD`, if that
is what your broker calls it) — MFX never renames an instrument on your behalf.

One `Idempotency-Key` makes one terminal. Send the same key with the same body and you get the
original back; send it with a different body and you get a **409** that names the original.

## What "live" means

`ACTIVE` plus three conditions reading `ok`:

| Condition | It is `ok` when |
|---|---|
| `ProcessRunning` | the terminal process is up on its cell |
| `BrokerConnected` | the broker connection answered recently |
| `TicksFresh` | a tick arrived for one of your instruments in the last few seconds, or their market is closed |

A condition that is missing has not been observed yet; `unknown` means nobody could read it, which
is different from `failed`.

**A closed market is not a fault.** When every instrument the terminal is judged on is outside its
broker's trading sessions, `TicksFresh` stays `ok` and its `reason` reads `market_closed`: no tick
is expected until the market opens, and the terminal is healthy. An `ok` with no reason means ticks
are arriving. So at a weekend, read the reason before you conclude that prices are flowing.

**A slow or quiet broker is said to be the broker's.** A tick older than 15 seconds makes
`TicksFresh` `failed`, whoever caused it: a stale price is a stale price, and the terminal's health
is degraded. When your terminal is running and logged in and the only thing wrong is the broker's
own feed, the condition says so in its `reason` and in `attributed_to`:

| `reason` | What it means |
|---|---|
| `slow_feed` | this feed's own pace is slower than 15 seconds. Some feeds are simply slow: a crypto symbol at a weekend may quote less than once a minute. It reads `slow_feed` for as long as that is so, not once per gap |
| `broker_quiet` | the last tick is well past the feed's own usual pace |

Both carry `attributed_to: "broker"`. MFX times each feed against its own measured pace, so a feed
that is always slow is not reported as broken and one that stops is. Any other `failed` reason has
`attributed_to: null`: it is not attributed, and you can ask us about it.

The `terminal.condition_changed` event carries the same `attributed_to`. It is sent when a
condition's status changes, and also when a slow feed becomes a quiet broker or the reverse. A slow
feed is one event when it becomes slow and one when its pace is back, not one for every late tick.

## Run it

```
python samples/terminal_lifecycle.py all --terminal <terminal id> --key-id mfxk_... --private-key-file <your key file> --instruments EURUSD,XAUUSD
```

Your key needs the scopes `term:read` and `term:write`. Logins and passwords are typed at a prompt
and go nowhere else. The script prints every change of state and condition with the seconds since
it started, and ends with `PASS` or says what it was still waiting for.
