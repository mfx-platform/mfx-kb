# MFX Hub knowledge base

Guides and runnable samples for building on [MFX Hub](https://mfxhub.com): hosted trading terminals
for your own broker accounts, with market data, execution, account state and a live event stream
behind one signed API.

Every code sample is a real file under [`samples/`](samples/). A guide quotes its sample and never
carries code of its own, and the samples run in CI against MFX Hub's development environment, so a
sample that stops working fails a build instead of misleading you.

## Start here

1. **[Your first terminal](guides/your-first-terminal.md)** - from an API key to a terminal that is
   `ACTIVE`, then retired.
2. **[Your first order](guides/your-first-order.md)** - a quote, a filled order and the position
   closed, from nothing.
3. **[A terminal's whole life](guides/a-terminals-whole-life.md)** - every state, what to do in
   each, and what `OFFLINE` means for you.
4. **[Following an account live](guides/following-an-account-live.md)** - balance, equity, margin
   and positions as they move.
5. **[Following prices live](guides/following-prices-live.md)** and **[reading price history](guides/reading-price-history.md)** -
   quotes, ticks and bars.
6. **[Sizing a position](guides/sizing-a-position.md)** - decimals, not floats, from risk to lots.
7. **[When a call fails](guides/when-a-call-fails.md)** - what each error means and when to retry.

Then, as you need them: [several terminals on one login](guides/several-terminals-on-one-login.md),
[changing a terminal's password](guides/changing-a-terminals-password.md),
[receiving webhooks](guides/receiving-webhooks.md) and
[running your own expert advisor](guides/running-your-own-ea.md).

## Running a sample

The samples use the [MFX Hub Python SDK](https://github.com/mfx-platform/mfx-sdk-python):

```bash
pip install mfx-sdk
export MFX_KEY_ID=mfxk_...                # your API key id
export MFX_PRIVATE_KEY="$(cat my.key)"    # its private key, shown once when the key was issued
python samples/price_history.py --broker-id <broker id> --symbol EURUSD
```

Each sample's `--help` lists what it needs. Samples default to the development environment
(`https://api.dev.mfxhub.com/v1`); pass `--base-url https://api.mfxhub.com/v1` for production.
Broker passwords are always typed at a prompt, never passed as an argument or an environment
variable.

## Support and security

- Documentation, including the API and SDK reference: [docs.mfxhub.com](https://docs.mfxhub.com).
- Questions and corrections: open an issue, or write to support@mfxhub.com.
- Contributing: see [CONTRIBUTING.md](CONTRIBUTING.md).
- Security reports: see [SECURITY.md](SECURITY.md). Please do not open a public issue for one.

## Licence and trademarks

The guides and samples are licensed under the [Apache License 2.0](LICENSE).

MFX Hub is an independent service and is not affiliated with, endorsed by or sponsored by
MetaQuotes Ltd. MetaTrader is a trademark of MetaQuotes Ltd., used here only to say which trading
platform MFX Hub's terminals are compatible with.
