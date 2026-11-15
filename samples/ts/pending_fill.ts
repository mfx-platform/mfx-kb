// A pending order that fills: the second runnable half of guides/pending-orders.md, in TypeScript.
//
// It finds your farm, broker and server by name, asks for a terminal, logs it in, opens the stream,
// places a buy stop just above the market, and waits on the stream for the broker to fill it: an
// `order.closed` with reason `filled` names the position, and a `position.opened` names the order it
// came from. It then closes that position and retires the terminal. Use a demo account: the order
// is real on whatever account you log in to.
//
//     MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \
//     MFX_BROKER=Pepperstone MFX_SERVER=Pepperstone-Demo MFX_LOGIN=12345678 \
//     node samples/ts/pending_fill.ts --symbol EURUSD --confirm
//
// Without --confirm it stops after the quote and places nothing. A stop fills only when the price
// reaches it: if the market does not move up to it within the wait, the sample cancels it and says
// so, which is not a failure. Whatever happens, it cancels the order, closes the position it opened
// and retires the terminal before it ends. The broker password is typed at a prompt: never an
// argument, an environment variable or a file.
//
// The last line it prints is its result, as JSON: {"outcome": "passed"} when the order filled and
// the position closed, {"outcome": "amber", "notice": ...} when the market was closed or did not
// reach the stop.

import { parseArgs } from "node:util";

import { Channel, MfxApiError, MfxClient, StreamMessage, type Quote, type Terminal, isUsable, type components } from "@mfxhub/sdk";

import { BASE_URL, Dec, Stop, ask, env, everyItem, nextBefore, onePage, RETIRE_WAIT_S, run, runId, sleep, terminalLabels, waitUntilRetired } from "./common.ts";

type Farm = components["schemas"]["TenantFarm"];
type Broker = components["schemas"]["TenantBroker"];
type Server = components["schemas"]["TenantBrokerServer"];
type SymbolSpec = components["schemas"]["Symbol"];

/** How long to wait for the stop to fill before cancelling it and saying so. */
const FILL_WAIT_S = 90;
/** A quote older than this says the market is closed: nothing would fill. */
const STALE_QUOTE_S = 60;
/** Points above the broker's stop level: the closest a buy stop may sit, plus a little. */
const EXTRA_POINTS = 2;

/** The farm, broker and server ids for the names you know, and what the farm offers. */
async function findIds(client: MfxClient, broker: string, server: string): Promise<[string, string, string, Set<string>]> {
  const wanted = process.env.MFX_FARM_ID;
  let farm: Farm | null = null;
  let offers = new Set<string>();
  for await (const candidate of everyItem<Farm>(client, "/farms")) {
    const has = new Set(candidate.usages.map(String));
    if (candidate.id === wanted || (!wanted && has.has("order_execution"))) {
      farm = candidate;
      offers = has;
      break;
    }
  }
  if (farm === null) throw new Stop("no farm on your plan executes orders; ask support which one to use");
  let brokerRow: Broker | null = null;
  for await (const row of everyItem<Broker>(client, "/brokers")) {
    if ([row.code.toLowerCase(), row.display_name.toLowerCase()].includes(broker.toLowerCase())) {
      brokerRow = row;
      break;
    }
  }
  if (brokerRow === null) throw new Stop(`no broker called '${broker}'; GET /v1/brokers lists them`);
  const servers: Server[] = [];
  for await (const row of everyItem<Server>(client, `/brokers/${brokerRow.id}/servers`)) servers.push(row);
  const serverRow = servers.find((s) => s.server_name.toLowerCase() === server.toLowerCase());
  if (serverRow === undefined || !serverRow.enabled) {
    const names = servers.filter((s) => s.enabled).map((s) => s.server_name).join(", ");
    throw new Stop(`server '${server}' is not available; ${brokerRow.display_name} offers ${names}`);
  }
  console.log(`farm ${farm.name}, broker ${brokerRow.display_name}, server ${serverRow.server_name}`);
  return [farm.id, brokerRow.id, serverRow.id, offers];
}

/** The symbol's quote, and the account type it was asked under (see first_order.ts). */
async function priced(client: MfxClient, brokerId: string, symbol: string, serverId: string): Promise<[Quote, string | undefined]> {
  let last: MfxApiError;
  try {
    return [await client.catalog.quote(brokerId, symbol, { serverId }), undefined];
  } catch (refused) {
    if (!(refused instanceof MfxApiError) || refused.code !== "ambiguous_symbol") throw refused;
    last = refused;
  }
  let cursor: string | undefined;
  for (;;) {
    const page = await onePage<SymbolSpec>(client, `/brokers/${brokerId}/symbols`, { server_id: serverId, cursor, limit: 200 });
    for (const listed of page.items.filter((s) => s.symbol === symbol && s.account_type)) {
      try {
        return [await client.catalog.quote(brokerId, symbol, { serverId, accountType: listed.account_type }), listed.account_type];
      } catch (refused) {
        if (!(refused instanceof MfxApiError)) throw refused;
        last = refused;
      }
    }
    if (!page.nextCursor) throw last;
    cursor = page.nextCursor;
  }
}

/** The closest buy stop the broker takes: the ask, plus the stop level, plus a couple of points,
 * up onto the symbol's tick and written with its digits. Exact: whole units of the last digit,
 * never a float. */
function stopAbove(ask: string, spec: SymbolSpec): string {
  const digits = Number(spec.digits);
  const units = (text: string): bigint => {
    const d = Dec.of(text);
    return d.scale <= digits ? d.units * 10n ** BigInt(digits - d.scale) : d.units / 10n ** BigInt(d.scale - digits);
  };
  const point = spec.point != null ? units(String(spec.point)) : 1n;
  const tick = spec.tick_size != null ? units(String(spec.tick_size)) : point;
  const level = BigInt(Number(spec.stops_level ?? 0) + EXTRA_POINTS);
  const raw = units(ask) + point * level;
  const step = tick > 0n ? tick : 1n;
  const snapped = ((raw + step - 1n) / step) * step;
  const text = snapped.toString().padStart(digits + 1, "0");
  return digits ? `${text.slice(0, -digits)}.${text.slice(-digits)}` : text;
}

/** A number the platform sent, or null. */
const num = (value: unknown): number | null => (typeof value === "number" ? value : typeof value === "string" && value !== "" ? Number(value) : null);

/** The last line: what a runner reads as this sample's outcome. */
function result(outcome: string, notice: string | null = null, detail: Record<string, unknown> | null = null): void {
  console.log(JSON.stringify({ outcome, notice, detail }));
}

run(async () => {
  const { values: args } = parseArgs({
    options: { symbol: { type: "string", default: "EURUSD" }, confirm: { type: "boolean", default: false } },
  });
  const symbol = args.symbol!;
  const run_ = runId(); // one run, one set of idempotency keys: a retry repeats nothing
  const mark = `mfx-sample-${run_}`; // on the order's comment, so this run finds its own order
  const client = new MfxClient(BASE_URL, env("MFX_KEY_ID"), env("MFX_PRIVATE_KEY"));
  const [farmId, brokerId, serverId, offers] = await findIds(client, env("MFX_BROKER"), env("MFX_SERVER"));

  let terminal: Terminal = await client.terminals.create({
    farmId,
    brokerId,
    serverId,
    streaming: offers.has("quote_streaming"),
    execution: offers.has("order_execution"),
    instruments: [symbol],
    labels: terminalLabels(),
    idempotencyKey: `fill-terminal-${run_}`,
  });
  try {
    await client.write({
      method: "POST",
      path: `/terminals/${terminal.id}/credentials`,
      body: { login: Number(env("MFX_LOGIN")), password: await ask("Broker password: ", true), server: env("MFX_SERVER") },
      idempotencyKey: `fill-credential-${run_}`,
    });
    for (;;) {
      terminal = await client.terminals.get(terminal.id);
      if (isUsable(terminal)) break;
      if (terminal.state === "FAILED" || terminal.state === "RETIRED") throw new Stop(`the terminal ended ${terminal.state}: read its conditions`);
      console.log(terminal.state);
      await sleep(5000);
    }
    console.log(`terminal ${terminal.id} is ACTIVE`);

    const [quote, accountType] = await priced(client, brokerId, symbol, serverId);
    const spec = (await client.read({
      method: "GET",
      path: `/brokers/${brokerId}/symbols/${encodeURIComponent(symbol)}`,
      query: { server_id: serverId, account_type: accountType },
    })) as SymbolSpec;
    console.log(`${symbol} bid ${quote.bid} ask ${quote.ask}, ${Number(quote.age_s).toFixed(1)} s old`);
    if (!args.confirm) {
      console.log("not placing an order: pass --confirm to place a stop and wait for its fill");
      return;
    }
    if (Number(quote.age_s) > STALE_QUOTE_S) {
      result("amber", `the last price is ${Number(quote.age_s).toFixed(0)} s old: the market looks closed`);
      return;
    }

    // Open the stream first, so the fill cannot happen before anyone is listening.
    const stream = await client.stream();
    let tickets: [number, number] | null = null;
    let ticket: number;
    try {
      await stream.subscribe(Channel.events);
      // A buy stop just above the ask: it fills as soon as the price rises to it.
      const price = stopAbove(String(quote.ask), spec);
      let placed = await client.execution.placeOrder(terminal.id, {
        symbol,
        side: "buy",
        orderType: "stop",
        price,
        volume: String(spec.volume_min),
        comment: mark,
        idempotencyKey: `fill-place-${run_}`,
      });
      if (placed.commandId) placed = await client.execution.waitFor(placed.commandId);
      if (placed.state !== "SUCCEEDED" || placed.tickets.order === null) throw new Stop(`the order was not placed: ${placed.reasonText ?? placed.detail}`);
      ticket = placed.tickets.order;
      console.log(`buy stop ${ticket} placed at ${price}`);

      // Both events arrive, in either order: order.closed (reason filled, position_ticket) and
      // position.opened (order_ticket). Any other reason means it closed without filling.
      const deadline = Date.now() + FILL_WAIT_S * 1000;
      let fromOrder: number | null = null;
      let opened: number | null = null;
      for (;;) {
        const event = await nextBefore(() => stream.next(), deadline);
        if (event === null) break;
        if (!(event instanceof StreamMessage) || event.data.terminal_id !== terminal.id) continue;
        if (event.type === "order.closed" && num(event.data.ticket) === ticket) {
          console.log(`order.closed ${ticket}: ${String(event.data.reason)}, position ${String(event.data.position_ticket)}`);
          if (event.data.reason !== "filled") break;
          fromOrder = num(event.data.position_ticket);
        } else if (event.type === "position.opened" && num(event.data.order_ticket) === ticket) {
          opened = num(event.data.ticket);
          console.log(`position.opened ${opened} at ${String(event.data.open_price)}, from order ${ticket}`);
        }
        if (fromOrder !== null && opened !== null) {
          if (fromOrder !== opened) throw new Stop(`the order names position ${fromOrder}, the position ${opened}`);
          tickets = [opened, ticket];
          break;
        }
      }
    } finally {
      await stream.close();
    }
    if (tickets === null) {
      result("amber", `the stop did not fill within ${FILL_WAIT_S} s`, { order: ticket });
      return; // the way out cancels it
    }
    const [positionTicket] = tickets;
    let closed = await client.execution.closePosition(terminal.id, positionTicket, { idempotencyKey: `fill-close-${run_}` });
    if (closed.commandId) closed = await client.execution.waitFor(closed.commandId);
    console.log(`close ${closed.state}`);
    if (closed.state !== "SUCCEEDED") throw new Stop(`the position was not closed: ${closed.reasonText ?? closed.detail}`);
    result("passed", null, { order: ticket, position: positionTicket });
  } finally {
    // Leave nothing behind: this run's order still pending, or its position still open (each
    // found by the comment), then the terminal.
    if (args.confirm) await tidy(client, terminal.id, mark, run_);
    await client.write({ method: "DELETE", path: `/terminals/${terminal.id}`, idempotencyKey: `fill-retire-${run_}` });
    if (await waitUntilRetired(client, terminal.id)) console.log("terminal retired");
    else console.log(`not RETIRED after ${RETIRE_WAIT_S} s: it may still hold its broker session`);
  }
});

/** Cancel this run's pending order and close its position, if either is still there. */
async function tidy(client: MfxClient, terminalId: string, mark: string, run_: string): Promise<void> {
  let orders, positions;
  try {
    orders = (await client.accounts.pendingOrders(terminalId)).items;
    positions = (await client.accounts.positions(terminalId)).items;
  } catch (error) {
    console.log(`could not read the account on the way out: ${error instanceof Error ? error.message : String(error)}`);
    return;
  }
  for (const order of orders.filter((o) => o.comment === mark)) {
    let gone = await client.execution.cancelOrder(terminalId, order.ticket, { idempotencyKey: `fill-cleanup-order-${order.ticket}-${run_}` });
    if (gone.commandId) gone = await client.execution.waitFor(gone.commandId);
    console.log(`order ${order.ticket} cancelled on the way out: ${gone.state}`);
  }
  for (const position of positions.filter((p) => p.comment === mark)) {
    let gone = await client.execution.closePosition(terminalId, position.ticket, { idempotencyKey: `fill-cleanup-position-${position.ticket}-${run_}` });
    if (gone.commandId) gone = await client.execution.waitFor(gone.commandId);
    console.log(`position ${position.ticket} closed on the way out: ${gone.state}`);
  }
}
