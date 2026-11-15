// Check that the TypeScript SDK keeps the platform's promises against a real MFX Hub, with your key
// - the TypeScript twin of samples/sdk_check.py, running the same live cases (samples/suite/live.json).
//
// Every MFX Hub SDK passes a shared test suite before it releases, against a fake hub that can be
// told how to answer. A handful of its rules a real hub shows on its own; this runs each of them
// through the SDK against your account: your signature is accepted and the suite's test key is
// refused, a terminal you do not have is not found, a list follows its cursor, a quote is exact
// decimals, a number volume is refused before sending, ticks arrive in seq order (skipped when the
// market is closed; not run without a subscription for the symbol), and with --orders an order
// sent twice with one idempotency key is one order.
//
//     MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" node samples/ts/sdk_check.ts \
//         --terminal-id <uuid> --broker-id <uuid> --server-id <uuid> --symbol EURUSD
//
// Without --orders nothing is placed. --orders --confirm runs only the case that places an order:
// use a demo account, as it buys the symbol's smallest volume once and closes it. On a
// closed market (a quote over a minute old, or no tick to quote at all) it places nothing and ends
// skipped. A quote the hub cannot give for want of a tick (503, no_tick) skips that case: there is
// no price to judge, which is not a wrong one. --json ends with one line the platform's checks
// read: {"outcome", "notice", "detail"}.

import { readFileSync } from "node:fs";
import { parseArgs } from "node:util";

import { AuthError, Channel, MfxApiError, MfxClient, NotFoundError, StreamMessage, type Quote, type components } from "@mfxhub/sdk";

import { BASE_URL, everyItem, keyId, privateKey, run } from "./common.ts";

type SymbolSpec = components["schemas"]["Symbol"];
type Subscription = components["schemas"]["MdSubscription"];
type Json = Record<string, unknown>;
interface LiveCase {
  readonly id: string;
  readonly client?: string;
  readonly call: { readonly op: string; readonly args: Json };
  readonly observe: Json;
}

/** How long a case that reads ticks waits for them before it says the market is not quoting. */
const TICK_WAIT_MS = 30_000;
const ORDER_WAIT_MS = 30_000;
/** A quote older than this says the market is closed: --orders then places nothing. */
const STALE_QUOTE_S = 60;
const KINDS: Readonly<Record<string, abstract new (...args: never[]) => Error>> = { AuthError, NotFoundError };
const SUITE = new URL("../suite/", import.meta.url);

/** The case cannot be judged here: the market is not quoting. */
class Skip extends Error {}
/** The case needs something this account has not set up, such as a subscription. */
class NotRun extends Error {}

/** A case value: a run value, a number to be refused, or the value itself. */
function value(raw: unknown, values: Readonly<Record<string, string>>): unknown {
  if (Array.isArray(raw)) return raw.map((item) => value(item, values));
  if (typeof raw === "object" && raw !== null) {
    if ("$run" in raw) return values[String(raw.$run)];
    if ("$float" in raw) return Number(raw.$float);
    if ("$decimal" in raw) return String(raw.$decimal);
  }
  return raw;
}

/** Make one call through the SDK and say what came back, in the suite's names. */
async function call(client: MfxClient, op: string, a: Json): Promise<Json> {
  if (op === "terminals.get") {
    const terminal = await client.terminals.get(String(a.terminal_id));
    return { id: terminal.id, state: terminal.state };
  }
  if (op === "terminals.page") {
    const page = await client.terminals.page({ limit: Number(a.limit) });
    return { count: page.length, next_cursor: page.nextCursor };
  }
  if (op === "terminals.list") {
    const ids: string[] = [];
    for await (const terminal of client.terminals.list({ limit: Number(a.limit) })) ids.push(terminal.id);
    return { ids };
  }
  if (op === "catalog.quote") {
    let quote: Quote;
    try {
      quote = await quoted(client, String(a.broker_id), String(a.symbol), String(a.server_id));
    } catch (error) {
      // A closed market sends no tick, and a symbol no terminal watches then has no price to give
      // (guides/when-a-call-fails.md): there is nothing to judge, which is not a wrong one.
      if (noTick(error)) throw new Skip("no price to read now: the terminal saw no tick in time (no_tick)");
      throw error;
    }
    return { bid: quote.bid, ask: quote.ask, age_s: quote.age_s, state: quote.state };
  }
  if (op === "execution.place_order") {
    // A number volume is passed as it came: the SDK must refuse it, which is what the case checks.
    const result = await client.execution.placeOrder(String(a.terminal_id), {
      symbol: String(a.symbol),
      side: a.side as "buy" | "sell",
      volume: a.volume as string,
      idempotencyKey: String(a.idempotency_key),
    });
    return { ok: result.ok, state: result.state, command_id: result.commandId, duplicate: result.duplicate, position: result.tickets.position };
  }
  if (op === "stream.read") return readTicks(client, a.channels as string[], Number(a.count));
  throw new Error(`this sample does not know the operation ${op}`);
}

/** The hub had no tick to price the symbol with: 503 upstream_error, details.reason no_tick. */
function noTick(error: unknown): boolean {
  return error instanceof MfxApiError && error.code === "upstream_error" && error.details.reason === "no_tick";
}

/** How many listings of the instrument a quote is asked of before giving up. */
const TRIES = 6;

/** EURUSD, EURUSD.sd and EURUSDm are one instrument; EURUSD2 and EURUSDX too long a suffix are not. */
function isVariant(name: string, base: string): boolean {
  const upper = name.toUpperCase();
  const plain = base.toUpperCase();
  if (upper === plain) return true;
  const rest = upper.slice(plain.length);
  return upper.startsWith(plain) && rest.length > 0 && rest.length <= 4 && !/^[0-9]/.test(rest);
}

/**
 * The symbol's quote, from a listing that has one to give. A server may list one
 * instrument under several names and account types (EURUSD and EURUSD.sd). A quote that names none
 * is refused as ambiguous, and a listing no terminal watches has no tick to price itself with (503,
 * no_tick): the price is then asked of every listing of the instrument, the name as given first,
 * until one answers. The case judges the SDK's decimals, not which listing a terminal watches.
 */
async function quoted(client: MfxClient, brokerId: string, symbol: string, serverId: string): Promise<Quote> {
  let last: unknown;
  try {
    return await client.catalog.quote(brokerId, symbol, { serverId });
  } catch (refused) {
    const ambiguous = refused instanceof MfxApiError && refused.code === "ambiguous_symbol";
    if (!ambiguous && !noTick(refused)) throw refused;
    last = refused;
  }
  const listed: SymbolSpec[] = [];
  for await (const one of everyItem<SymbolSpec>(client, `/brokers/${brokerId}/symbols`, { server_id: serverId })) {
    if (isVariant(one.symbol, symbol)) listed.push(one);
  }
  listed.sort((a, b) => Number(a.symbol !== symbol) - Number(b.symbol !== symbol) || a.symbol.length - b.symbol.length);
  for (const one of listed.slice(0, TRIES)) {
    try {
      return await client.catalog.quote(brokerId, one.symbol, { serverId, accountType: one.account_type ?? undefined });
    } catch (refused) {
      last = refused;
    }
  }
  throw last;
}

/** Read `count` messages from the channels; Skip when they do not come in time, NotRun when there
 * is no subscription to stream them. */
async function readTicks(client: MfxClient, channels: readonly string[], count: number): Promise<Json> {
  for (const channel of channels) {
    const [, serverId, symbol] = channel.split(":");
    let found = false;
    for await (const s of everyItem<Subscription>(client, "/md/subscriptions")) {
      if (s.server.id === serverId && s.symbol === symbol) found = true;
    }
    if (!found) throw new NotRun(`no subscription for ${symbol} on that server, so no ticks are streamed`);
  }
  const seqs: (number | null)[] = [];
  const stream = await client.stream();
  const deadline = Date.now() + TICK_WAIT_MS;
  let timer: NodeJS.Timeout | undefined;
  try {
    for (const channel of channels) await stream.subscribe(channel);
    const late = new Promise<null>((resolve) => {
      timer = setTimeout(() => {
        resolve(null);
      }, TICK_WAIT_MS);
    });
    while (seqs.length < count && Date.now() < deadline) {
      const event = await Promise.race([stream.next(), late]);
      if (event === null) break;
      if (event instanceof StreamMessage) seqs.push(event.seq);
    }
    if (seqs.length < count) throw new Skip(`${seqs.length} of ${count} ticks in ${TICK_WAIT_MS / 1000} s: the market is not quoting`);
    return { seqs, dropped: stream.stats.dropped };
  } finally {
    clearTimeout(timer);
    await stream.close();
  }
}

/** An exact decimal above zero: a decimal string, never a number. */
function exact(got: unknown): boolean {
  return typeof got === "string" && /^\d+(\.\d+)?$/.test(got) && /[1-9]/.test(got);
}

const same = (got: unknown, want: unknown): boolean => String(got) === String(want);

/** Close the position an order opened, once its command is final. Never leaves one open. */
async function closeOpened(client: MfxClient, results: readonly Json[], terminalId: string): Promise<void> {
  for (const result of results) {
    if (result.duplicate === true) continue;
    let position = result.position as number | null;
    if (position === null && typeof result.command_id === "string") {
      position = (await client.execution.waitFor(result.command_id, { timeoutMs: ORDER_WAIT_MS })).tickets.position;
    }
    if (position === null) continue;
    const closed = await client.execution.closePosition(terminalId, position, { idempotencyKey: `sdk-check-close-${crypto.randomUUID().slice(0, 12)}` });
    if (closed.commandId) await client.execution.waitFor(closed.commandId, { timeoutMs: ORDER_WAIT_MS });
    console.log(`  closed position ${position}`);
  }
}

/** One case: passed, failed or skipped, and why when it is not passed. */
async function judge(c: LiveCase, client: MfxClient, testKey: MfxClient, values: Readonly<Record<string, string>>): Promise<[string, string | null]> {
  const observe = c.observe;
  const args = Object.fromEntries(Object.entries(c.call.args).map(([name, raw]) => [name, value(raw, values)]));
  const who = c.client === "test_key" ? testKey : client;
  if ("raises" in observe) {
    try {
      const got = await call(who, c.call.op, args);
      return ["failed", `returned ${JSON.stringify(got)}, the case wants ${String(observe.raises)}`];
    } catch (error) {
      if (!(error instanceof MfxApiError)) throw error;
      const kind = KINDS[String(observe.raises)];
      if (kind === undefined || !(error instanceof kind)) return ["failed", `raised ${error.name} ${error.status} ${error.code}`];
      const fields = (observe.fields ?? {}) as Json;
      const wrong = Object.keys(fields).filter((k) => !same((error as unknown as Json)[k], fields[k]));
      return wrong.length ? ["failed", `raised with ${wrong.join(", ")} not as the case says`] : ["passed", null];
    }
  }
  if ("refuses" in observe) {
    try {
      const got = await call(who, c.call.op, args);
      return ["failed", `sent and returned ${JSON.stringify(got)}, the case wants it refused before sending`];
    } catch (error) {
      if (error instanceof TypeError || error instanceof RangeError) return ["passed", null];
      throw error;
    }
  }
  if ("repeat" in observe) {
    const want = observe.repeat as { returns?: Json; same?: string[] };
    const first = await call(who, c.call.op, { ...args });
    let second: Json = {};
    try {
      second = await call(who, c.call.op, { ...args });
      const bad = [
        ...Object.entries(want.returns ?? {}).filter(([k, v]) => !same(second[k], v)).map(([k]) => k),
        ...(want.same ?? []).filter((k) => !same(second[k], first[k])),
      ];
      return bad.length ? ["failed", `the second call differs on ${bad.join(", ")}`] : ["passed", null];
    } finally {
      await closeOpened(client, [first, second], values.terminal_id!);
    }
  }
  const got = await call(who, c.call.op, args);
  const problems: string[] = [];
  for (const [name, w] of Object.entries((observe.returns ?? {}) as Json)) {
    if (!same(got[name], value(w, values))) problems.push(`${name} is ${JSON.stringify(got[name])}`);
  }
  for (const [name, w] of Object.entries((observe.includes ?? {}) as Json)) {
    if (!(got[name] as unknown[]).map(String).includes(String(value(w, values)))) problems.push(`${name} does not include ${String(value(w, values))}`);
  }
  for (const name of (observe.exact ?? []) as string[]) {
    if (!exact(got[name])) problems.push(`${name} is ${JSON.stringify(got[name])}, not an exact decimal above zero`);
  }
  if (typeof observe.ascending === "string") {
    const seq = got[observe.ascending] as (number | null)[];
    if (!seq.length || seq.some((s, i) => i > 0 && (s === null || seq[i - 1] === null || s <= seq[i - 1]!))) {
      problems.push(`${observe.ascending} ${JSON.stringify(seq)} does not rise`);
    }
  }
  return problems.length ? ["failed", problems.join("; ")] : ["passed", null];
}

run(async () => {
  const { values: args } = parseArgs({
    options: {
      "key-id": { type: "string" },
      "private-key-file": { type: "string" },
      "terminal-id": { type: "string" },
      "broker-id": { type: "string" },
      "server-id": { type: "string" },
      symbol: { type: "string" },
      orders: { type: "boolean", default: false },
      confirm: { type: "boolean", default: false },
      json: { type: "boolean", default: false },
      "base-url": { type: "string", default: BASE_URL },
    },
  });
  const terminalId = args["terminal-id"];
  const brokerId = args["broker-id"];
  const serverId = args["server-id"];
  const symbol = args.symbol;
  if (!terminalId || !brokerId || !serverId || !symbol) return 2;
  if (args.orders && !args.confirm) {
    console.log("not placing an order: --orders needs --confirm, on a demo account");
    return 2;
  }
  const every = JSON.parse(readFileSync(new URL("live.json", SUITE), "utf8")) as LiveCase[];
  const vocabulary = JSON.parse(readFileSync(new URL("vocabulary.json", SUITE), "utf8")) as { key: { id: string; seed_hex: string } };
  const version = readFileSync(new URL("VERSION", SUITE), "utf8").trim();
  // The cases that place an order run apart, where orders are expected; the rest only read.
  const cases = every.filter((c) => "repeat" in c.observe === args.orders);
  const baseUrl = args["base-url"]!;
  const client = new MfxClient(baseUrl, keyId(args["key-id"]), privateKey(args["private-key-file"]));
  const testKey = new MfxClient(baseUrl, vocabulary.key.id, Buffer.from(vocabulary.key.seed_hex, "hex"));
  const spec = (await client.read({ method: "GET", path: `/brokers/${brokerId}/symbols/${encodeURIComponent(symbol)}`, query: { server_id: serverId } })) as SymbolSpec;
  if (args.orders) {
    // An order on a closed market would only be refused: say so and place nothing.
    let notice: string | undefined;
    try {
      const quote = await quoted(client, brokerId, symbol, serverId);
      if (Number(quote.age_s) > STALE_QUOTE_S) notice = `the last price is ${Number(quote.age_s).toFixed(0)} s old: the market looks closed`;
    } catch (error) {
      if (!noTick(error)) throw error;
      notice = "no price to read now (no_tick): the market looks closed";
    }
    if (notice !== undefined) {
      console.log(`sdk check: skipped - ${notice}`);
      if (args.json) {
        const detail = { suite: version, library: "typescript", part: "orders", cases: {} };
        console.log(JSON.stringify({ outcome: "skipped", notice, detail }));
      }
      return 0;
    }
  }
  console.log(`suite ${version}: ${cases.length} live cases against ${baseUrl}`);
  const results: Record<string, string> = {};
  const why: Record<string, string> = {};
  for (const c of cases) {
    const values = {
      terminal_id: terminalId, broker_id: brokerId, server_id: serverId, symbol,
      md_channel: Channel.md(serverId, symbol), min_volume: String(spec.volume_min),
      unused_id: crypto.randomUUID(), new_key: `sdk-check-${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`,
    };
    let outcome: string;
    let reason: string | null;
    try {
      [outcome, reason] = await judge(c, client, testKey, values);
    } catch (error) {
      if (error instanceof Skip) [outcome, reason] = ["skipped", error.message];
      else if (error instanceof NotRun) [outcome, reason] = ["not_run", error.message];
      else if (error instanceof MfxApiError) [outcome, reason] = ["failed", `${error.name} ${error.status} ${error.code}`];
      else throw error;
    }
    results[c.id] = outcome;
    if (reason) why[c.id] = reason;
    console.log(`${c.id}: ${outcome}${reason ? ` - ${reason}` : ""}`);
  }
  const failed = Object.keys(results).filter((id) => results[id] === "failed");
  const skipped = Object.keys(results).filter((id) => results[id] === "skipped");
  const notRun = Object.keys(results).filter((id) => results[id] === "not_run");
  const outcome = failed.length ? "failed" : skipped.length ? "amber" : "passed";
  const said = [
    failed.length ? `${failed.length} of ${cases.length} failed: ${failed.join(", ")}` : "",
    skipped.length ? `${skipped.length} skipped: ${skipped.join(", ")}` : "",
    notRun.length ? `${notRun.length} not run: ${notRun.join(", ")}` : "",
  ].filter((part) => part !== "");
  const notice = said.length ? said.join("; ") : null;
  // Last, so a short tail of the output still says why.
  for (const id of failed) console.log(`  ${id}: ${why[id]}`);
  console.log(`sdk check: ${outcome}${notice ? ` - ${notice}` : ""}`);
  if (args.json) {
    const detail = { suite: version, library: "typescript", part: args.orders ? "orders" : "reads", cases: results, why };
    console.log(JSON.stringify({ outcome, notice, detail }));
  }
  return failed.length ? 1 : 0;
});
