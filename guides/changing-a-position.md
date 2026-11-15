---
title: "Changing a position"
topic: trading
order: 20
summary: "Protect an open position with a stop loss and a take profit, close part of it and let a trailing stop follow the price."
---

# Changing a position

Once a position is open you can protect it, take part of it off, and let the platform follow the
price for you. This guide opens a small position, sets a stop loss and a take profit, closes half,
puts a trailing stop on the rest, takes the trailing stop off again and closes the position. Use a
**demo** account. The orders are real on whatever account you log in to.

The whole flow is one runnable file: [`samples/position_changes.py`](../samples/position_changes.py).
Without `--confirm` it stops after the quote and opens nothing. Whatever happens, it closes the
position it opened and retires its terminal before it ends.

## Before you start

The same as [your first order](your-first-order.md): an API key, your broker's and server's names,
and a login and password for a demo account.

## The steps

1. **A terminal, `ACTIVE`, a quote and the symbol's rules**, as in
   [pending orders](pending-orders.md).
2. **Buy twice the smallest volume**, so that half of it can be closed on its own. The position's
   ticket is `tickets.position` on the result.
3. **Set a stop loss and a take profit** with
   `client.execution.modify_position(terminal_id, ticket, sl=..., tp=...)`. The sample puts each
   2 % from the fill price, on the symbol's tick. A level too close to the price is refused with a
   `reason` that says so. Your broker sets the minimum distance (`stops_level` on the symbol).
4. **Close half** with `client.execution.close_position_partial(terminal_id, ticket, volume=...)`.
   The rest stays open under the same ticket. `client.accounts.positions(terminal_id)` shows its
   new `volume` once the account's next snapshot arrives, usually within seconds.
5. **Add a trailing stop** with `client.execution.set_trailing_stop(terminal_id, ticket,
   distance=...)`. The platform moves the stop loss up behind the best price the position reaches,
   never down. The position then carries `trailing_stop`, with its `distance` and `state`. Take it
   off with `clear_trailing_stop`. The stop loss stays where the trail left it.
6. **Close what is left** with `client.execution.close_position(terminal_id, ticket)`, and read the
   positions until the ticket is gone.
7. **Retire the terminal**, and wait until it reads `RETIRED` before you start another terminal
   on the same login (see [a terminal's whole life](a-terminals-whole-life.md#retiring-is-an-intent-not-a-deletion)).

Every change is a command. Wait for each with `client.execution.wait_for(command_id)` and read
`state`: `SUCCEEDED` happened, `FAILED`, `EXPIRED` and `REJECTED` did not, and `UNKNOWN` may have,
so never send it again under a new key.

## What you will see

```text
farm MFX DEV, broker Pepperstone, server Pepperstone-Demo
REQUESTED
LOGGING_IN
terminal 06ada7be-1978-779d-8000-dc3528b97c84 is ACTIVE
EURUSD bid 1.12129 ask 1.12181, 1.2 s old
buy 0.02: SUCCEEDED
position 80123456 opened at 1.12183
stop loss 1.09939, take profit 1.14426: SUCCEEDED
close 0.01: SUCCEEDED
position 80123456 now holds 0.01
trailing stop 0.02243: SUCCEEDED
trailing stop 0.02243, active
trailing stop removed: SUCCEEDED
close: SUCCEEDED
position 80123456 is closed
terminal retired
```

This shows the shape of a run. Ids, tickets, prices and timings are illustrations, not figures to
expect.

## Where to go next

* [Sizing a position](sizing-a-position.md): from the money you will risk to a volume and a stop.
* [Following an account live](following-an-account-live.md): see each change arrive as it happens.
* The [execution reference](https://docs.mfxhub.com/sdk-execution): every position command and
  field.
