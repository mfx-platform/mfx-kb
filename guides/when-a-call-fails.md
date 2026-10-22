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
| `RateLimitError` (`rate_limited`, `cooldown`, ...) | 429 | Too many, too soon | Wait `retry_after_s`. Reads are retried for you |
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
