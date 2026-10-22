# Your first terminal

A terminal is one trading terminal session, compatible with MetaTrader 5, that MFX Hub runs for you: it logs in to your broker with your
credential, and everything else — quotes, account state, orders — happens through it. This guide
takes you from an API key to a terminal that is `ACTIVE`, and then retires it.

## Before you start

* An API key: a **key id** and the **private key** shown once when the key was issued. MFX Hub keeps
  only the public half, so a lost private key is rotated, never recovered.
* Your **broker's name** (for example `Pepperstone`) and your **server's name**, exactly as it
  appears in your trading platform's login window (for example `Pepperstone-Demo`).
* Your broker **login** and **password**. They travel once, over TLS, straight into the platform's
  vault; no API ever returns them.

## Finding your ids

The API takes ids, not names. The sample looks them up for you, in three calls:

* **Farm** - where your terminals run. `client.farms.list()` lists the farms your plan allows and
  what each offers (`order_execution`, `quote_streaming`, `account_read`).
* **Broker** - `client.catalog.all_brokers()` lists every broker MFX Hub supports, with its `code`
  and `display_name`.
* **Server** - `client.catalog.servers(broker_id)` returns one page of that broker's servers; each
  has a `server_name` and says whether it is `enabled`.

## The five steps

1. **Create a client.** Every request is signed with your private key (Ed25519); you never write the
   headers yourself.
2. **Request the terminal.** The answer is immediate and the state is `REQUESTED`: placing it on a
   cell is asynchronous. An `idempotency_key` is required — retry with the same key and you get the
   same terminal back, never a second one.
3. **Submit the credential.** It is stored by reference; the terminal can log in from now on.
4. **Wait for `ACTIVE`.** It passes through `PROVISIONING` (a cell is preparing it) and `LOGGING_IN`
   (the broker is answering). `conditions` say why if it stalls — never guess from the state alone.
5. **Retire it** when you are done. It drains (`DRAINING`) and then it is `RETIRED`.

The whole flow is one runnable file: [`samples/first_terminal.py`](../samples/first_terminal.py).

## What each state means to you

| State | What is happening | What you do |
|---|---|---|
| `REQUESTED` | Accepted; waiting for a cell with room | Nothing |
| `PROVISIONING` | A cell is preparing the terminal | Submit the credential if you have not |
| `LOGGING_IN` | The terminal is logging in to your broker | Nothing; read `conditions` if it takes long |
| `ACTIVE` | Logged in and healthy | Use it |
| `OFFLINE` | The cell running your terminal is down | Nothing — MFX brings it back or moves it |
| `DRAINING` | Stopping, at your request or for maintenance | Nothing |
| `RETIRED` | Gone; its slot is free | Request a new one if you need it |
| `FAILED` | The broker refused the login, or it did not finish in time | Read `conditions`, then [submit the credential again](changing-a-terminals-password.md) |

## Two things that look wrong and are not

* **A health check reads `failed` while the terminal is `LOGGING_IN`.** `TicksFresh` is checked
  from the first moment, before the broker has sent a live tick, so it can read `failed` for a few
  seconds on the way to `ACTIVE`. Judge the terminal by its state; read `conditions` only if it
  stays out of `ACTIVE`.
* **Times from the broker are ahead of, or behind, your clock.** Fields named `broker_time`, and the
  times on positions and deals, are on your broker's own clock, which is usually not UTC. Times
  named `*_at` and `tick_time` are UTC.

Next: [your first order](your-first-order.md).
