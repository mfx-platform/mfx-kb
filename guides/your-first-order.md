# Your first order

This guide goes from nothing to a quote and a filled order, then closes the position and retires
the terminal: the shortest path through everything a trading program does. Use a **demo** account.
The order is real on whatever account you log in to.

The whole flow is one runnable file: [`samples/first_order.py`](../samples/first_order.py). Without
`--confirm` it stops after the quote and places nothing.

## Before you start

The same as [your first terminal](your-first-terminal.md): an API key, your broker's name, your
server's name, and your login and password. The sample finds the farm, broker and server ids from
those names, and takes a farm that offers `order_execution`.

## The steps

1. **Find your ids and ask for a terminal**, as in [your first terminal](your-first-terminal.md),
   naming the symbol you will trade in `instruments` (the broker's own name for it, such as
   `EURUSD`).
2. **Wait for `ACTIVE`.** `mfx.is_usable(terminal)` is true only then.
3. **Read a quote.** `client.catalog.quote(broker_id, symbol, server_id=...)` answers from your
   terminal's latest tick, with `age_s`, how old it is. No streaming farm is needed for this; that
   is for continuous prices on the stream. `state` says whether the market can be traded; `unknown`
   in the first minutes after a terminal goes live means its trading conditions have not been
   reported yet.
4. **Place the order.** `client.execution.place_order(...)` with `side="buy"` and the smallest
   volume the symbol allows (`client.catalog.symbol(...).volume_min`), as a decimal string. Every
   write takes an `idempotency_key`: send the same key again and you get the same order back, never
   a second one.
5. **Wait for the outcome.** Execution never raises: `client.execution.wait_for(command_id)` returns
   an `ExecutionResult` once it is final. Read `state` first: `SUCCEEDED` filled; `FAILED`,
   `EXPIRED` and `REJECTED` did not happen, and `reason` says why; `UNKNOWN` may already be a
   position, so never send it again under a new key. `exec_price`, `exec_volume` and
   `comment_raw` (your broker's own words) describe the fill, and `tickets.position` is the
   position it opened.
6. **Close the position** with `client.execution.close_position(terminal_id, ticket, ...)`, and
   wait for that result the same way.
7. **Retire the terminal.**

## What you will see

```text
farm MFX DEV, broker Pepperstone, server Pepperstone-Demo
REQUESTED
LOGGING_IN
terminal 06ada7be-1978-779d-8000-dc3528b97c84 is ACTIVE
EURUSD bid 1.12129 ask 1.12181, 51.2 s old
order SUCCEEDED: 0.01 at 1.12192 (Request executed)
close SUCCEEDED at 1.12128000
terminal retired
```

About half a minute end to end on the development environment. Prices, ids and timings are an
example run, not figures to expect.

## Where to go next

* [Sizing a position](sizing-a-position.md): from the money you will risk to a volume.
* [When a call fails](when-a-call-fails.md): what each error and each `reason` means.
* The [execution reference](https://docs.mfxhub.com/sdk-execution): every order method and every
  field of `ExecutionResult`.
