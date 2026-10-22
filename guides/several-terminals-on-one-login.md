# Several terminals on one broker login

> Runs against the dev environment today, where adding a second terminal to a login was run on
> 3 October 2026. The sample it quotes is
> [`../samples/several_terminals_one_login.py`](../samples/several_terminals_one_login.py).

You can run more than one terminal on the same broker login, on every plan. A common reason: one
terminal runs an EA on crypto, another streams and trades everything else, and you want both live
at once.

Every terminal can do everything: trade, run an EA, stream its own instruments. There is no
"main" terminal and no read-only copy.

## They share the account

The terminals are separate sessions of **one account at the broker**. Each of them sees the same
balance, the same positions, the same orders and the same history. That has two consequences worth
planning for:

- **An order sent from one terminal is visible from all of them.** MFX Hub never sends one order
  twice. If two of your terminals each send the same order, the broker receives two orders.
- **On a netting account, two strategies on one symbol net into one position.** A buy from one
  terminal and a sell from another reduce each other. If each strategy needs its own position, use
  a hedging account or separate accounts.

## They share a symbol's prices

Prices for a symbol on a broker's server are **one stream** for all your terminals there. If two
of your terminals watch the same symbol, a subscriber to that symbol's price channel gets each tick
once, whichever terminal read it first, not once for each terminal.

Ticks are samples. A tick older than one already sent for the symbol is not sent, so prices only
move forward in the broker's time; and if the platform is ever slow, late ticks are dropped and the
latest price still arrives. Read the latest price as the truth, and do not count ticks.

## Add one

Request a terminal as you did the first one, then give it the same login. From the sample:

```python
return await client.terminals.create(
    farm_id=field(like, "farm")["id"],
    broker_id=field(like, "broker")["id"],
    server_id=field(like, "server")["id"],
    streaming=bool(field(like, "streaming", False)),
    idempotency_key=f"same-login-{uuid.uuid4()}",
)
```

```python
return await client.terminals.submit_credentials(
    terminal_id,
    login=login,
    password=password,
    server=server,
    idempotency_key=f"same-login-credential-{uuid.uuid4()}",
)
```

Each terminal holds **its own copy** of the credential. MFX Hub never copies a password from one of
your terminals to another.

## How many

Two limits apply, and the refusal tells you which one you met. Both are checked when you submit
the credential, and a refusal stores nothing.

| Limit | Counts | The refusal |
|---|---|---|
| Your plan's terminals per login | your own terminals on the login that are not `RETIRED` or `FAILED` | **409 `quota_exceeded`**, `details.reason` is `login_terminal_limit`, with `details.quota` and `details.in_use` |
| The broker's sessions per login | every session on the login | **409 `placement_error`**, `details.reason` is `login_session_limit` |

The broker's limit is the broker's own rule, and it counts every session on the login, including
ones MFX Hub cannot see, such as a desktop terminal you are logged in to yourself. That is why the
second refusal carries no numbers.

From the sample:

```python
reason = refused.details.get("reason")
```

## The first time a server holds one more session

Brokers do not publish how many sessions a login may hold, so MFX Hub learns it per broker server
by watching. When the terminal you add is one more session than MFX Hub has seen that server hold,
its first minutes are watched:

- **The broker holds them all.** Nothing more happens. This is the usual case.
- **The broker drops one of your other sessions to make room.** MFX Hub stops the terminal you just
  added, so the earlier one reconnects. The new terminal reads `DRAINING` and then `RETIRED`, with
  the reason `the broker allows N sessions on this login`. From then on, one terminal too many is
  refused when you submit its credential, before anything is started.

Only one such check runs on a broker server at a time. While it runs, another request for one more
session on that server answers **409 `placement_error`** with `details.retry_after_s`. Wait that
long and submit again.

## How many of yours share a login

Every terminal you read carries `same_login_terminals`: how many of **your** terminals that are not
`RETIRED` or `FAILED` use its login, this one included. It is empty until the terminal has a
credential.

```python
print(f"{terminal_id} {seen.state}; yours on its login: "
      f"{field(seen, 'same_login_terminals')}")  # fmt: skip
```

## Changing the password

Because each terminal holds its own copy, a password you change at the broker has to be submitted
to **each** terminal on the login. The answer to a change tells you how many are left:
`same_login_terminals_stale` is the number of your other terminals on the login that were last
given their password before this one. Work through them until it reads 0. See
[Changing a terminal's password](changing-a-terminals-password.md).

## Retiring one

Retiring a terminal ends its session and nothing else. The other terminals on the login keep
running, and their `same_login_terminals` drops by one.

## Run it

```sh
python samples/several_terminals_one_login.py <terminal id> --key-id mfxk_... --private-key-file <your key file>
```

`<terminal id>` is a terminal of yours that already uses the login. Your key needs the scopes
`term:read` and `term:write`. The login, the server and the password are typed at a prompt and go
nowhere else. The script prints each change of state and ends with `PASS` when the new terminal is
`ACTIVE`.
