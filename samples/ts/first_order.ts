// Your first quote and your first order, from nothing: the runnable half of
// guides/your-first-order.md, in TypeScript.
//
// It finds your farm, broker and server by name, asks for a terminal, logs it in, reads a quote,
// buys the smallest volume the symbol allows, closes that position, and retires the terminal. Use a
// demo account: the order is real on whatever account you log in to.
//
//     MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \
//     MFX_BROKER=Pepperstone MFX_SERVER=Pepperstone-Demo MFX_LOGIN=12345678 \
//     node samples/ts/first_order.ts --symbol EURUSD --confirm
//
// Without --confirm it stops after the quote and places nothing. The broker password is typed at a
// prompt: never an argument, an environment variable or a file, so it reaches no shell history, no
// process list and no CI log.

import { parseArgs } from "node:util";

import { MfxApiError, MfxClient, type Quote, type Terminal, isUsable, type components } from "@mfxhub/sdk";

import { BASE_URL, Stop, ask, env, everyItem, onePage, RETIRE_WAIT_S, run, runId, sleep, terminalLabels, waitUntilRetired } from "./common.ts";

type Farm = components["schemas"]["TenantFarm"];
type Broker = components["schemas"]["TenantBroker"];
type Server = components["schemas"]["TenantBrokerServer"];
type SymbolSpec = components["schemas"]["Symbol"];

/** The farm, broker and server ids for the names you know, and what the farm offers. */
async function findIds(
  client: MfxClient,
  broker: string,
  server: string,
): Promise<[string, string, string, Set<string>]> {
  // A farm is where your terminals run; your plan says which you may use. Take one that can
  // execute orders, or the one named in MFX_FARM_ID.
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

  // The broker by its name (code or display name), then its server by the name your broker gave
  // you, exactly as it appears in your trading platform's login window.
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

/** The symbol's quote, and the account type it was asked under. A server may list one symbol
 * under several account types; a quote that names none is then refused as ambiguous, so it is
 * asked again under each listing's account type until one answers. */
async function priced(
  client: MfxClient,
  brokerId: string,
  symbol: string,
  serverId: string,
): Promise<[Quote, string | undefined]> {
  let last: MfxApiError;
  try {
    return [await client.catalog.quote(brokerId, symbol, { serverId }), undefined];
  } catch (refused) {
    if (!(refused instanceof MfxApiError) || refused.code !== "ambiguous_symbol") throw refused;
    last = refused;
  }
  let cursor: string | undefined;
  for (;;) {
    const page = await onePage<SymbolSpec>(client, `/brokers/${brokerId}/symbols`, {
      server_id: serverId,
      cursor,
      limit: 200,
    });
    for (const listed of page.items.filter((s) => s.symbol === symbol && s.account_type)) {
      try {
        const quote = await client.catalog.quote(brokerId, symbol, { serverId, accountType: listed.account_type });
        return [quote, listed.account_type];
      } catch (refused) {
        if (!(refused instanceof MfxApiError)) throw refused;
        last = refused;
      }
    }
    if (!page.nextCursor) throw last;
    cursor = page.nextCursor;
  }
}

run(async () => {
  const { values: args } = parseArgs({
    options: {
      symbol: { type: "string", default: "EURUSD" },
      confirm: { type: "boolean", default: false },
    },
  });
  const symbol = args.symbol!;
  const run_ = runId(); // one run, one set of idempotency keys: a retry repeats nothing
  const client = new MfxClient(BASE_URL, env("MFX_KEY_ID"), env("MFX_PRIVATE_KEY"));
  const [farmId, brokerId, serverId, offers] = await findIds(client, env("MFX_BROKER"), env("MFX_SERVER"));

  // 1. A terminal for your account, asking only for what the farm offers.
  let terminal: Terminal = await client.terminals.create({
    farmId,
    brokerId,
    serverId,
    streaming: offers.has("quote_streaming"),
    execution: offers.has("order_execution"),
    instruments: [symbol], // the broker's own symbol names
    labels: terminalLabels(),
    idempotencyKey: `first-order-terminal-${run_}`,
  });
  try {
    // The SDK has no method for credentials yet; this is the same signed call it would make.
    await client.write({
      method: "POST",
      path: `/terminals/${terminal.id}/credentials`,
      body: { login: Number(env("MFX_LOGIN")), password: await ask("Broker password: ", true), server: env("MFX_SERVER") },
      idempotencyKey: `first-order-credential-${run_}`,
    });
    // Usable means ACTIVE. On the way, a health check can read "failed" before the first live tick
    // arrives; that is not an error.
    for (;;) {
      terminal = await client.terminals.get(terminal.id);
      if (isUsable(terminal)) break;
      if (terminal.state === "FAILED" || terminal.state === "RETIRED") {
        throw new Stop(`the terminal ended ${terminal.state}: read its conditions`);
      }
      console.log(terminal.state);
      await sleep(5000);
    }
    console.log(`terminal ${terminal.id} is ACTIVE`);

    // 2. A quote, always with its age. broker_time is the broker's clock, not UTC.
    const [quote, accountType] = await priced(client, brokerId, symbol, serverId);
    console.log(`${symbol} bid ${quote.bid} ask ${quote.ask}, ${Number(quote.age_s).toFixed(1)} s old`);
    if (!args.confirm) {
      console.log("not placing an order: pass --confirm to buy the smallest volume and close it");
      return;
    }

    // 3. The smallest volume the symbol allows, as a decimal string.
    const spec = (await client.read({
      method: "GET",
      path: `/brokers/${brokerId}/symbols/${encodeURIComponent(symbol)}`,
      query: { server_id: serverId, account_type: accountType },
    })) as SymbolSpec;
    const placed = await client.execution.placeOrder(terminal.id, {
      symbol,
      side: "buy",
      volume: String(spec.volume_min),
      idempotencyKey: `first-order-buy-${run_}`,
    });
    // Execution never throws: every outcome is a result. Wait until it is final.
    const done = placed.commandId ? await client.execution.waitFor(placed.commandId) : placed;
    console.log(`order ${done.state}: ${done.execVolume} at ${done.execPrice} (${done.commentRaw})`);
    if (done.state !== "SUCCEEDED" || done.tickets.position === null) {
      throw new Stop(`the order did not fill: ${done.reasonText ?? done.detail}`);
    }

    // 4. Close the position the order opened, by its ticket.
    let closed = await client.execution.closePosition(terminal.id, done.tickets.position, {
      idempotencyKey: `first-order-close-${run_}`,
    });
    if (closed.commandId) closed = await client.execution.waitFor(closed.commandId);
    console.log(`close ${closed.state} at ${closed.execPrice}`);
  } finally {
    // 5. Retire the terminal, whatever happened above.
    await client.write({
      method: "DELETE",
      path: `/terminals/${terminal.id}`,
      idempotencyKey: `first-order-retire-${run_}`,
    });
    if (await waitUntilRetired(client, terminal.id)) console.log("terminal retired");
    else console.log(`not RETIRED after ${RETIRE_WAIT_S} s: it may still hold its broker session`);
  }
});
