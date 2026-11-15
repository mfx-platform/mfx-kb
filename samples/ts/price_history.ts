// Read a symbol's past bars and past ticks - the runnable half of guides/reading-price-history.md,
// in TypeScript.
//
// Three things to take away:
//
// 1. History comes through your own terminal. MFX Hub holds no broker account and keeps no price
//    store. The bars and ticks you are given are the ones your broker's server shows your account,
//    read by one of your ACTIVE terminals when you ask. No terminal on the server, no history.
// 2. Bars are read backwards from `to`, ticks forwards from `from`. A window that holds more than
//    1,000 bars answers with the newest 1,000, so you page by moving `to` earlier. A tick read
//    answers with the first ticks at or after `from` and says whether there are `more`.
// 3. Several ticks can share one millisecond. Continue a tick read from the last tick's time and
//    the next answer starts with ticks you already hold. allTicks below drops exactly those; do it
//    any other way and you either lose ticks or double them.
//
// Run it against dev:
//
//     node samples/ts/price_history.ts --key-id mfxk_... --private-key-file key.txt //         --broker-id <uuid> --symbol EURUSD.sd --timeframe M5 --hours 4 --tick-seconds 30
//
// It only reads. Nothing is placed and nothing is subscribed.

import { parseArgs } from "node:util";

import { MfxApiError, MfxClient, type components } from "@mfxhub/sdk";

import { BASE_URL, Dec, Stop, keyId, maxDec, minDec, minuteUtc, privateKey, run, secondUtc } from "./common.ts";

type Candle = components["schemas"]["Candle"];
type Tick = components["schemas"]["Tick"];
type TickList = components["schemas"]["TickList"];

/** The most bars one call returns. A wider window is read in pages, newest first. */
const PAGE = 1000;

/**
 * Every bar of the window, oldest first, read a page at a time from the newest end.
 *
 * A window holding more than a page answers with its newest bars. To get the ones before, ask
 * again with `to` set just before the oldest bar you were given. A page that comes back short is
 * the start of the window.
 */
async function barsNewestFirst(
  client: MfxClient,
  brokerId: string,
  symbol: string,
  timeframe: string,
  start: Date,
  end: Date,
  serverId: string | undefined,
): Promise<Candle[]> {
  const pages: Candle[][] = [];
  let to = end;
  for (;;) {
    const page = (await client.read({
      method: "GET",
      path: "/md/candles",
      query: {
        broker: brokerId,
        symbol,
        tf: timeframe,
        from: start.toISOString(),
        to: to.toISOString(),
        server_id: serverId,
      },
    })) as { items: Candle[] };
    if (!page.items.length) break;
    pages.push(page.items);
    if (page.items.length < PAGE) break;
    to = new Date(Date.parse(page.items[0]!.ts) - 1000);
    if (to < start) break;
  }
  return pages.reverse().flat();
}

async function ticks(
  client: MfxClient,
  brokerId: string,
  symbol: string,
  from: Date | string,
  options: { limit?: number; serverId?: string } = {},
): Promise<TickList> {
  return (await client.read({
    method: "GET",
    path: "/md/ticks",
    query: {
      broker: brokerId,
      symbol,
      from: typeof from === "string" ? from : from.toISOString(),
      limit: options.limit,
      server_id: options.serverId,
    },
  })) as TickList;
}

/**
 * Every tick of a window, in order, each once. The way to continue is to ask again from the last
 * tick's time, and several ticks can share one millisecond, so the next answer begins with ticks
 * already held: those are dropped, and only those.
 */
async function* allTicks(
  client: MfxClient,
  brokerId: string,
  symbol: string,
  from: Date,
  serverId: string | undefined,
): AsyncGenerator<Tick, void, undefined> {
  const key = (t: Tick): string => JSON.stringify([t.ts, t.bid, t.ask, t.last, t.volume]);
  let start: string = from.toISOString();
  let held: string[] = [];
  for (;;) {
    const answer = await ticks(client, brokerId, symbol, start, serverId ? { serverId } : {});
    const fresh: Tick[] = [];
    for (const tick of answer.items) {
      const at = held.indexOf(key(tick));
      if (tick.ts === start && at >= 0) {
        held.splice(at, 1);
        continue;
      }
      fresh.push(tick);
    }
    yield* fresh;
    // Nothing new with more promised is one millisecond holding more ticks than a call returns;
    // asking again would return the same ones for ever.
    if (!answer.more || !fresh.length) return;
    start = answer.items[answer.items.length - 1]!.ts;
    held = answer.items.filter((t) => t.ts === start).map(key);
  }
}

run(async () => {
  const { values: args } = parseArgs({
    options: {
      "key-id": { type: "string" },
      "private-key-file": { type: "string" },
      "broker-id": { type: "string" },
      symbol: { type: "string" },
      timeframe: { type: "string", default: "M5" },
      hours: { type: "string", default: "4" },
      "tick-seconds": { type: "string", default: "30" },
      "server-id": { type: "string" },
      "base-url": { type: "string", default: BASE_URL },
    },
  });
  const brokerId = args["broker-id"];
  const symbol = args.symbol;
  if (!brokerId || !symbol) throw new Stop("--broker-id and --symbol are needed", 2);
  const hours = Number(args.hours);
  const tickSeconds = Number(args["tick-seconds"]);
  const serverId = args["server-id"];
  const client = new MfxClient(args["base-url"]!, keyId(args["key-id"]), privateKey(args["private-key-file"]));
  const now = new Date();

  // -- bars ---------------------------------------------------------------------------------------
  const bars = await barsNewestFirst(
    client,
    brokerId,
    symbol,
    args.timeframe!,
    new Date(now.getTime() - hours * 3600_000),
    now,
    serverId,
  );
  console.log(`${symbol} ${args.timeframe}: ${bars.length} bars over the last ${hours} h`);
  if (bars.length) {
    // o, h, l and c are decimal strings, compared exactly. `ts` is the bar's open time as a true
    // instant, whatever clock the broker keeps.
    const high = bars.map((b) => Dec.of(b.h)).reduce(maxDec);
    const low = bars.map((b) => Dec.of(b.l)).reduce(minDec);
    console.log(`  first ${minuteUtc(bars[0]!.ts)} UTC, last ${minuteUtc(bars[bars.length - 1]!.ts)} UTC`);
    console.log(`  range ${low} to ${high}; last close ${bars[bars.length - 1]!.c}`);
    console.log("  the last bar is still forming: its close is the latest price, not a final one");
  }

  // -- ticks --------------------------------------------------------------------------------------
  const since = new Date(now.getTime() - tickSeconds * 1000);
  const one = await ticks(client, brokerId, symbol, since, { limit: 5 });
  console.log(`first ${one.items.length} ticks since ${secondUtc(since)} UTC (more: ${one.more ? "True" : "False"})`);
  for (const tick of one.items) {
    // `last` and `volume` are null where the broker sends none, as most FX brokers do.
    console.log(`  ${tick.ts.slice(11, 23)} bid ${tick.bid} ask ${tick.ask} last ${tick.last ?? "None"}`);
  }

  let count = 0;
  let widest = Dec.of("0");
  for await (const tick of allTicks(client, brokerId, symbol, since, serverId)) {
    count += 1;
    widest = maxDec(widest, Dec.of(tick.ask).minus(Dec.of(tick.bid)));
  }
  console.log(`${count} ticks in the last ${tickSeconds} s; widest spread ${widest}`);

  // -- a symbol the server does not list -------------------------------------------------------------
  try {
    await ticks(client, brokerId, "NOT-A-SYMBOL", since, { limit: 1 });
  } catch (refused) {
    if (!(refused instanceof MfxApiError)) throw refused;
    // 404 not_found, with details.reason = symbol_unknown: the terminal's server has no such
    // symbol. Symbol names are the broker's own; read them from the broker's symbol list.
    console.log(`an unlisted symbol: ${refused.status} ${refused.code}`);
  }
});
