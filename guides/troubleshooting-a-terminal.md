---
title: "Troubleshooting a terminal"
topic: accounts
order: 40
summary: "Three plain reads, in order, that tell you why a terminal will not log in, stops trading or is FAILED."
---

# Troubleshooting a terminal

When a terminal will not log in, stops trading or says it is `FAILED`, three reads tell you why. Make
them in this order. Each one is a plain read and changes nothing.

The whole check is one runnable file:
[`samples/troubleshoot_terminal.py`](../samples/troubleshoot_terminal.py).

## 1. The terminal: its state and its conditions

`client.terminals.get(terminal_id)` answers with:

- **`state`**: where the terminal is in its life. [A terminal's whole life](a-terminals-whole-life.md)
  says what each one means and what to do in it.
- **`server_kind`**: `demo` or `live`, as the broker's server reports itself, or null until the
  terminal has connected once. A demo login on a live server, or the other way round, is a common
  reason a broker refuses a login.
- **`conditions`**: one entry for each check the platform runs (the broker connection, fresh
  ticks, the login). One that is not `ok` gives its `reason`.

When the broker refused the login, that condition also carries:

- **`broker_words`**: the broker's own sentence, as MetaTrader showed it, up to 120 characters.
  Show it to whoever owns the account: it is the broker speaking, not MFX Hub.
- **`refusal_kind`**: the same refusal sorted for your code: `invalid_account` when the broker
  says the login or password is wrong, `other` for everything else it said (an account disabled
  or expired, trading not allowed). Read `broker_words` for the detail.

A refused login is not tried again by itself with the same password. Fix what the broker named,
then [change the terminal's password](changing-a-terminals-password.md) or retire it.

## 2. The journal: MetaTrader's own log

`client.terminals.journal(terminal_id, lines=20)` returns the terminal's MetaTrader journal, newest
line first, up to 200 lines.

- While the terminal runs, the lines are read from it now and `from` is `live`.
- After it stops, fails or is retired, the platform keeps its last lines for **24 hours**. `from`
  is then `last_words` and `stopped` says why it stopped.
- A terminal that is not running and kept no lines answers `409 state_error` with
  `details.reason` `not_running`. That is an answer, not a fault.

Account numbers are masked and file paths removed before you see a line. Expert advisor logs are
not part of the journal. Every read is recorded against your key.

## 3. Maintenance: is the platform working on it?

`client.maintenance.list()` goes through the planned windows that reach your terminals: scheduled ones
and any in progress, soonest first. Each says when it starts and ends, how many of your terminals
are in it, and what it `suspends`. While a window runs, a suspended call is refused with
`403 maintenance` (the SDK's `MaintenanceError`) until the window's `ends_at`. Nothing is wrong
with your key or your request; try again after the window.

You don't have to poll for this. Each time a window that reaches you is announced, starts, has
its expected end moved, ends or is cancelled, your event stream (and your webhooks) carries one
`maintenance.window_changed` event. Its fields are the window as `client.maintenance.list()`
shows it, and `change` says what moved: `announced`, `started`, `eta_moved`, `ended` or
`cancelled`. See [receiving webhooks](receiving-webhooks.md).

## 4. Still stuck?

Write to support@mfxhub.com with the terminal's id and the time you saw the problem, in UTC.
The journal lines and the condition you read above are what support will look at first.
