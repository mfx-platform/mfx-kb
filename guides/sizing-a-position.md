# Sizing a position

> The runnable half of this page is [`samples/position_sizing.py`](../samples/position_sizing.py),
> which type-checks against the SDK as it shipped.

If you take one thing from this page: **do the arithmetic in `Decimal`, never in `float`.**

## Why a float is the wrong tool here

A position size is *computed* — a risk budget divided by a stop distance — and binary floating point
cannot hold the decimals money is written in:

```python
>>> 0.07 * 3
0.21000000000000002
>>> 1.1 + 0.00021
1.1002100000000001
```

Those are not display quirks. They are the numbers, and `0.21000000000000002` lots is not a size any
broker deals in. MFX takes every decimal on the execution path **as a string** for that reason, in
both directions, and the SDK refuses a `float` rather than quietly sending one:

```python
>>> await client.execution.place_order(..., volume=0.07 * 3, ...)
TypeError: volume must be a Decimal, a str or an int, not a float: a computed float carries
binary error (0.07 * 3 is 0.21000000000000002) and the platform takes decimals as strings so
nothing is lost. Send Decimal("0.21000000000000002") or a string.
```

That error is deliberate. A `400` from the edge, or worse a fill at a size you did not choose, is a
far worse way to learn this.

## The rules

1. **Build every decimal with `Decimal(str)`.** `Decimal("0.1")` is a tenth; `Decimal(0.1)` carries
   the float's error straight into the `Decimal`, which defeats the point.
2. **Read the broker's own rules from the catalogue.** `volume_step`, `volume_min` and
   `contract_size` all arrive as strings, so `Decimal()` takes them exactly.
3. **Quantise to `volume_step`, rounding *down*.** Rounding up risks more than your budget, which is
   the one direction a sizing rule must never take on its own.
4. **Check `volume_min` before you send.** A budget too small for one minimum lot is a decision for
   you, not a rejection to discover at the broker.

```python
raw = risk / (stop_distance * contract_size)
volume = raw.quantize(volume_step, rounding=ROUND_DOWN)
```

## What the platform does with it

The value you send is the value that reaches your terminal. If a decimal does arrive slightly off the symbol's
step — from an internal caller, or from arithmetic done in binary — the agent puts it exactly onto
that step before sending, and **anything further off than float noise is sent as you wrote it**: MFX
never quietly moves a price you chose. A volume genuinely off the step is refused, with the reason.

## Try it

```
python samples/position_sizing.py --key-id mfxk_... --private-key-file key.txt \
    --terminal-id <uuid> --broker-id <uuid> --symbol EURUSD.sd \
    --risk 50 --stop-distance 0.00150
```

Nothing is placed unless you add `--place`. Without it the sample prints the size it would send,
which is the half worth reading first.
