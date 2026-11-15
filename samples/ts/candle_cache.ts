// Keep price history cached, read only what is missing, and wait when told to - the runnable half
// of guides/limits-on-price-history.md, in TypeScript.
//
// Three things to take away:
//
// 1. A closed bar never changes. Once you hold it, never ask for it again. This sample keeps the
//    closed bars it has read in a small file, and the second read asks only for the bars after the
//    newest one it holds.
// 2. `source` says what an answer cost. `store` was answered by MFX Hub's candle store and cost your
//    terminals nothing. `terminal` (or `store+terminal`) went to one of your terminals, and those
//    reads are limited per terminal per minute.
// 3. A 429 terminal_reads_limit means "next minute". The SDK already waits Retry-After and asks
//    again a few times. If it still throws RateLimitError, wait retryAfterS yourself and carry on;
//    never retry in a tight loop.
//
// Run it against dev:
//
//     node samples/ts/candle_cache.ts --key-id mfxk_... --private-key-file key.txt \n//         --broker-id <uuid> --symbol EURUSD.sd --timeframe M5 --hours 4
//
// It only reads. Nothing is placed and nothing is subscribed. The cache file is written in your
// temporary folder unless you name one with --cache-file.

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs } from "node:util";

import { MfxClient, RateLimitError, type components } from "@mfxhub/sdk";

import { BASE_URL, Stop, keyId, privateKey, run, sleep } from "./common.ts";

type Candle = components["schemas"]["Candle"];
type CandleList = components["schemas"]["CandleList"];
type Held = Record<string, { o: string; h: string; l: string; c: string; v: number }>;

/** The most bars one call returns. */
const PAGE = 1000;
/** How long each timeframe's bar lasts, in ms. MN1 is left out: a month has no fixed length. */
const BAR: Record<string, number> = {
  M1: 60_000, M5: 300_000, M15: 900_000, M30: 1_800_000,
  H1: 3_600_000, H4: 14_400_000, D1: 86_400_000, W1: 604_800_000,
};
/** How many times this sample waits on its own after the SDK's retries are spent. */
const OWN_WAITS = 2;

/** Closed bars kept on disk, one series per (broker, server, symbol, timeframe). */
class BarCache {
  series: Record<string, Held> = {};
  readonly path: string;

  constructor(path: string) {
    this.path = path;
    if (existsSync(path)) this.series = JSON.parse(readFileSync(path, "utf8")) as Record<string, Held>;
  }

  bars(name: string): Held {
    return (this.series[name] ??= {});
  }

  newest(name: string): number | null {
    const held = Object.keys(this.series[name] ?? {});
    return held.length ? Math.max(...held.map((ts) => Date.parse(ts))) : null;
  }

  save(): void {
    writeFileSync(this.path, JSON.stringify(this.series));
  }
}

/**
 * GET /md/candles, waiting Retry-After when a terminal's reads for the minute are spent.
 *
 * The SDK has already waited and asked again a few times before it throws. A read that is still
 * refused is told when the next minute starts: wait that long, then ask once more.
 */
async function candlesWaitingWhenTold(client: MfxClient, query: Record<string, string | undefined>): Promise<CandleList> {
  for (let wait = 0; ; wait++) {
    try {
      return (await client.read({ method: "GET", path: "/md/candles", query })) as CandleList;
    } catch (refused) {
      if (!(refused instanceof RateLimitError) || refused.code !== "terminal_reads_limit" || wait === OWN_WAITS) {
        throw refused;
      }
      const seconds = refused.retryAfterS ?? 1;
      console.log(`  this terminal's ${String(refused.details["limit"])} reads a minute are spent; waiting ${seconds} s`);
      await sleep(seconds * 1000);
    }
  }
}

/**
 * Bring the cached series up to now, reading only what it does not hold. Answers the `source` of
 * each read made, so you can see what each cost.
 */
async function fill(
  client: MfxClient,
  cache: BarCache,
  name: string,
  where: { brokerId: string; symbol: string; timeframe: string; serverId: string | undefined },
  start: number,
  now: number,
): Promise<string[]> {
  const held = cache.bars(name);
  const newest = cache.newest(name);
  const length = BAR[where.timeframe]!;
  // Only the range after the newest closed bar we hold: one small read, not the whole history.
  const begin = newest !== null ? newest + length : start;
  const sources: string[] = [];
  let to = now;
  while (begin <= to) {
    const page = await candlesWaitingWhenTold(client, {
      broker: where.brokerId,
      symbol: where.symbol,
      tf: where.timeframe,
      from: new Date(begin).toISOString(),
      to: new Date(to).toISOString(),
      server_id: where.serverId,
    });
    sources.push(page.source ?? "not said");
    for (const bar of page.items as Candle[]) {
      const opened = Date.parse(bar.ts);
      // Closed: keep it. The bar still forming is not kept.
      if (opened + length <= now) held[new Date(opened).toISOString()] = { o: bar.o, h: bar.h, l: bar.l, c: bar.c, v: bar.v };
    }
    if (page.items.length < PAGE) break;
    to = Date.parse(page.items[0]!.ts) - 1000;
  }
  cache.save();
  return sources;
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
      "server-id": { type: "string" },
      "cache-file": { type: "string" },
      "base-url": { type: "string", default: BASE_URL },
    },
  });
  const brokerId = args["broker-id"];
  const symbol = args.symbol;
  if (!brokerId || !symbol) throw new Stop("--broker-id and --symbol are needed", 2);
  const timeframe = args.timeframe!;
  if (!(timeframe in BAR)) throw new Stop(`--timeframe is one of ${Object.keys(BAR).join(" ")}`, 2);
  const serverId = args["server-id"];
  const path = args["cache-file"] ?? join(tmpdir(), "mfx-candle-cache-ts.json");
  const cache = new BarCache(path);
  const name = `${brokerId}|${serverId ?? ""}|${symbol}|${timeframe}`;
  const where = { brokerId, symbol, timeframe, serverId };
  const client = new MfxClient(args["base-url"]!, keyId(args["key-id"]), privateKey(args["private-key-file"]));

  const now = Date.now();
  const start = now - Number(args.hours) * 3_600_000;
  const first = await fill(client, cache, name, where, start, now);
  console.log(`${symbol} ${timeframe}: ${Object.keys(cache.bars(name)).length} closed bars cached in ${path}`);
  console.log(`  reads made: ${first.length}, answered by ${first.join(", ") || "nothing new"}`);

  // A moment later: the cache already holds every closed bar, so the next read asks only for the
  // bars after the newest one - normally the bar still forming, and nothing else.
  const second = await fill(client, cache, name, where, start, Date.now());
  console.log(
    `  then: ${second.length} read for only what is new (${second.join(", ") || "none"}); ` +
      `${Object.keys(cache.bars(name)).length} closed bars held`,
  );
  console.log("  follow new bars on the candles channel (md.candle_closed) instead of polling");
});
