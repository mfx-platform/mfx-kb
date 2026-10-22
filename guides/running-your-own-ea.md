# Running your own Expert Advisor

> The runnable half of this page is [`samples/expert_advisor.py`](../samples/expert_advisor.py),
> with the smallest Expert Advisor that honours the contract,
> [`samples/ea/mfx_sample_ea.mq5`](../samples/ea/mfx_sample_ea.mq5).

MFX Hub can run your own MQL5 Expert Advisor on your terminal. You send its **source**. MFX Hub
checks it, builds it for your farm's MetaTrader, runs it through a trial with trading off, and only
then lets it trade. It runs only on a farm that runs customer code; ask us for one if you don't
have it.

## 1. What your Expert Advisor must do

Your expert and MFX Hub talk through two kinds of file in the terminal's `MQL5\Files\mfx` folder,
and nothing else. Your expert never opens a socket and never sees a credential.

| File | Written by | What it holds |
|---|---|---|
| `params.json` | MFX Hub | your parameter set: its `version`, and `enabled`, which is `false` when trading must stop |
| `telemetry.<segment>.ndjson` | your expert | one JSON object per line: its heartbeat, and any record you want delivered to you |

Three things are required. An expert that does not do them is reported, and is not supported:

1. **Read `params.json`** whenever it changes. It may not exist until a parameter set is sent.
2. **Write a heartbeat line at least every 30 seconds**, carrying the parameter version it last
   read: `{"ts":"<unix seconds>","kind":"heartbeat","schema_version":"1.0","applied_version":<n>}`.
   Heartbeats are how MFX Hub knows your expert is alive and has read its parameters. They are not
   sent to you.
3. **Stop trading when `enabled` is `false`.** MFX Hub sets it when a bound is crossed, and it does
   not wait for your expert to agree.

Every other line you write (`{"ts": ..., "kind": "signal", ...}`, any JSON object) reaches you on
the terminal's `ea` channel (see section 5). Start a new file every minute or so, numbering the
segments upwards. A file is sent once a newer one exists, so this keeps your records arriving soon
after you write them.

What your expert may not do: load a DLL, make web requests, or write outside its own files. All
three are switched off for it in MetaTrader, and the checks flag an expert that tries.

## 2. Upload and the checks

`client.expert_advisors.upload(name, {path: source})` takes the bundle: exactly one `.mq5` and the
`.mqh` files it includes, as text. Never a compiled `.ex5`, which cannot be checked. An include must
be in your bundle or in MetaTrader's standard library. Sending the same bundle again answers the
version it already made, so nothing is checked twice.

The checks look for the following, grouped in the report:

| Group | What it looks for |
|---|---|
| security, threat | anything that reaches outside the terminal, or tries to hide what it does |
| trading | orders without bounds, risk the account cannot carry, ignoring `enabled` |
| performance | loops and timers that would cost the terminal more than its share |
| interface, contract and build | the contract above; that it builds for your MetaTrader |
| best practice | notes that do not stop it running |

Part of the review is done by an AI model. **Your source, fenced as untrusted text, is read by a
model provider** to look for what rules alone miss. A finding from the model never refuses your
expert by itself: a person confirms it first.

## 3. Reading the verdict and the report

`client.expert_advisors.wait_for_verdict(id)` reads the version until the checks have settled it.

- **green**: nothing found, or notes only. Approved, and you can attach it.
- **amber**: something needs judgement. It is held, and a person looks at it.
- **red**: something certain that has no honest use here. Refused, and the report is emailed to
  your representative.

`client.expert_advisors.report(id)` gives each finding with its rule, colour, file and line,
why it matters and what to change.

## 4. Attach, trial, live

`client.terminals.attach_expert_advisor(terminal_id, version_id, symbol=, timeframe=, reason=)`
restarts the terminal with your expert on that chart. Follow the restart with
`client.terminals.wait_for_operation`. Then the **trial**: your expert runs for about half an hour
with trading off, while MFX Hub measures it. The figures are on the version, under the terminal:

- processor against the terminal's cap;
- memory and chart objects added;
- what it wrote to disk;
- the orders it would have sent in its busiest minute;
- whether its heartbeat arrived every time.

A trial that stays within its lines is green.

- **A demo account** then goes live by itself: the terminal restarts once more, with trading on.
- **A real-money account** waits for your representative. The trial email carries a link and a
  one-time code. Use either the link, or `client.terminals.confirm_expert_advisor(terminal_id,
  code=)`. A key alone cannot turn on trading on real money.

`client.terminals.detach_expert_advisor(terminal_id, reason=)` takes it off again. The terminal's
folder is rebuilt, and your expert's files go with it.

## 5. What your expert writes, live

Subscribe to `Channel.ea(terminal_id)` on the stream, and read each message with
`mfx.telemetry_of`. Delivery is at least once:

- **A batch** gives its records in the order written, each with the segment and line it came
  from. A pair you have seen before is a repeat.
- **A loss** says which files were discarded, and how many records they held. That happens when
  your expert wrote faster than its records could be sent. Trading is never held for telemetry.
