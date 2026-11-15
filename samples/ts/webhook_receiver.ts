// A webhook receiver that checks every delivery - the runnable half of guides/receiving-webhooks.md,
// in TypeScript.
//
// MFX Hub POSTs each event you registered for to your HTTPS address, signed. This receiver does the
// three things every receiver must:
//
// 1. Verify the signature over the body exactly as it arrived. Parsing the JSON first and checking
//    a re-serialised copy changes the bytes, and the signature with them.
// 2. Answer 2xx quickly, and do the work afterwards. Anything else, or no answer in time, is a
//    failure and the event is sent again later.
// 3. De-duplicate on MFX-Event-Id. Delivery is at least once and in no promised order: the same
//    event can arrive twice, and a retried one can arrive after a later one.
//
// Run it (behind your own TLS-terminating proxy; MFX Hub only calls HTTPS addresses):
//
//     node samples/ts/webhook_receiver.ts --port 8080 --secret-file secret.txt
//
// During the day after you rotate the secret, both secrets sign each delivery. Pass both:
//
//     node samples/ts/webhook_receiver.ts --secret-file new.txt --secret-file old.txt
//
// Check the receiver without MFX Hub (it signs a delivery itself, the way MFX Hub does, and posts it):
//
//     node samples/ts/webhook_receiver.ts --self-check

import { createHmac, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { parseArgs } from "node:util";

import { Stop, run } from "./common.ts";

/** Event ids already handled. Bounded: an id older than this many deliveries is not sent again. */
const SEEN_LIMIT = 10_000;
/** How far the signed time may be from this clock, either way: what stops a replayed delivery. */
const TOLERANCE_S = 300;

/** Why a delivery is refused: malformed (no t or no v1), stale, or mismatch. */
class SignatureRefused extends Error {
  readonly reason: "malformed" | "stale" | "mismatch";
  constructor(reason: "malformed" | "stale" | "mismatch") {
    super(reason);
    this.reason = reason;
  }
}

/** Check one delivery's MFX-Signature against the body as received; return its signed time. */
function verify(body: Buffer, header: string, secrets: readonly string[], now = Date.now() / 1000): number {
  let stamp: number | null = null;
  const given: string[] = [];
  for (const part of header.split(",")) {
    const [name, value] = part.trim().split("=", 2);
    if (name === "t" && value && /^\d+$/.test(value)) stamp = Number(value);
    if (name === "v1" && value) given.push(value);
  }
  if (stamp === null || given.length === 0) throw new SignatureRefused("malformed");
  if (Math.abs(now - stamp) > TOLERANCE_S) throw new SignatureRefused("stale");
  const signed = Buffer.concat([Buffer.from(`${stamp}.`), body]);
  for (const secret of secrets) {
    const expected = Buffer.from(createHmac("sha256", secret).update(signed).digest("hex"));
    for (const value of given) {
      const candidate = Buffer.from(value);
      if (candidate.length === expected.length && timingSafeEqual(candidate, expected)) return stamp;
    }
  }
  throw new SignatureRefused("mismatch");
}

function receiver(secrets: readonly string[], handled: unknown[]): Server {
  const seen = new Set<string>();
  return createServer((request: IncomingMessage, answer) => {
    if (request.method !== "POST") {
      answer.writeHead(405).end();
      return;
    }
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      const body = Buffer.concat(chunks);
      try {
        verify(body, String(request.headers["mfx-signature"] ?? ""), secrets);
      } catch (refused) {
        answer.writeHead(400).end();
        console.log(`refused: ${refused instanceof SignatureRefused ? refused.reason : "malformed"}`);
        return;
      }
      answer.writeHead(204).end(); // answer first; the work below must not hold the reply
      const eventId = String(request.headers["mfx-event-id"] ?? "");
      if (seen.has(eventId)) {
        console.log(`duplicate ${eventId}, attempt ${String(request.headers["mfx-delivery-attempt"])}`);
        return;
      }
      seen.add(eventId);
      // A Set keeps the order things were added: the oldest id goes first.
      while (seen.size > SEEN_LIMIT) seen.delete(seen.values().next().value as string);
      const event = JSON.parse(body.toString("utf8")) as { type?: string };
      handled.push(event);
      console.log(`${eventId} ${event.type}`);
    });
  });
}

/** How MFX Hub signs a delivery: HMAC-SHA256, keyed with the secret's text, over `<t>.<body>`. */
function sign(body: Buffer, secret: string, at: number): string {
  const digest = createHmac("sha256", secret)
    .update(Buffer.concat([Buffer.from(`${at}.`), body]))
    .digest("hex");
  return `t=${at},v1=${digest}`;
}

async function post(url: string, body: Buffer, signature: string, eventId: string): Promise<number> {
  const answer = await fetch(url, {
    method: "POST",
    body: new Uint8Array(body),
    headers: {
      "Content-Type": "application/json",
      "MFX-Signature": signature,
      "MFX-Event-Id": eventId,
      "MFX-Event-Type": "webhook.test",
      "MFX-Delivery-Attempt": "1",
    },
  });
  return answer.status;
}

/** Start the receiver on a free local port, send it a good, a duplicate, a tampered and an old
 * delivery, and say what it did with each. */
async function selfCheck(): Promise<void> {
  const secret = "a-secret-for-the-self-check-only";
  const handled: unknown[] = [];
  const server = receiver([secret], handled);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
  const body = Buffer.from(JSON.stringify({ type: "webhook.test", schema_version: "1", id: "1-0" }));
  const now = Math.floor(Date.now() / 1000);
  const results = {
    good: await post(url, body, sign(body, secret, now), "1-0"),
    duplicate: await post(url, body, sign(body, secret, now), "1-0"),
    tampered: await post(url, Buffer.from(body.toString().replace("test", "tost")), sign(body, secret, now), "2-0"),
    "ten minutes old": await post(url, body, sign(body, secret, now - 600), "3-0"),
  };
  server.close();
  console.log(results);
  const expected = { good: 204, duplicate: 204, tampered: 400, "ten minutes old": 400 };
  if (JSON.stringify(results) !== JSON.stringify(expected)) throw new Stop("self-check failed");
  if (handled.length !== 1) throw new Stop("self-check failed: the duplicate was handled twice");
  console.log("self-check passed");
}

run(async () => {
  const { values } = parseArgs({
    options: {
      port: { type: "string", default: "8080" },
      "secret-file": { type: "string", multiple: true, default: [] },
      "self-check": { type: "boolean", default: false },
    },
  });
  if (values["self-check"]) {
    await selfCheck();
    return;
  }
  const files = values["secret-file"] ?? [];
  if (files.length === 0) throw new Stop("--secret-file is needed (or --self-check)", 2);
  const secrets = files.map((path) => readFileSync(path, "utf8").trim());
  console.log(`listening on :${values.port}`);
  receiver(secrets, []).listen(Number(values.port), "0.0.0.0");
  await new Promise(() => {}); // serve until stopped
});
