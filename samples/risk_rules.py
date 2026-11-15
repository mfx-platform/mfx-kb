"""Protect an account with rules: a daily loss limit and a drawdown limit, put in force, read back.

Read an account's rules as MFX Hub runs them - its figures, its coverage - and size a trade
against them with the planner, which sends nothing to your broker (needs `risk:read`)::

    MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \\
    python samples/risk_rules.py --account <account id> --broker-id <broker id> --symbol EURUSD

Write a policy with a 5% daily loss limit and a 10% drawdown limit, put it in force on the account,
and check an order that is far too large for it (needs `risk:write`)::

    python samples/risk_rules.py --account <account id> --broker-id <broker id> --symbol EURUSD --protect

Add an exit plan to an open position - at 1R, close half and move the stop to the entry::

    python samples/risk_rules.py --account <account id> --broker-id <broker id> --symbol EURUSD --plan <position ticket>

Run a challenge - one rule set over many accounts, each passing or failing on its own::

    python samples/risk_rules.py --account <account id> --broker-id <broker id> --symbol EURUSD --challenge

Every figure is a decimal string. Without Risk Management in your plan the sample says so and
exits cleanly, as it does when your SDK is older than the one that has `client.risk`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from decimal import Decimal

from mfx import ForbiddenError, MfxClient, SigningKey

BASE_URL = os.environ.get("MFX_BASE_URL", "https://api.dev.mfxhub.com/v1")

#: Your rules, never MFX Hub's: these are an example, not a recommendation.
RULES = {
    "daily_loss": {
        "limit": {"pct": "5"},  # 5% of the day's starting balance
        "basis": "start_balance",
        "warn_at": {"pct": "4"},  # tell me at 4%
        "act_at": {"pct": "5"},
        "check_entries": True,  # refuse an entry that would pass the limit
        "actions": {"kinds": ["notify", "block_entries"]},
    },
    "drawdown": {
        "kind": "trailing",  # from the highest equity, which never goes down
        "limit": {"pct": "10"},
        "initial": "0",
        "peak_basis": "equity",
        "peak_when": "intraday",
        "warn_at": {"pct": "8"},
        "act_at": {"pct": "10"},
        "actions": {"kinds": ["notify", "block_entries"]},
    },
}


def client() -> MfxClient:
    return MfxClient(
        BASE_URL, os.environ["MFX_KEY_ID"], SigningKey(os.environ["MFX_PRIVATE_KEY"])
    )


async def read(
    mfx: MfxClient, account_id: str, broker_id: str, symbol: str, distance: Decimal
) -> None:
    """The account's figures and coverage, and a trade sized from a 0.5% budget."""
    account = await mfx.risk.account(account_id)
    print(f"account {account.account_id}: activation {account.activation_state}")
    if account.daily_loss is not None:
        loss = account.daily_loss
        print(
            f"  daily loss used {loss.used} of {loss.limit}; the day resets {loss.next_reset_at}"
        )
    if account.drawdown is not None:
        print(f"  drawdown used {account.drawdown.used} of {account.drawdown.limit}")
    coverage = account.coverage
    print(
        f"  monitoring {coverage.monitoring.state}, pre-trade {coverage.pre_trade.state}"
    )
    for kind in (coverage.monitoring, coverage.pre_trade):
        if str(kind.state) != "healthy" and kind.reason:
            print(f"    why: {kind.reason}")  # a green terminal is not full coverage

    quote = await mfx.catalog.quote(broker_id, symbol)
    stop = (
        Decimal(str(quote.bid)) - distance
    )  # a buy's stop, under the bid; Decimal, never float
    preview = await mfx.risk.preview(
        {
            "account_id": account_id,
            "symbol": symbol,
            "side": "buy",
            "entry_type": "market",
            "sl": format(stop, "f"),
            "budget": {"kind": "pct_of_equity", "value": "0.5"},
        }
    )
    print(f"  a 0.5% risk buys {preview.volume} lots; verdict {preview.verdict}")
    for check in preview.checks:
        print(f"    {check.rule}: {check.verdict}")


async def protect(mfx: MfxClient, account_id: str) -> None:
    """Write the rules, put them in force on the account, and check an order too large for them."""
    key = uuid.uuid4().hex
    policy = await mfx.risk.create_policy(
        "Funded account",
        RULES,
        reset={"zone": "broker"},
        idempotency_key=f"policy-{key}",
    )
    print(f"policy {policy.id} version {policy.current_version.version}")
    result = await mfx.risk.activate(
        policy.id, [account_id], version=1, idempotency_key=f"activate-{key}"
    )
    for outcome in result.results:
        print(f"  {outcome.account_id}: {outcome.outcome} {outcome.reason or ''}")
    checked = await mfx.risk.check(
        account_id,
        {"symbol": "EURUSD", "side": "buy", "type": "market", "volume": "100"},
    )
    print(
        f"  100 lots: {checked.verdict} {checked.reason or ''}"
    )  # refused by a rule, with why


async def plan(mfx: MfxClient, account_id: str, ticket: str) -> None:
    """At 1R close half the position, and move its stop to the entry."""
    made = await mfx.risk.create_plan(
        {
            "account_id": account_id,
            "ticket": ticket,
            "steps": [
                {"trigger": {"kind": "r", "value": "1"},
                 "action": {"kind": "close_part", "pct": "50"}, "held_by": "mfx"},
                {"trigger": {"kind": "after_step", "step": 1},
                 "action": {"kind": "move_stop", "sl": "entry"}, "held_by": "broker"},
            ],
        },
        idempotency_key=f"plan-{ticket}",
    )  # fmt: skip
    print(f"plan {made.id} is {made.state}; 1R is {made.one_r}")
    for step in made.steps:
        print(f"  step {step.n}: {step.state}, held by {step.held_by}")


async def challenge(mfx: MfxClient, account_id: str) -> None:
    """A two-phase programme's first phase: 8% profit in 30 days, 5% a day, 10% overall."""
    key = uuid.uuid4().hex
    made = await mfx.risk.create_challenge(
        "Evaluation, phase 1",
        {
            "daily_loss": {"limit": {"pct": "5"}, "basis": "start_balance"},
            "max_drawdown": {"kind": "trailing", "limit": {"pct": "10"}},
            "profit_target": {"pct": "8"},
            "min_trading_days": 4,
            "period_days": 30,
            "clock": "broker",
            "period_kinds": ["date", "lifetime"],
            "on_fail": {"kinds": ["notify", "block_entries"]},
        },
        idempotency_key=f"challenge-{key}",
    )
    print(f"challenge {made.id} version {made.version}")
    await mfx.risk.enrol(made.id, [account_id], idempotency_key=f"enrol-{key}")
    page = await mfx.risk.challenge_accounts_page(made.id, sort="profit")
    stats = page.stats
    if stats:  # the first page carries the counts of the whole list
        print(f"  {stats.accounts} account(s), {stats.in_progress} in progress")
    for row in page.items:
        print(f"  {row.account_id}: {row.state}, profit {row.profit_pct}%")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", default=os.environ.get("MFX_TERMINAL_ID"))
    parser.add_argument("--broker-id", default=os.environ.get("MFX_BROKER_ID"))
    parser.add_argument("--symbol", default=os.environ.get("MFX_SYMBOL", "EURUSD"))
    parser.add_argument("--stop-distance", type=Decimal, default=Decimal("0.0020"))
    parser.add_argument("--base-url")
    parser.add_argument("--protect", action="store_true")
    parser.add_argument("--plan", metavar="TICKET")
    parser.add_argument("--challenge", action="store_true")
    args = parser.parse_args()
    if args.base_url:
        global BASE_URL
        BASE_URL = args.base_url
    if not args.account or not args.broker_id:
        print(
            "pass --account and --broker-id, or set MFX_TERMINAL_ID and MFX_BROKER_ID"
        )
        return 2
    async with client() as mfx:
        if not hasattr(mfx, "risk"):
            print("this SDK has no client.risk: install mfx-sdk 0.16.0 or later")
            return 0
        try:
            await read(
                mfx, args.account, args.broker_id, args.symbol, args.stop_distance
            )
            if args.protect:
                await protect(mfx, args.account)
            if args.plan:
                await plan(mfx, args.account, args.plan)
            if args.challenge:
                await challenge(mfx, args.account)
        except ForbiddenError as refused:
            if refused.code == "feature_not_entitled":
                print(
                    f"Risk Management is not in this plan ({refused.details}): nothing to read"
                )
                return 0
            if "risk:read" in str(refused):
                # A key made without Risk Management's scopes: create one with risk:read (and
                # risk:write to protect an account) to run this sample.
                print("this key does not carry risk:read: create a key with it to read your rules")
                return 0
            raise
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
