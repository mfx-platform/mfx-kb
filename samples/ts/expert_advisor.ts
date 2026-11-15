// Run your own Expert Advisor on a terminal - the runnable half of guides/running-your-own-ea.md,
// in TypeScript.
//
// The whole path, in the order you meet it:
//
// 1. Upload the source. One .mq5 and the .mqh files it includes, as text. Never a compiled .ex5:
//    MFX Hub builds it for your farm's MetaTrader, after checking the source.
// 2. Wait for the verdict and read the report. Green is approved. Amber is held for a person to
//    look at. Red is refused, and the report gives the line and what to change.
// 3. Attach it to a terminal on a farm that runs customer code. MFX Hub restarts the terminal with
//    your expert on the chart you name, and follows that as an operation.
// 4. The trial. Your expert runs with trading off while MFX Hub measures what it costs: processor,
//    memory, chart objects, what it writes, the orders it would have sent and whether its heartbeat
//    arrives. A green trial goes live on a demo account by itself. A real-money account waits for
//    the code sent to your representative.
//
// Run it against dev:
//
//     node samples/ts/expert_advisor.ts --key-id mfxk_... --private-key-file key.txt \
//         --terminal-id <uuid> --symbol EURUSD --timeframe M1 --detach
//
// Without --terminal-id it uploads, waits for the verdict and prints the report, and attaches
// nothing. Your key needs ea:read and ea:write.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

import { ConflictError, MfxClient, type components } from "@mfxhub/sdk";

import { BASE_URL, keyId, privateKey, run, sleep } from "./common.ts";

type Version = components["schemas"]["TenantExpertAdvisor"];
type Report = components["schemas"]["EaReport"];
type Operation = components["schemas"]["TerminalOperation"];
type Accepted = components["schemas"]["OperationAccepted"];
type Attachment = Version["terminals"][number];

const HERE = dirname(fileURLToPath(import.meta.url));
/** How often the sample reads the trial while it runs. It lasts about half an hour. */
const TRIAL_POLL_MS = 30_000;
/** A version is settled by its checks in any of these. */
const SETTLED = new Set(["approved", "held", "rejected", "withdrawn", "revoked"]);
/** An operation is over in any of these; every other state is on its way. */
const OPERATION_FINAL = new Set(["SUCCEEDED", "FAILED", "NOT_STARTED", "EXPIRED", "CANCELLED", "SUPERSEDED"]);

const getVersion = async (client: MfxClient, id: string): Promise<Version> =>
  (await client.read({ method: "GET", path: `/expert-advisors/${id}` })) as Version;

/** Read the version until its checks have settled it, or until `timeoutS`. The last read either
 * way: a version still being checked when the time runs out is reported as it is. */
async function waitForVerdict(client: MfxClient, id: string, timeoutS: number): Promise<Version> {
  const deadline = Date.now() + timeoutS * 1000;
  let wait = 2000;
  let version = await getVersion(client, id);
  while (!SETTLED.has(version.state) && Date.now() < deadline) {
    await sleep(Math.min(wait, 15_000));
    wait *= 2;
    version = await getVersion(client, id);
  }
  return version;
}

/** Read the operation until it is over, or for 15 minutes; restart, attach and detach alike. */
async function waitForOperation(client: MfxClient, terminalId: string, operationId: string): Promise<Operation> {
  const read = async (): Promise<Operation> =>
    (await client.read({ method: "GET", path: `/terminals/${terminalId}/operations/${operationId}` })) as Operation;
  const deadline = Date.now() + 900_000;
  let wait = 2000;
  let operation = await read();
  while (!OPERATION_FINAL.has(operation.state) && Date.now() < deadline) {
    await sleep(Math.min(wait, 15_000));
    wait *= 2;
    operation = await read();
  }
  return operation;
}

run(async () => {
  const { values: args } = parseArgs({
    options: {
      "key-id": { type: "string" },
      "private-key-file": { type: "string" },
      "ea-file": { type: "string", default: join(HERE, "..", "ea", "mfx_sample_ea.mq5") },
      name: { type: "string", default: "mfx-sample-ea" },
      "terminal-id": { type: "string" },
      symbol: { type: "string", default: "EURUSD" },
      timeframe: { type: "string", default: "M1" },
      detach: { type: "boolean", default: false },
      "base-url": { type: "string", default: BASE_URL },
    },
  });
  const terminalId = args["terminal-id"];
  const client = new MfxClient(args["base-url"]!, keyId(args["key-id"]), privateKey(args["private-key-file"]));

  const source = readFileSync(args["ea-file"]!);
  // The same bundle uploaded again answers the version it already made, so a key made from the
  // bundle's own hash makes a re-run of this sample cost nothing.
  const bundle = createHash("sha256").update(source).digest("hex").slice(0, 16);
  let version = (await client.write({
    method: "POST",
    path: "/expert-advisors",
    body: { name: args.name, files: [{ path: basename(args["ea-file"]!), content_base64: source.toString("base64") }] },
    idempotencyKey: `upload-${bundle}`,
  })) as Version;
  console.log(`uploaded ${version.name} v${version.version}: ${version.state}`);

  version = await waitForVerdict(client, version.id, 900);
  const verdict = version.verdict ?? null;
  console.log(`verdict: ${verdict ?? "None"} (${version.state})`);
  if (verdict === null) {
    console.log("the checks have not finished; run again later");
    return;
  }
  const report = (await client.read({ method: "GET", path: `/expert-advisors/${version.id}/report` })) as Report;
  for (const finding of report.findings) {
    const where = finding.file ? `${finding.file}:${finding.line_from}` : "-";
    console.log(`  ${finding.colour.padEnd(5)} ${finding.rule_id.padEnd(7)} ${where.padEnd(20)} ${finding.why}`);
  }
  if (version.state !== "approved" || terminalId === undefined) return;

  let accepted: Accepted;
  try {
    accepted = (await client.write({
      method: "PUT",
      path: `/terminals/${terminalId}/expert-advisor`,
      body: {
        expert_advisor_id: version.id,
        symbol: args.symbol,
        timeframe: args.timeframe,
        reason: "run my expert advisor",
      },
      idempotencyKey: `attach-${crypto.randomUUID()}`,
    })) as Accepted;
  } catch (refused) {
    if (!(refused instanceof ConflictError) || refused.code !== "not_customer_code_farm") throw refused;
    // Only a farm that runs customer code carries an expert. The version is checked and kept:
    // attach it to a terminal on such a farm. Said as skipped, not failed: nothing is wrong.
    const notice = "not attached: this needs a terminal on a farm that runs customer code";
    console.log(notice);
    console.log(JSON.stringify({ outcome: "skipped", notice, detail: null }));
    return;
  }
  let operation = await waitForOperation(client, terminalId, accepted.operation_id);
  console.log(`attach: ${operation.state}`);
  if (operation.state !== "SUCCEEDED") return;

  // The trial is read on the version, under the terminal it runs on. Its figures are filled in
  // when it ends; a trial that does not pass ends with the expert taken off the terminal.
  type Trial = { state: string; verdict?: string | null; figures: { label: string; display?: string | null; grade?: string | null }[] };
  let trial: Trial | null = null;
  let here: Attachment | undefined;
  for (;;) {
    version = await getVersion(client, version.id);
    here = version.terminals.find((t) => t.terminal_id === terminalId);
    const seen = (here as { trial?: Trial | null } | undefined)?.trial;
    if (seen) trial = seen;
    if (here === undefined || here.state !== "trial") break;
    await sleep(TRIAL_POLL_MS);
  }
  // A trial that did not pass ends with the expert taken off the terminal; the version keeps it
  // as its last trial, with the reason, and the report carries what it found.
  const last = version.last_trial as { trial?: Trial | null; ended_reason?: string | null } | null | undefined;
  if (trial === null || here === undefined) trial = last?.trial ?? trial;
  if (trial !== null) {
    console.log(`trial: ${trial.state}, verdict ${trial.verdict ?? "None"}`);
    for (const figure of trial.figures) {
      console.log(`  ${figure.label.padEnd(28)} ${(figure.display || "-").padEnd(16)} ${figure.grade ?? "None"}`);
    }
  }
  if (here === undefined) {
    console.log(`now: not attached (${last?.ended_reason || "taken off"})`);
    const judged = ((await client.read({ method: "GET", path: `/expert-advisors/${version.id}/report` })) as Report)
      .trial as { findings?: { colour: string; rule_id: string; evidence?: string | null }[] } | null | undefined;
    for (const finding of judged?.findings ?? []) {
      console.log(`  ${finding.colour.padEnd(5)} ${finding.rule_id.padEnd(7)} ${finding.evidence ?? ""}`);
    }
  } else {
    console.log(`now: ${here.state}`);
  }

  if (args.detach && here !== undefined) {
    // Going live was itself a restart, and a terminal takes one operation at a time with a pause
    // between: queue=true lets the platform start the detach when it may.
    accepted = (await client.write({
      method: "DELETE",
      path: `/terminals/${terminalId}/expert-advisor`,
      query: { reason: "sample finished", queue: true },
      idempotencyKey: `detach-${crypto.randomUUID()}`,
    })) as Accepted;
    operation = await waitForOperation(client, terminalId, accepted.operation_id);
    console.log(`detach: ${operation.state}`);
  }
});
