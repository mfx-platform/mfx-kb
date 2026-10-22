# Receiving webhooks

> The runnable half of this page is [`samples/webhook_receiver.py`](../samples/webhook_receiver.py).
> `python samples/webhook_receiver.py --self-check` runs it end to end on your own machine.

A webhook is MFX Hub calling **your** HTTPS address for the events you asked for: a position
closing, a terminal changing state, anything on your `events` channel. You don't need an open
stream. Use it when the thing that must react is a server of yours that should not hold a
WebSocket open all day.

## Registering an endpoint

Register it with `client.webhooks.create(url, event_types, idempotency_key=...)`, using a key
with the `hook:write` scope. `event_types` takes:

- exact types, such as `position.closed`;
- a family, such as `terminal.*`;
- `*` for every event.

Ticks and candles are not events and never arrive this way.

**The answer carries the signing secret, and that is the only time you will see it.** Store it
before you do anything else. If it is lost, rotate it with `client.webhooks.rotate_secret`.

MFX Hub only calls HTTPS addresses on the public internet. An address that resolves to a private,
loopback, link-local or metadata address is refused when you register it, and again at every
delivery.

## What arrives

Each event is one `POST` with a JSON body: the event exactly as the `events` channel carries it,
including `id`, its position in your event stream. These headers come with it:

| Header | What it holds |
|---|---|
| `MFX-Event-Id` | the event's id: de-duplicate on this |
| `MFX-Event-Type` | e.g. `position.closed` |
| `MFX-Delivery-Attempt` | `1` for the first try, then a higher number for each retry, or `replay` |
| `MFX-Signature` | `t=<unix seconds>,v1=<hex>`. After a rotation, two `v1` values |

## The three rules for a receiver

1. **Verify the signature over the body as it arrived.** Call `mfx.verify(body, header, secret)`
   on the raw bytes. If you parse the JSON and check a re-serialised copy, the bytes change and the
   check fails. `verify` also refuses a delivery signed more than five minutes from your clock,
   which stops a captured delivery being sent again later. A refused delivery raises
   `WebhookSignatureError`, and its `reason` is `malformed`, `stale` or `mismatch`.
2. **Answer 2xx quickly, then do the work.** Any other answer, or no answer before the timeout,
   counts as a failure, and the event is sent again later.
3. **Expect the same event more than once, and in any order.** Delivery is at least once. An
   event that failed is retried on its own while later ones go ahead, so a retry can arrive after
   newer events. De-duplicate on `MFX-Event-Id`. Where order matters to you, order by it.

## Rotating the secret

`client.webhooks.rotate_secret` returns a new secret, also shown only once. For one day after a
rotation every delivery is signed with both secrets, the new one first, so you can switch over
without losing anything. Pass both to `verify` until `previous_secret_until`.

## When deliveries fail

- `client.webhooks.failures(endpoint_id, state="dead")` lists each event that could not be
  delivered, with every attempt made on it.
- `client.webhooks.replay(endpoint_id, failure_id=...)` sends one of those events again.
  `replay(endpoint_id, after=<an MFX-Event-Id>)` sends every event after that point again.
- An endpoint that has delivered nothing for a long time is paused, and your stream says so with a
  `webhook.endpoint_paused` event. Resume it with `client.webhooks.update(endpoint_id, paused=False)`.

Ask for a test event at any time with `client.webhooks.test(endpoint_id)`. It is delivered and
signed like any other.
