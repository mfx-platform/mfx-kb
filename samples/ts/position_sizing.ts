// Size a position from a risk budget, exactly - the runnable half of guides/sizing-a-position.md,
// in TypeScript.
//
// The one thing to take away: do the arithmetic in exact decimals and never in JavaScript numbers.
// A size is computed, not typed - risk budget over stop distance - and in binary floating point
// `0.07 * 3` is `0.21000000000000002`. MFX takes decimals on the execution path as strings for that
// reason, and the SDK refuses a number rather than quietly sending one.
//
// Run it against dev:
//
//     node samples/ts/position_sizing.ts --key-id mfxk_... --private-key-file key.txt \
//         --terminal-id <uuid> --broker-id <uuid> --symbol EURUSD.sd \
//         --risk 50 --stop-distance 0.00150
//
// Nothing is placed unless you pass --place. Without it the sample prints the size it would send,
// which is the half worth reading.

import { parseArgs } from "node:util";

import { MfxClient, type components } from "@mfxhub/sdk";

import { BASE_URL, Dec, Stop, keyId, privateKey, run } from "./common.ts";

type SymbolSpec = components["schemas"]["Symbol"];

/**
 * How many lots put `risk` of the account currency behind `stopDistance`, on this symbol.
 *
 * Rounded down to the symbol's own step: rounding up would risk more than the budget, which is the
 * one direction a sizing rule must never take on its own.
 */
function lotsFor(risk: Dec, stopDistance: Dec, contractSize: Dec, volumeStep: Dec): Dec {
  if (!stopDistance.isPositive() || !contractSize.isPositive() || !volumeStep.isPositive()) {
    throw new RangeError("stop distance, contract size and volume step must all be above zero");
  }
  return risk.dividedDownTo(stopDistance.times(contractSize), volumeStep);
}

run(async () => {
  const { values: args } = parseArgs({
    options: {
      "key-id": { type: "string" },
      "private-key-file": { type: "string" },
      "terminal-id": { type: "string" },
      "broker-id": { type: "string" },
      symbol: { type: "string" },
      risk: { type: "string" },
      "stop-distance": { type: "string" },
      side: { type: "string", default: "buy" },
      "base-url": { type: "string", default: BASE_URL },
      place: { type: "boolean", default: false },
    },
  });
  const terminalId = args["terminal-id"];
  const brokerId = args["broker-id"];
  const symbolName = args.symbol;
  if (!terminalId || !brokerId || !symbolName || !args.risk || !args["stop-distance"]) {
    throw new Stop("--terminal-id, --broker-id, --symbol, --risk and --stop-distance are needed", 2);
  }
  if (args.side !== "buy" && args.side !== "sell") throw new Stop("--side is buy or sell", 2);

  // Exact from the start: a number would carry its binary error into the decimal.
  const risk = Dec.of(args.risk);
  const stopDistance = Dec.of(args["stop-distance"]);
  const client = new MfxClient(args["base-url"]!, keyId(args["key-id"]), privateKey(args["private-key-file"]));

  // The broker's own rules for this symbol. Every decimal it returns is a string.
  const symbol = (await client.read({
    method: "GET",
    path: `/brokers/${brokerId}/symbols/${encodeURIComponent(symbolName)}`,
  })) as SymbolSpec;
  const contractSize = Dec.of(symbol.contract_size);
  const volumeStep = Dec.of(symbol.volume_step);
  const volumeMin = Dec.of(symbol.volume_min);

  const volume = lotsFor(risk, stopDistance, contractSize, volumeStep);
  console.log(`${symbolName}: step ${volumeStep}, contract ${contractSize}, min ${volumeMin}`);
  console.log(`${risk} at risk over ${stopDistance} -> ${volume} lots`);

  if (volume.compare(volumeMin) < 0) {
    console.log(`refusing: ${volume} is below the symbol's minimum of ${volumeMin}`);
    return;
  }
  if (!args.place) {
    console.log("not placed (pass --place). The volume above is exactly what would be sent.");
    return;
  }

  const result = await client.execution.placeOrder(terminalId, {
    symbol: symbolName,
    side: args.side,
    volume: String(volume), // a decimal string: "0.21", never 0.21000000000000002
    idempotencyKey: `sizing-${crypto.randomUUID()}`,
  });
  // Every execution call returns a result and never throws: a timeout, a refusal and a broker
  // rejection are all outcomes of trying to trade.
  console.log(`${result.state} ok=${result.ok ? "True" : "False"} command=${result.commandId}`);
  if (!result.ok) console.log(`  reason=${result.reason} retryable=${result.retryable ? "True" : "False"}`);
});
