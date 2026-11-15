// One more terminal on a broker login you already use, and how many of yours now share it.
//
// The runnable half of guides/several-terminals-on-one-login.md, in TypeScript. It does only what a
// customer can do: every call is the public, signed API.
//
//     node samples/ts/several_terminals_one_login.ts <terminal id> --key-id mfxk_... --private-key-file <your key file>
//
// <terminal id> is a terminal of yours that already uses the login. The new terminal is requested
// on the same farm, broker and server. The login, the server and the password are typed at a
// prompt. They are never an argument, an environment variable or a file, so they reach no shell
// history, no process list and no CI log.

import { parseArgs } from "node:util";

import { MfxApiError, MfxClient, type Terminal, type components } from "@mfxhub/sdk";

import { BASE_URL, Stop, apiText, ask, keyId, privateKey, run, sleep, terminalLabels, waitUntilRetired } from "./common.ts";

const POLL_MS = 5000;
/** A first login is usually under two minutes; a cold cell can take longer. */
const TIMEOUT_S = 20 * 60;
/** What shows a terminal is logged in. Ticks are not part of it: a terminal with no instruments,
 * or one whose market is closed, is logged in and quotes nothing. */
const LIVE = ["ProcessRunning", "BrokerConnected"];
/** The two limits on terminals per login, as `details.reason` names them. */
const PLAN_LIMIT = "login_terminal_limit";
const BROKER_LIMIT = "login_session_limit";
/** Where a server's sessions per login are final. `probed` is only what the server was seen to
 * hold, a lower bound: one more may well fit, so the sample still asks. */
const KNOWN_CAP = new Set(["limit", "operator"]);
// DECISION: no settle time is waited here, after a terminal is retired or before one more is added.
// RETIRED is the platform's word that the terminal's broker session is gone, and the platform
// counts the sessions on a login; a pause guessed in a sample would hide a session the platform
// still counts wrong, and teach every reader a number nobody measured.

/** Which limit refused one more terminal on the login, in a sentence. */
function explain(refused: MfxApiError): string {
  const details = refused.details;
  const reason = details.reason;
  if (reason === PLAN_LIMIT) {
    return `your plan allows ${String(details.quota)} terminals on one login and you have ${String(details.in_use)}`;
  }
  if (reason === BROKER_LIMIT && details.retry_after_s) {
    return `the broker's limit on this server is being measured; try again in ${String(details.retry_after_s)} s`;
  }
  if (reason === BROKER_LIMIT) return "the broker's limit of sessions on this login is reached";
  return apiText(refused);
}

/** The sessions one login may hold on this server, when that number is final; else null.
 * GET /v1/usage lists every enabled server under `per_login.servers`. */
async function knownCap(client: MfxClient, serverId: string): Promise<number | null> {
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
    if (row.server.id === serverId && KNOWN_CAP.has(row.max_sessions_source ?? "")) return Number(row.max_sessions);
  }
  return null;
}

/** Say the sample was skipped, not failed: nothing is wrong, there is no room to show it. */
function skip(notice: string): void {
  console.log(notice);
  console.log(JSON.stringify({ outcome: "skipped", notice, detail: null }));
}

/** ACTIVE with the process up and the broker connected. */
function live(terminal: Terminal): boolean {
  if (terminal.state !== "ACTIVE") return false;
  const seen = (terminal.conditions ?? {}) as Record<string, { status?: string }>;
  return LIVE.every((name) => seen[name]?.status === "ok");
}

/** Poll until the terminal is logged in, failed or stopped, printing each change. */
async function waitUntilLive(client: MfxClient, terminalId: string): Promise<string> {
  const started = Date.now();
  let last = "";
  while (Date.now() - started < TIMEOUT_S * 1000) {
    const terminal = await client.terminals.get(terminalId);
    const isLive = live(terminal);
    const shown = `${terminal.state} live=${isLive ? "True" : "False"}`;
    if (shown !== last) {
      console.log(`[${String(Math.floor((Date.now() - started) / 1000)).padStart(4)}s] ${shown}`);
      last = shown;
    }
    if (["FAILED", "DRAINING", "RETIRED"].includes(terminal.state)) return terminal.state;
    if (isLive) return "ACTIVE";
    await sleep(POLL_MS);
  }
  throw new Stop(`FAILED: not live after ${TIMEOUT_S}s`);
}

run(async () => {
  const { values: args, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      "key-id": { type: "string" },
      "private-key-file": { type: "string" },
      "base-url": { type: "string", default: BASE_URL },
    },
  });
  const terminalId = positionals[0];
  if (!terminalId) throw new Stop("give the id of a terminal of yours that already uses the login", 2);
  const client = new MfxClient(args["base-url"]!, keyId(args["key-id"]), privateKey(args["private-key-file"]));

  const first = await client.terminals.get(terminalId);
  console.log(`${terminalId} is ${first.state}; yours on its login: ${first.same_login_terminals}`);

  // One more terminal is one more session on the login. When the server's limit is final and yours
  // already fill it, the broker would refuse the login: there is nothing to show.
  const cap = await knownCap(client, first.server.id);
  const inUse = first.same_login_terminals || 1;
  if (cap !== null && inUse + 1 > cap) {
    skip(`not added: this server allows ${cap} sessions on one login and yours use ${inUse}`);
    return;
  }

  const login = Number(await ask("Broker login (the one that terminal already has): "));
  const server = (await ask("Broker server: ")).trim();
  const password = await ask("Password: ", true);

  // Request one more terminal on the same farm, broker and server as the first.
  const second = await client.terminals.create({
    farmId: first.farm.id,
    brokerId: first.broker.id,
    serverId: first.server.id,
    streaming: Boolean(first.streaming),
    labels: terminalLabels(),
    idempotencyKey: `same-login-${crypto.randomUUID()}`,
  });
  console.log(`requested ${second.id}: ${second.state}`);
  try {
    // Give the new terminal the same login. Each terminal holds its own copy of the credential.
    await client.write({
      method: "POST",
      path: `/terminals/${second.id}/credentials`,
      body: { login, password, server },
      idempotencyKey: `same-login-credential-${crypto.randomUUID()}`,
    });
  } catch (refused) {
    if (!(refused instanceof MfxApiError)) throw refused;
    // Nothing was stored. The terminal you requested has no credential; retire it.
    await client.write({ method: "DELETE", path: `/terminals/${second.id}`, idempotencyKey: `retire-${crypto.randomUUID()}` });
    await waitUntilRetired(client, second.id);
    throw new Stop(`REFUSED: ${explain(refused)}`);
  }

  const state = await waitUntilLive(client, second.id);
  for (const id of [terminalId, second.id]) {
    const seen = await client.terminals.get(id);
    console.log(`${id} ${seen.state}; yours on its login: ${seen.same_login_terminals}`);
  }
  console.log(state === "ACTIVE" ? "PASS" : `FAILED: the new terminal is ${state}; read its conditions`);
});
