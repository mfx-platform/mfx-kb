// Your first terminal: find your ids, request it, give it a credential, wait for ACTIVE, retire it.
//
// The runnable half of guides/your-first-terminal.md, in TypeScript.
//
//     MFX_KEY_ID=mfxk_... MFX_PRIVATE_KEY="$(cat my.key)" \
//     MFX_BROKER=Pepperstone MFX_SERVER=Pepperstone-Demo MFX_LOGIN=12345678 \
//     node samples/ts/first_terminal.ts
//
// MFX_BROKER is your broker's name and MFX_SERVER the server name your broker gave you, exactly as
// it appears in your trading platform's login window; the sample looks up their ids. MFX_FARM_ID is
// optional: without it, the first farm your plan allows is used. MFX_SAME_LOGIN_TERMINAL_ID is
// optional too: a terminal of yours that already uses MFX_LOGIN. With it, the sample first reads how
// many sessions the server allows on one login, and stops, skipped, when yours already fill them.
//
// The broker password is typed at a prompt. It is never an argument, an environment variable or a
// file, so it reaches no shell history, no process list and no CI log.

import { MfxApiError, MfxClient, type Terminal, type components } from "@mfxhub/sdk";

import { BASE_URL, Stop, apiText, ask, env, everyItem, RETIRE_WAIT_S, run, runId, sleep, terminalLabels, waitUntilRetired } from "./common.ts";

type Farm = components["schemas"]["TenantFarm"];
type Broker = components["schemas"]["TenantBroker"];
type Server = components["schemas"]["TenantBrokerServer"];

const SETTLED = new Set(["ACTIVE", "FAILED", "RETIRED"]);
/** Where a server's sessions per login are final. `probed` is only what the server was seen to
 * hold, a lower bound: one more may well fit, so the sample still asks. */
const KNOWN_CAP = new Set(["limit", "operator"]);

/** The farm, broker and server ids for the names you know, and what the farm offers. */
async function findIds(
  client: MfxClient,
  broker: string,
  server: string,
): Promise<[string, string, string, Set<string>]> {
  // A farm is where your terminals run; your plan says which you may use.
  const wanted = process.env.MFX_FARM_ID;
  let farm: Farm | null = null;
  for await (const candidate of everyItem<Farm>(client, "/farms")) {
    if (!wanted || candidate.id === wanted) {
      farm = candidate;
      break;
    }
  }
  if (farm === null) throw new Stop("no farm found for your plan; ask support which one to use");
  const offers = new Set(farm.usages.map(String));

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
  console.log(`farm ${farm.name} offers [${[...offers].sort().map((u) => `'${u}'`).join(", ")}]`);
  return [farm.id, brokerRow.id, serverRow.id, offers];
}

/** Why one more session on the login cannot fit, when the server's limit says so; else null.
 * Only with MFX_SAME_LOGIN_TERMINAL_ID: without a terminal on the login, this is its first
 * session. GET /v1/usage lists every enabled server's limit under `per_login.servers`. */
async function noRoom(client: MfxClient, serverId: string): Promise<string | null> {
  const other = process.env.MFX_SAME_LOGIN_TERMINAL_ID;
  if (!other) return null;
  const inUse = (await client.terminals.get(other)).same_login_terminals || 1;
  let usage: components["schemas"]["Usage"];
  try {
    usage = (await client.read({ method: "GET", path: "/usage" })) as components["schemas"]["Usage"];
  } catch (refused) {
    if (!(refused instanceof MfxApiError)) throw refused;
    console.log(`the server's limit could not be read (${apiText(refused)}); asking anyway`);
    return null;
  }
  const rows = ((usage as { per_login?: { servers?: unknown[] } }).per_login?.servers ?? []) as {
    server: { id: string };
    max_sessions: number;
    max_sessions_source?: string;
  }[];
  for (const row of rows) {
    if (row.server.id === serverId && KNOWN_CAP.has(row.max_sessions_source ?? "")) {
      if (inUse + 1 > Number(row.max_sessions)) {
        return `not requested: this server allows ${row.max_sessions} sessions on one login and yours use ${inUse}`;
      }
    }
  }
  return null;
}

run(async () => {
  const run_ = runId(); // one run, one set of idempotency keys: a retry repeats nothing
  const client = new MfxClient(BASE_URL, env("MFX_KEY_ID"), env("MFX_PRIVATE_KEY"));
  const [farmId, brokerId, serverId, offers] = await findIds(client, env("MFX_BROKER"), env("MFX_SERVER"));
  // Said as skipped, not failed: nothing is wrong, the login has no session to spare.
  const notice = await noRoom(client, serverId);
  if (notice !== null) {
    console.log(notice);
    console.log(JSON.stringify({ outcome: "skipped", notice, detail: null }));
    return;
  }

  // Ask only for what the farm offers: a farm that does not stream quotes refuses a streaming
  // terminal, and one that does not execute refuses an executing one. The same key with the same
  // body returns the same terminal, never a second one.
  let terminal: Terminal = await client.terminals.create({
    farmId,
    brokerId,
    serverId,
    streaming: offers.has("quote_streaming"),
    execution: offers.has("order_execution"),
    instruments: ["EURUSD"], // the broker's own symbol names
    labels: terminalLabels(),
    idempotencyKey: `first-terminal-${run_}`,
  });
  console.log(terminal.id, terminal.state); // REQUESTED

  // The SDK has no method for credentials yet; this is the same signed call it would make.
  await client.write({
    method: "POST",
    path: `/terminals/${terminal.id}/credentials`,
    body: { login: Number(env("MFX_LOGIN")), password: await ask("Broker password: ", true), server: env("MFX_SERVER") },
    idempotencyKey: `first-credential-${run_}`,
  });

  // On the way to ACTIVE a health check can read "failed" before the first live tick arrives;
  // that is not an error. conditions say why if it stalls.
  while (!SETTLED.has(terminal.state)) {
    await sleep(5000);
    terminal = await client.terminals.get(terminal.id);
    const seen = (terminal.conditions ?? {}) as Record<string, { status?: string }>;
    const statuses = Object.entries(seen).map(([name, c]) => `'${name}': '${c.status ?? "?"}'`);
    console.log(terminal.state, `{${statuses.join(", ")}}`);
  }

  await client.write({ method: "DELETE", path: `/terminals/${terminal.id}`, idempotencyKey: `first-retire-${run_}` });
  if (await waitUntilRetired(client, terminal.id)) console.log("retired");
  else console.log(`not RETIRED after ${RETIRE_WAIT_S} s: it may still hold its broker session`);
  if (terminal.state !== "ACTIVE") throw new Stop(`the terminal ended ${terminal.state}, not ACTIVE`);
});
