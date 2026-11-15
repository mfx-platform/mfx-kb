---
title: "When a call fails"
topic: developers
order: 10
summary: "Every error has the same shape; this is what each code means and when a retry is safe."
---

# When a call fails

Every failure MFX Hub answers has the same shape, whichever route you called:

```json
{"error": {"code": "upstream_error", "message": "...", "details": {"reason": "terminal_timeout"}}}
```

- **`code`** is what to act on. It names the kind of failure, not the HTTP status.
- **`message`** is for a person.
- **`details`** holds only what you can act on.

The list of codes is **not closed**: a new rule brings a new code. Treat a code you don't know as
an error, by its HTTP status and message, and never fail just because you don't recognise it.

The SDK turns each code into its own exception, all subclasses of `mfx.MfxApiError`, carrying
`code`, `status`, `details` and `retry_after_s`.

## What to do with each kind

| You get | Status | What it means | What to do |
|---|---|---|---|
| `ValidationError` (`validation_error`, `unprocessable`, ...) | 400, 413, 422 | The request can't be taken as sent | Fix the request; `details` says which part |
| `AuthError` | 401 | Not signed by a key MFX Hub knows | Check the key id, the private key and your clock |
| `ForbiddenError` (and `TenantDisabledError`, `TenantSuspendedError`) | 403 | The key may not do this, or the tenant is stopped | Use a key with the scope, or read the message |
| `NotFoundError` | 404 | No such thing. Also the answer for another tenant's id | Check the id |
| `ConflictError`, `StateError`, `PlacementError` | 409 | It exists, or is in a state where this can't be done | Read `details`; often wait for an operation to finish |
| `RateLimitError` (`rate_limited`, `cooldown`, ...) | 429 | Too many, too soon | Wait `retry_after_s`. Reads are retried for you. For `terminal_reads_limit` and `reads_busy` on price history, see [limits on price history](limits-on-price-history.md) |
| `UpstreamError`: `upstream_error` | **503** | Your terminal, the broker or a service did not answer as needed. `reason` says why (`terminal_timeout`, `no_tick`, ...) | Read the terminal's state before trying again |
| `UpstreamError`: `dependency_unavailable` | 503 | A store of MFX Hub's own did not answer | Wait `retry_after_s` and try again |
| `InternalError` | 500 | A fault of MFX Hub's own | Try a read again later; report it if it persists |

## Two rules worth knowing

**MFX Hub never answers 502 or 504.** If you see either, it came from the network between you
and MFX Hub, not from the platform. The SDK still raises it as an `UpstreamError`, with
`from_edge` set to true, and it has no `code` or `reason` of MFX Hub's in it.

**A write is never retried for you.** A write that timed out may already have been done. An
order may already be a position. Look first: read the command by its id, or the account, before
sending it again. The idempotency key you sent makes the same request safe to repeat. A new key
makes a new request.

## A symbol the server does not list

> Runnable: [`samples/unknown_symbol.py`](../samples/unknown_symbol.py) asks for a name no broker
> lists and passes only on this answer.

Ask for a price, history or an order on a name your broker's server does not list, and the call
fails with **`404 not_found`**. It never answers a price of zero or an empty list. `details` says
which form it took:

- `details.nearest`: the platform's catalogue does not list the name. It offers the closest names it
  does list, which may be none.
- `details.reason` `symbol_unknown`: your terminal itself does not know the name.

Names are the broker's own, suffix and all: `EURUSD.sd` is not `EURUSD`. Read them from
`client.catalog.symbols` instead of typing them.

## A market that is closed

> Runnable: [`samples/closed_market.py`](../samples/closed_market.py) finds a symbol whose session
> is closed now and shows the answer you get.

A closed market is not a failed call. What you get depends on whether one of your terminals
watches the symbol:

| The symbol is | A quote answers | What to do |
|---|---|---|
| watched by one of your terminals | `200`, the last price, `state` `closed_session` | Read `state` before you act on a price; the price is old |
| watched by none of them | `503 upstream_error`, `details.reason` `no_tick` | Check the symbol's session; ask again when it opens |
| sent an order | the order's `reason` is `market_closed`, and nothing is placed | Send it when the session is open |

A symbol's `trade_sessions` say when it trades, on the **broker's clock**. A quote's `broker_time`
is its `tick_time` on that clock, which gives you the difference to UTC.
