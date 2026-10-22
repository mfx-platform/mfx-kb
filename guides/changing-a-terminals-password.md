# Changing a terminal's password

> Runs against the dev environment today, where every path below was run on 2 October 2026, and a
> change on a terminal with open positions on 3 October 2026. The
> sample it quotes is [`../samples/change_password.py`](../samples/change_password.py).

You changed your password at the broker, or the one you submitted was wrong and the terminal is
`FAILED`. Either way the fix is the same call you used the first time: **submit the credential
again**. You do not retire the terminal, and it keeps its id, its instruments and its history.

## Submit it again

From the sample:

```python
return await client.terminals.submit_credentials(
    terminal_id,
    login=login,
    password=password,
    server=server,
    idempotency_key=f"change-password-{uuid.uuid4()}",
)
```

```python
takes_effect = field(answer, "takes_effect", "first_login")
```

The first submission for a terminal stores its credential. Every later one **for the same login**
replaces the password. The answer tells you when the terminal will use it:

| `takes_effect` | The terminal was | What happens |
|---|---|---|
| `restart` | `ACTIVE` | It is restarted now and logs in with the new password. Expect a short gap in ticks |
| `retry_login` | `FAILED` | It rejoins the login queue now. This is how you recover from a wrong password |
| `next_login` | anything else | Nothing is started. Its next login uses the new password |
| `first_login` | new, with no credential yet | The ordinary first submission |

`operation_id` names the restart or retry so you can follow it. It is empty for `next_login`.

**Open positions do not hold a password change back.** An `ACTIVE` terminal is restarted even when
the account has open positions, because a restart is the only way the new password is used. Your
positions stay open at the broker while the terminal reconnects; expect a gap of a few seconds in
ticks and account updates.

You can still get `next_login` for an `ACTIVE` or `FAILED` terminal. That means the restart or retry
could not start right now, and `not_applied_reason` says why, using the same code the restart call
would have answered:

| `not_applied_reason` | Means |
|---|---|
| `operation_in_progress` | Another restart, rebuild or move is already running on this terminal |
| `cooldown` | The terminal was restarted moments ago |
| `tenant_rate` or `concurrency` | You have used your restarts for now |
| `positions_open` | Your platform operator has switched on the confirmation for restarts with open positions |

The new password is stored all the same. Restart the terminal when you are ready, or leave it: its
next login uses the new password. `not_applied_reason` is empty in every other answer.

## What is refused

- **A different login.** The call answers **409 `conflict`** with `details.reason` set to
  `login_changed`. Another login is another account, with its own positions and history, so it
  needs its own terminal: request a new one, and retire this one when you are done with it.
- **A terminal that is going.** `DRAINING` and `RETIRED` answer **409 `state_error`**.
- **A different server.** The credential's server must be the terminal's own; anything else is a
  **400**.

## When the broker says no

If the broker refuses the login, the terminal becomes `FAILED` within a couple of minutes and its
`BrokerConnected` condition reads `failed` with the reason `login_refused`. MFX Hub does not keep
retrying a refused login, because repeated bad logins are how a broker locks an account. Nothing
happens until you act:

1. Check the login, the password and the server at your broker.
2. Submit the credential again. The answer is `retry_login` and the terminal logs in.

A login that fails for another reason, such as a broker that did not answer in time, is different:
MFX Hub tries it again by itself after 30 minutes, up to three times in a day. You can still submit
the credential again, or ask for a retry, without waiting.

A broker gives the same answer for a wrong password as for an account that no longer exists, so
MFX Hub only ever tells you that the login was refused. Demo accounts expire; if the password is
right and the login is still refused, check that the account is still open.

## An investor password

Submitting an investor (read-only) password turns order execution off for the terminal, and the
answer says so in `execution_disabled`. Submitting a master password later does not turn it back on
by itself.

## More than one terminal on the login

Each terminal holds its own copy of the credential, so a password you changed at the broker has to
be submitted to every terminal that uses the login. The answer counts what is left:
`same_login_terminals_stale` is how many of your other terminals on the login were last given
their password before this one. Submit to each until it reads 0. See
[Several terminals on one broker login](several-terminals-on-one-login.md).

## Your password is never returned

The password travels once, over TLS, into the platform's vault. No API returns it, no log holds it,
and the answer to this call is a reference and nothing else.

## Run it

```sh
python samples/change_password.py <terminal id> --key-id mfxk_... --private-key-file <your key file>
```

Your key needs the scopes `term:read` and `term:write`. The login, the server and the new password
are typed at a prompt and go nowhere else. The script prints each change of state and ends with
`PASS` when the terminal is `ACTIVE` on the new password.
