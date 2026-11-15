---
title: "Protecting an account with rules"
topic: risk
order: 20
summary: "Write your own loss, drawdown, exposure and schedule rules once, and MFX Hub checks every entry against them."
---

# Protecting an account with rules

> The runnable half of this page is [`samples/risk_rules.py`](../samples/risk_rules.py), which
> type-checks against the SDK as it shipped (`mfx-sdk` 0.16.0 or later).

If you take one thing from this page: **the rules are yours, and MFX Hub runs them on the
account's own numbers.** A daily loss limit, a drawdown limit, an exposure cap, a trading
schedule: you write them, MFX Hub checks every new entry against them before it reaches your
broker, watches the account between entries, and tells you what it did and why. It recommends none
of them.

## Write the rules once, as a policy

A policy is a versioned rule set. The sample's has two rules, a daily loss limit of 5% of the day's
starting balance and a trailing drawdown of 10% from the highest equity. Each rule has a level at
which you are told (`warn_at`) and one at which it acts (`act_at`), and the actions it may take:

- `notify` tells you, as a `risk.rule_warning` or `risk.rule_breached` event;
- `block_entries` refuses new entries until the rule clears;
- `cancel_pending` and `close_positions` act on orders and positions, in the scope you choose:
  only what MFX Hub placed, or everything on the account.

Every figure is a decimal string: `"5"`, never `5.0`. The SDK refuses a `float` before anything is
sent, for the reason [sizing a position](sizing-a-position.md) explains.

`create_policy` makes version 1, and `activate` puts it in force on one account or many, with one
outcome for each. A later version that **relaxes** protection (a higher limit, an action removed)
needs a key with `risk:override` and a reason. Without them it is refused with
`RiskOverrideRequiredError`, and nothing changes.

## Read what MFX Hub sees

`client.risk.account(account_id)` gives the account's figures as MFX Hub runs them: the daily
loss used of its limit, when the day resets, the drawdown used, the exposure. It also gives the
account's **coverage**:

| Coverage | Says |
|---|---|
| `monitoring` | whether the rules were evaluated on a fresh read of the account |
| `pre_trade` | whether the next entry can be checked: inputs fresh, the symbol's specification known |
| `broker_held` | whether each stop you asked the broker to hold is at the broker |
| `managed_actions` | whether the steps MFX Hub holds for you can run |

**A green terminal is not full coverage.** Read the reason beside any kind that is not healthy.
`client.risk.coverage()` lists every account where something is not.

## A refused entry says why

An order a rule would refuse never reaches the broker. Through the SDK it raises
`RiskRefusedError`, whose `refusal` names the reason, the rule, the policy (or challenge) and its
version, and three figures as text: what was used, the limit, and what the entry would have made
it. Send a smaller entry, add a stop, or wait for the window to open.

You can ask first. `client.risk.check(account_id, order)` judges an order already sized exactly as
the pre-trade check would, and `client.risk.preview(...)` sizes one for you from a risk budget
(0.5% of equity in the sample) and checks every rule. Neither sends anything to your broker.

Closes, partial closes and cancels are never refused: a rule never stands between you and getting
out.

## An exit plan on a position

An exit plan is a set of steps on one position: at 1R close half, then move the stop to the entry,
then trail. Each step is **held by your broker** (a stop or target on the position, which stands
even when MFX Hub cannot reach the terminal) or **held by MFX Hub** (judged on every read, and sent
when it fires). The sample's plan closes half at 1R, held by MFX Hub, and then moves the stop,
held by your broker.

A position has one manager. A second plan on the same position is refused unless it asks to
replace the first.

## A challenge: one rule set over many accounts

A challenge is the firm's side of an evaluation programme: a daily loss and a drawdown limit, a
profit target, a minimum number of trading days, and a period. Each enrolled account passes
(target and days both met), fails (a loss rule breached) or ends (the period closes first), once,
and never changes again. A failed account can block its own entries until you clear it.

`challenge_accounts` lists the programme's accounts with their figures, sorted by the one you name
(daily loss used, drawdown, profit, trading days, period end), and its first page carries the
counts of the whole list. That list, page by page, is your export.

## Try it

```
python samples/risk_rules.py --account <account id> --broker-id <broker id> --symbol EURUSD
```

That reads the account's rules and coverage and sizes a trade, and changes nothing. Add
`--protect` to write and activate the policy, `--plan <ticket>` to put an exit plan on an open
position, and `--challenge` to run a challenge on the account. Use a demo account while you try
it.
