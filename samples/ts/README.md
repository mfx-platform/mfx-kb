# The samples in TypeScript

The same samples as the Python ones beside this folder, written against the
[TypeScript SDK](https://github.com/mfx-platform/mfx-sdk-typescript) (`@mfxhub/sdk`). Each takes the
same arguments, prints the same lines and exits the same way as its Python twin, so a guide can
point at either.

| Sample | What it shows | Guide |
| --- | --- | --- |
| `webhook_receiver.ts` | checking every delivery: the signature over the body as it arrived, answer first, no duplicate handled twice | [Receiving webhooks](../../guides/receiving-webhooks.md) |
| `prices_live.ts` | a subscription, then its channel, and the two events that say when prices stop | [Following prices live](../../guides/following-prices-live.md) |
| `price_history.ts` | bars read backwards in pages, ticks read forwards, each tick once | [Reading price history](../../guides/reading-price-history.md) |
| `candle_cache.ts` | closed bars cached, only the missing range read, and a wait when a terminal's reads for the minute are spent | [Limits on price history](../../guides/limits-on-price-history.md) |
| `account_live.ts` | an account kept current from its snapshot and marks, and noticing when it goes quiet | [Following an account live](../../guides/following-an-account-live.md) |
| `position_sizing.ts` | a size from a risk budget, in exact decimals | [Sizing a position](../../guides/sizing-a-position.md) |
| `expert_advisor.ts` | upload, verdict, attach, trial and detach of your own Expert Advisor | [Running your own EA](../../guides/running-your-own-ea.md) |
| `first_order.ts` | a terminal from nothing, a quote, the smallest order, closed, retired | [Your first order](../../guides/your-first-order.md) |
| `pending_fill.ts` | a buy stop just above the market, waited for on the stream until it fills, then closed | [Pending orders](../../guides/pending-orders.md#when-it-fills) |
| `sdk_check.ts` | the shared SDK test suite's live cases, run through this SDK against your account; `--orders --confirm` adds the one that places an order | [The SDK test suite](https://github.com/mfx-platform/mfx-sdk-conformance#running-the-live-cases) |
| `first_terminal.ts` | a terminal requested, logged in, watched to ACTIVE and retired | [Your first terminal](../../guides/your-first-terminal.md) |
| `several_terminals_one_login.ts` | one more terminal on a login you already use, and the two limits on it | [Several terminals on one login](../../guides/several-terminals-on-one-login.md) |
| `terminal_lifecycle.ts` | retire the terminals you name, ask for them again, watch them go live | [A terminal's whole life](../../guides/a-terminals-whole-life.md) |

## Running one

Node.js 22.18 or later runs a `.ts` file as it is: there is no build step.

```bash
cd samples/ts
npm ci
node prices_live.ts --key-id mfxk_... --private-key-file key.txt --server-id <uuid> --symbol EURUSD.sd
```

`common.ts` holds what every sample shares: reading the key, the prompt for a broker password,
exact decimals and one way to end. A broker password is always typed at a prompt, never passed as
an argument, an environment variable or a file.

Where the SDK has no method for a call yet (subscriptions, credentials, history, Expert Advisors),
the sample makes the same signed call itself with `client.read` and `client.write`, and says so.

`npm run typecheck` checks every sample against the SDK version `package.json` pins.
