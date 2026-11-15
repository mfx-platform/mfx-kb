---
title: "Pending orders"
topic: trading
order: 10
summary: "Place a limit or stop order, find it, move its price and cancel it."
---

# Pending orders

A pending order waits at your broker until the price reaches it: a **limit** buys below or sells
above the market, a **stop** buys above or sells below it. This guide places one, finds it in your
pending orders, moves its price, cancels it and checks it is gone. Use a **demo** account. The order
is real on whatever account you log in to.

The whole flow is one runnable file: [`samples/pending_orders.py`](../samples/pending_orders.py).
Without `--confirm` it stops after the quote and places nothing. Whatever happens, it cancels the
order it placed and retires its terminal before it ends.

## Before you start

The same as [your first order](your-first-order.md): an API key, your broker's and server's names,
and a login and password for a demo account. The sample takes a farm that offers
`order_execution`.

## The steps

1. **A terminal, `ACTIVE`, and a quote**, exactly as in [your first order](your-first-order.md).
   Read the symbol's rules too: `client.catalog.symbol(broker_id, symbol, server_id=...)` gives
   `volume_min`, `digits` and `tick_size`. A price must sit on the symbol's tick, written with its
   digits, or the broker refuses it.
2. **Place a buy limit below the market.** `client.execution.place_order(...)` with
   `order_type="limit"` and `price`. The sample puts it 5 % below the bid so it does not fill while
   it runs. Wait for the outcome with `client.execution.wait_for(command_id)`. `SUCCEEDED` means the
   broker accepted the order, not that it filled, and `tickets.order` is its ticket.
3. **See it in your pending orders.** `client.accounts.pending_orders(terminal_id)` lists them, each
   with `ticket`, `order_type`, `price`, `volume`, `sl`, `tp`, `expires_at`, `magic` and `comment`.
   The list is a snapshot of the account, so a new order can take a few seconds to appear. Read it
   again rather than treating its absence as a failure.
4. **Move its price** with `client.execution.modify_order(terminal_id, ticket, price=...)`. You can
   also change `sl`, `tp` and `expires_at`. The ticket stays the same.
5. **Cancel it** with `client.execution.cancel_order(terminal_id, ticket, ...)`, wait for the
   outcome, and read the list again until the ticket is gone.
6. **Retire the terminal**, and wait until it reads `RETIRED` before you start another terminal
   on the same login (see [a terminal's whole life](a-terminals-whole-life.md#retiring-is-an-intent-not-a-deletion)).

Every write takes an `idempotency_key`. Send the same key again and you get the same result back,
never a second order. The sample also writes its own mark into the order's `comment`, so that on
the way out it can find and cancel an order of its own even when a step's outcome was `UNKNOWN`.

## When it fills

A second file, [`samples/pending_fill.py`](../samples/pending_fill.py), places a **buy stop** just
above the ask (the broker's `stops_level` plus two points, from `client.catalog.symbol(...)`) and
waits for the broker to fill it. It listens on the stream rather than reading the pending list:

* open `client.stream()` and subscribe to `Channel.EVENTS` **before** placing the order, so the
  fill cannot happen while nobody is listening;
* read each message with `mfx.event_of(message)`. An `OrderClosed` for your ticket with
  `reason == "filled"` names the position in `position_ticket`; a `PositionOpened` names the order
  it came from in `order_ticket`. The two may arrive in either order. Any other `reason`
  (`cancelled`, `expired`, `rejected`) means it closed without filling;
* close the position with `client.execution.close_position(terminal_id, position_ticket, ...)`.

A stop fills only when the price reaches it. If the market does not move up to it within 90 s,
or the last price is more than a minute old (the market is closed), the sample cancels the order
and its last line says `{"outcome": "amber", ...}`: nothing went wrong, nothing filled.

## What you will see

```text
farm MFX DEV, broker Pepperstone, server Pepperstone-Demo
REQUESTED
LOGGING_IN
terminal 06ada7be-1978-779d-8000-dc3528b97c84 is ACTIVE
EURUSD bid 1.12129 ask 1.12181, 1.2 s old
buy limit 70123456 placed at 1.06522
order 70123456 is in your pending orders
modify SUCCEEDED: now at 1.05401
cancel SUCCEEDED
order 70123456 is gone
terminal retired
```

This shows the shape of a run. Ids, tickets, prices and timings are illustrations, not figures to
expect.

## Where to go next

* [Changing a position](changing-a-position.md): stops, a partial close and a trailing stop.
* [When a call fails](when-a-call-fails.md): what each `reason` means, such as a price that is too
  close to the market.
* The [execution reference](https://docs.mfxhub.com/sdk-execution): every order type and field.
