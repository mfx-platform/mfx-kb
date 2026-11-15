// A terminal's whole life, twice: retire what you have, then ask for it again and watch it go live.
//
// The runnable half of guides/a-terminals-whole-life.md, in TypeScript. It does only what a
// customer can do - every call is the public, signed API - in three phases you can run together or
// one at a time:
//
//     retire    the terminals you name with --terminal are retired - never any other; the script
//               waits until each is RETIRED and writes what it was (farm, broker, server,
//               instruments - no secret) to a plan file
//     request   every terminal in the plan is asked for again, given its credential, and watched
//               until it is ACTIVE with fresh ticks
//     all       retire, pause until you press Enter, then request
//
//     node samples/ts/terminal_lifecycle.ts all --terminal <terminal id> \
//         --key-id mfxk_... --private-key-file C:\secure\mfx-dev.key --instruments EURUSD,XAUUSD
//
// Broker logins and passwords are typed at a prompt. They are never an argument, an environment
// variable or a file, so they reach no shell history, no process list and no CI log - and the plan
// file holds nothing you would mind losing.

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { parseArgs } from "node:util";

import { MfxApiError, MfxClient, type Terminal } from "@mfxhub/sdk";

import { BASE_URL, Stop, apiText, ask, keyId, privateKey, run, runId, sleep, terminalLabels } from "./common.ts";

const PLAN = "terminal-lifecycle-plan.json";
const POLL_MS = 5000;
/** Generous on purpose: a cold cell image pull and a first broker login are minutes, not seconds. */
const RETIRE_TIMEOUT_S = 15 * 60;
const ACTIVE_TIMEOUT_S = 20 * 60;
const LIVE = ["ProcessRunning", "BrokerConnected", "TicksFresh"];

interface PlanEntry {
  was: string;
  farm_id: string;
  broker_id: string;
  broker: string;
  server_id: string;
  server: string;
  streaming: boolean;
  execution: boolean;
  instruments: string[];
}

function conditions(terminal: Terminal): Record<string, string> {
  const seen = (terminal.conditions ?? {}) as Record<string, { status?: string }>;
  return Object.fromEntries(Object.entries(seen).map(([name, c]) => [name, c.status ?? "?"]));
}

function label(ref: { label?: string | null; id: string }): string {
  return ref.label ?? ref.id;
}

function line(started: number, terminal: Terminal): void {
  const dots = Object.entries(conditions(terminal))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, status]) => `${name}=${status}`)
    .join(" ");
  const elapsed = String(Math.floor((Date.now() - started) / 1000)).padStart(4);
  console.log(`[${elapsed}s] ${label(terminal.broker).padEnd(14)} ${terminal.state.padEnd(13)} ${dots}`.trimEnd());
}

/** Poll until every terminal satisfies `until`, printing each change of state or condition. */
async function watch(
  client: MfxClient,
  ids: string[],
  until: (terminal: Terminal) => boolean,
  timeoutS: number,
): Promise<Terminal[]> {
  const started = Date.now();
  const last = new Map<string, string>();
  const done = new Map<string, Terminal>();
  while (done.size < ids.length) {
    if (Date.now() - started > timeoutS * 1000) {
      const waiting = ids.filter((id) => !done.has(id));
      throw new Stop(`FAILED: still waiting after ${timeoutS}s for ${JSON.stringify(waiting)}`);
    }
    for (const id of ids) {
      if (done.has(id)) continue;
      const terminal = await client.terminals.get(id);
      const seen = JSON.stringify([terminal.state, Object.entries(conditions(terminal)).sort()]);
      if (last.get(id) !== seen) {
        last.set(id, seen);
        line(started, terminal);
      }
      if (until(terminal)) done.set(id, terminal);
    }
    await sleep(POLL_MS);
  }
  return ids.map((id) => done.get(id)!);
}

async function retire(client: MfxClient, names: string[]): Promise<void> {
  // Only the terminals named on the command line, and only those that are ACTIVE: a sample that
  // retired everything a tenant had would take its live terminals down with it.
  const named = await Promise.all(names.map((name) => client.terminals.get(name)));
  const active = named.filter((t) => t.state === "ACTIVE");
  if (!active.length) throw new Stop("None of the terminals named is ACTIVE, so there is nothing to retire.");
  const plan: PlanEntry[] = active.map((t) => ({
    was: t.id,
    farm_id: t.farm.id,
    broker_id: t.broker.id,
    broker: label(t.broker),
    server_id: t.server.id,
    server: label(t.server),
    streaming: t.streaming,
    execution: t.execution,
    instruments: [...t.instruments],
  }));
  writeFileSync(PLAN, JSON.stringify(plan, null, 2), "utf8");
  console.log(`Retiring ${active.length} terminal(s); what they were is in ${PLAN}`);

  for (const terminal of active) {
    // DELETE is an intent: the answer is DRAINING, and only the cell's word makes it RETIRED.
    const answer = (await client.write({
      method: "DELETE",
      path: `/terminals/${terminal.id}`,
      idempotencyKey: `retire-${crypto.randomUUID()}`,
    })) as Terminal;
    console.log(`  ${label(answer.broker)}: ${answer.state}`);
  }
  await watch(
    client,
    active.map((t) => t.id),
    (t) => t.state === "RETIRED",
    RETIRE_TIMEOUT_S,
  );
  console.log("PASS: every terminal drained on its cell and is RETIRED.");
}

async function request(client: MfxClient, instruments: string[]): Promise<void> {
  if (!existsSync(PLAN)) throw new Stop(`${PLAN} is missing: run \`retire\` first, or write it by hand.`);
  const plan = JSON.parse(readFileSync(PLAN, "utf8")) as PlanEntry[];
  const run_ = runId();
  const ids: string[] = [];
  for (const [index, entry] of plan.entries()) {
    const wanted = entry.instruments.length ? entry.instruments : instruments;
    // The key is fixed for this run: if the call is retried, the same terminal comes back.
    const terminal = await client.terminals.create({
      farmId: entry.farm_id,
      brokerId: entry.broker_id,
      serverId: entry.server_id,
      streaming: entry.streaming,
      execution: entry.execution,
      instruments: wanted,
      labels: { ...terminalLabels(), proof: run_ },
      idempotencyKey: `lifecycle-${run_}-${index}`,
    });
    console.log(`${entry.broker}: ${terminal.id} ${terminal.state} watching ${JSON.stringify(wanted)}`);

    const login = Number((await ask(`  ${entry.broker} login number: `)).trim());
    const server = (await ask(`  ${entry.broker} server [${entry.server}]: `)).trim();
    await client.write({
      method: "POST",
      path: `/terminals/${terminal.id}/credentials`,
      body: { login, password: await ask(`  ${entry.broker} password: `, true), server: server || entry.server },
      idempotencyKey: `lifecycle-${run_}-${index}-credential`,
    });
    ids.push(terminal.id);
  }

  const live = (terminal: Terminal): boolean => {
    if (terminal.state === "FAILED") throw new Stop(`FAILED: ${label(terminal.broker)} went FAILED`);
    const ok = conditions(terminal);
    return terminal.state === "ACTIVE" && LIVE.every((name) => ok[name] === "ok");
  };
  await watch(client, ids, live, ACTIVE_TIMEOUT_S);
  console.log("PASS: every terminal is ACTIVE, connected to its broker, with fresh ticks.");
}

run(async () => {
  const { values: args, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      terminal: { type: "string", multiple: true, default: [] },
      "key-id": { type: "string" },
      "private-key-file": { type: "string" },
      "base-url": { type: "string", default: BASE_URL },
      instruments: { type: "string", default: "" },
    },
  });
  const phase = positionals[0];
  if (phase !== "retire" && phase !== "request" && phase !== "all") {
    throw new Stop("say which phase: retire, request or all", 2);
  }
  const instruments = args.instruments!
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
  const client = new MfxClient(args["base-url"]!, keyId(args["key-id"]), privateKey(args["private-key-file"]));
  try {
    if (phase === "retire" || phase === "all") {
      const names = args.terminal ?? [];
      if (!names.length) throw new Stop("name the terminals to retire: --terminal <id>, once per terminal");
      await retire(client, names);
    }
    if (phase === "all") {
      // DECISION: no settle time is waited between retire and request. Each terminal asked for
      // again replaces one just RETIRED, so the login needs no session more than it had, and
      // RETIRED is the platform's word that the broker session is gone.
      await ask("Terminals are retired. Press Enter when the cell is ready for new ones... ");
    }
    if (phase === "request" || phase === "all") await request(client, instruments);
  } catch (refused) {
    // The platform's own words: what was refused, why, and the request id to quote.
    if (refused instanceof MfxApiError) throw new Stop(`REFUSED: ${apiText(refused)}`);
    throw refused;
  }
});
