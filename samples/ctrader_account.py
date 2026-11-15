"""Connect a cTrader account, read it, and place and close one market order through it.

A cTrader account is connected by its owner: you start a grant, send them to the consent link it
gives you, they sign in at cTrader with their cTrader ID and choose which accounts to share, and
they come back to your page. Then you connect the accounts you want, and each becomes a trading
account of yours, read and traded through the same calls on every venue.

Start a grant and print the consent link (needs the scope `term:write`)::

    MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \\
    python samples/ctrader_account.py --connect --return-url https://example.com/ctrader/back

After the trader has consented, connect the accounts they shared (the grant id came back on your
return URL)::

    python samples/ctrader_account.py --grant <grant id> --choose <cTrader account id>

Read a connected account's funds and symbols (needs `acct:read`), and with ``--confirm`` buy the
smallest volume of a symbol and close it again (needs `exec:write`)::

    MFX_CTRADER_ACCOUNT_ID=<account id> python samples/ctrader_account.py --symbol EURUSD --confirm

``--confirm`` is refused on a live account: use a demo one. Without an account id the sample says
it has nothing to read and exits cleanly, so a check run without a cTrader account is not a
failure.

Volumes are in lots on every venue. On cTrader a volume must be a whole ``volume_step`` between
``volume_min`` and ``volume_max``, which the account's symbols say; the smallest order is
``volume_min``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from mfx import MfxClient, SigningKey

BASE_URL = os.environ.get("MFX_BASE_URL", "https://api.dev.mfxhub.com/v1")


def client() -> MfxClient:
    return MfxClient(BASE_URL, os.environ["MFX_KEY_ID"], SigningKey(os.environ["MFX_PRIVATE_KEY"]))


async def connect(return_url: str, environment: str) -> None:
    """Start a grant and print where to send the trader."""
    async with client() as mfx:
        started = await mfx.trading_accounts.start_ctrader_grant(
            environment=environment,
            scope="trading",  # "accounts" reads only; "trading" reads and trades
            return_url=return_url,
            tenant_state=uuid.uuid4().hex,  # your own reference; it comes back on the return URL
            idempotency_key=f"ctrader-grant-{uuid.uuid4().hex}",
        )
        print(f"grant {started.grant_id}: send the trader to {started.consent_url}")
        print(f"the link works until {started.expires_at}")


async def choose(grant_id: str, accounts: list[str]) -> None:
    """List what the trader shared, and connect the accounts chosen."""
    async with client() as mfx:
        grant = await mfx.trading_accounts.ctrader_grant(grant_id)
        print(f"grant {grant.id} is {grant.state}")
        for shared in grant.accounts:
            kind = "live" if shared.live else "demo"
            print(f"  {shared.ctid_trader_account_id} {kind} at {shared.broker_name}")
        if str(grant.state) != "ACTIVE" or not accounts:
            return
        connected = await mfx.trading_accounts.connect_ctrader_accounts(
            grant_id, accounts, idempotency_key=f"ctrader-connect-{grant_id}"
        )
        for account in connected:
            print(f"account {account.id} is {account.state}; it reads CONNECTED once authorised")


async def trade(account_id: str, symbol: str, confirm: bool) -> None:
    """Read the account, then buy the smallest volume and close it, with --confirm on demo."""
    run = uuid.uuid4().hex[:8]  # one run, one set of idempotency keys: a retry repeats nothing
    async with client() as mfx:
        account = await mfx.trading_accounts.get(account_id)
        print(f"{account.venue} {account.environment} account at {account.broker.name}: {account.state}")

        funds = await mfx.trading_accounts.funds(account_id)
        if funds.snapshot is not None:
            # Amounts are decimal strings. On cTrader the equity is computed by the platform.
            print(f"balance {funds.snapshot.balance}, equity {funds.snapshot.equity}"
                  f"{' (stale)' if funds.stale else ''}")  # fmt: skip

        symbols = await mfx.trading_accounts.symbols(account_id)
        spec = next((s for s in symbols.items if s.name == symbol), None)
        if spec is None:
            raise SystemExit(f"{symbol} is not among this account's {len(symbols.items)} symbols")
        print(f"{symbol}: from {spec.volume_min} lots in steps of {spec.volume_step}, "
              f"leverage {spec.leverage}")  # fmt: skip

        if not confirm:
            print("not placing an order: pass --confirm to buy the smallest volume and close it")
            return
        if str(account.environment) != "demo":
            raise SystemExit("--confirm places a real order: use a demo account")

        placed = await mfx.trading_accounts.place_order(
            account_id,
            symbol=symbol,
            side="buy",
            volume=spec.volume_min,
            idempotency_key=f"ctrader-buy-{run}",
        )
        # Orders never raise: every outcome is a result. Wait until it is final.
        done = await mfx.execution.wait_for(placed.command_id) if placed.command_id else placed
        print(f"order {done.state}: {done.exec_volume} lots at {done.exec_price}")
        if str(done.state) != "SUCCEEDED" or done.tickets.position is None:
            # The venue's own code, when it gave one, sits beside the reason.
            raise SystemExit(f"the order did not fill: {done.reason_text} {done.venue_code or ''}")

        closed = await mfx.trading_accounts.close_position(
            account_id, done.tickets.position, idempotency_key=f"ctrader-close-{run}"
        )
        closed = await mfx.execution.wait_for(closed.command_id) if closed.command_id else closed
        print(f"close {closed.state} at {closed.exec_price}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--connect", action="store_true", help="start a grant")
    parser.add_argument("--return-url", help="where the trader comes back to, https")
    parser.add_argument("--environment", choices=("demo", "live"), default="demo")
    parser.add_argument("--grant", help="a grant id, to list and connect its accounts")
    parser.add_argument("--choose", action="append", default=[], help="a cTrader account id")
    parser.add_argument("--account", default=os.environ.get("MFX_CTRADER_ACCOUNT_ID"),
                        help="a connected account's id (or MFX_CTRADER_ACCOUNT_ID)")  # fmt: skip
    parser.add_argument("--symbol", default=os.environ.get("MFX_CTRADER_SYMBOL", "EURUSD"))
    parser.add_argument("--confirm", action="store_true", help="place and close one order")
    args = parser.parse_args()

    if args.connect:
        if not args.return_url:
            parser.error("--connect needs --return-url")
        asyncio.run(connect(args.return_url, args.environment))
    elif args.grant:
        asyncio.run(choose(args.grant, args.choose))
    elif args.account:
        asyncio.run(trade(args.account, args.symbol, args.confirm))
    else:
        print("skipped: no cTrader account to read; set MFX_CTRADER_ACCOUNT_ID or pass --account")


if __name__ == "__main__":
    main()
