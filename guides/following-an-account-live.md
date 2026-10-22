# Following an account live

Your users want to see their balance, equity, margin and every open position's floating profit move
as the market moves. MFX pushes that to you over the stream. This guide explains what arrives, how to
keep your copy correct, and how to do it for thousands of users without wasting bandwidth.

## Two kinds of thing arrive

| Kind | Examples | Channel | After a reconnect |
|---|---|---|---|
| **The account as it is now** | balance, equity, margin, free margin, margin level, the open positions with their current price and floating profit | `account:{terminal_id}`, one per terminal | you get the current state again |
| **Things that happened** | a position opened, modified or closed; the account's profile changed | `events` | missed ones are replayed for up to 15 minutes |
| **Your orders' outcomes** | filled, rejected, expired | `results:{terminal_id}` | missed ones are replayed for up to 15 minutes |

The account is **state**: only the latest version matters, so MFX never replays old versions to you.
Events and order outcomes are **history**: none may be lost, so MFX replays what you missed. Keep the two
apart in your own code too.

## Subscribing to an account

Open the stream, then subscribe to `account:{terminal_id}` for each terminal you want to follow. Your
key needs the scope `acct:read`, and the terminal must be yours.

You receive two kinds of message on that channel:

| Message | When | What it carries |
|---|---|---|
| `account.snapshot` | as soon as you subscribe; again whenever a position opens, closes or changes (stop loss, take profit, volume); and every 60 seconds | everything: the account figures, every open position in full, the pending orders, and `as_of` |
| `account.marks` | in between, whenever prices move, at the terminal's refresh rate; every 5 seconds even if nothing moved | the account figures, and for each position whose price or profit moved: its ticket, current price, floating profit and swap |

A snapshot is the whole picture. A marks message is a small update to the snapshot you already hold.
For an account with a few positions, a marks message is a few hundred bytes; for an account with
hundreds of positions, it is still a small fraction of a snapshot.

Every message carries a `seq`, which increases on that channel, and a `base`: the `seq` of the snapshot
it applies to. A snapshot's `base` is its own `seq`.

Amounts are **decimal strings** (`"10012.40"`), never floating-point numbers. Parse them as decimals.

## Keeping your copy correct

Four rules. The SDK's live account view follows them for you; if you use the raw stream, follow them
yourself.

1. **Replace on a snapshot.** Throw away your copy and take the snapshot as it is.
2. **Apply marks only to the snapshot they name.** If a marks message's `base` is the `seq` of the
   snapshot you hold, update the account figures and, for each ticket in it, the position's current
   price, floating profit and swap. If its `base` is different, or it names a ticket you do not have,
   your copy is behind: ignore it. The next snapshot arrives within 60 seconds, or ask for one now with
   `resume`.
3. **On a gap in `seq`, send `resume`.** On this channel the answer is a fresh snapshot, never a replay
   of old messages.
4. **Use the events for what happened, the snapshot for what is.** Show a notification when
   `position.opened` or `position.closed` arrives on `events`; draw the list of positions from the
   snapshot and its marks.

If nothing at all arrives on an account channel for 15 seconds while the account has open positions,
treat the numbers as stale and say so. `GET /v1/terminals/{id}/account` answers the same picture with a
`stale` flag.

## Doing it for thousands of users

These practices matter most when one platform serves many users, each with their own terminal.

- **Keep a channel subscribed for as long as you need it.** An account channel may stay subscribed
  around the clock: that is the supported way to watch equity for a risk rule, such as a daily-loss
  limit, while nobody is looking. Channels ride your connections, and your plan limits how many
  connections you hold (`ws_conns` in `GET /v1/usage`), so carry many accounts on a few connections.
  Where nothing needs an account's live figures, such as a screen nobody has open, unsubscribe: fewer
  channels mean fewer messages for you to process.
- **Use a few connections, not one per user.** One stream connection carries many channels. Multiplex
  your users' subscriptions over a small pool of connections from your servers, and fan the updates out
  to your users yourself.
- **Do not poll.** Everything on the account arrives on the stream. Read the REST endpoints for a page
  that is not live, such as an account statement, or to check one value; never call them in a loop.
- **Choose each terminal's refresh rate on purpose.** It sets how often prices are swept and how often
  the account's marks can arrive. One second is plenty for a display. A faster rate means more messages
  and uses more of your plan's capacity, so keep it for users who need it.
- **Keep marks cheap on your side.** A marks message is designed to be applied in place: look up the
  ticket, set three numbers. Avoid rebuilding a whole page or writing every message to a database.
- **Store what you need from events, not from marks.** If you keep history, write `position.*` events
  and your order outcomes. The marks stream is a live view, not a record.

## Reconnecting

The stream connection has a maximum lifetime of 24 hours, and networks drop. After reconnecting:

- for each account channel you had, subscribe again (or `resume`): you get the current snapshot;
- for `events` and `results:{terminal_id}`, `resume` with the last `seq` you saw: you get what you missed
  from the last 15 minutes, or `resync` if it was longer, in which case read the current state over REST.

## Run it

The sample follows one terminal's account live, applies marks to the snapshot it holds, and prints
every update with the time since it started:

```
python samples/account_live.py --key-id mfxk_... --private-key-file <your key file> --terminal <terminal id>
```

Your key needs the scope `acct:read`.
