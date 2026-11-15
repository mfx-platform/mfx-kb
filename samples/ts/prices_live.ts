// Follow a symbol's prices live, and know when they stop - the runnable half of
// guides/following-prices-live.md, in TypeScript.
//
// Three things to take away:
//
// 1. Two steps, in this order. A subscription is your standing request for a symbol on a broker's
//    server. A channel on the stream is where its ticks arrive. Open the channel without the
//    subscription and it answers not_subscribed.
// 2. A quiet channel proves nothing. A market that is shut, a terminal that is gone and a symbol
//    the broker stopped quoting all look the same from the channel: nothing arrives. The
//    subscription says which it is, in its state, and tells you the moment it changes with two
//    events.
// 3. One of your terminals must be watching the symbol. The prices are your broker's, read by your
//    own terminals. A subscription asks for them to be streamed to you; it does not put the symbol
//    on a terminal.
//
// Run it against dev:
//
//     node samples/ts/prices_live.ts --key-id mfxk_... --private-key-file key.txt \
//         --server-id <uuid> --symbol EURUSD.sd --seconds 30
//
// Add --subscribe to create the subscription first (your key needs md:write for that; following
// one that exists needs only md:read).

import { parseArgs } from "node:util";

import { Channel, ChannelRefused, MfxClient, Resync, StreamMessage, type components } from "@mfxhub/sdk";

import { BASE_URL, Stop, everyItem, keyId, nextBefore, privateKey, run } from "./common.ts";

type Subscription = components["schemas"]["MdSubscription"];

/** What each reason means for you. The reason is the platform's own word and new ones can appear:
 * treat one you do not know as "not delivering" and read the subscription. */
const WHAT_TO_DO: Record<string, string> = {
  symbol_removed: "your broker no longer lists the symbol; unsubscribe",
  subscription_refused: "your broker refused the symbol on your terminals; check its name",
  terminal_disconnected: "you have no ACTIVE terminal on this server; bring one back",
  report_stale: "no terminal of yours is reporting the symbol; is it in a terminal's instruments?",
  no_ticks_in_session: "the market is open and the broker is not quoting; nothing to do but wait",
};

async function mine(client: MfxClient, serverId: string, symbol: string): Promise<Subscription[]> {
  // The SDK has no method for subscriptions yet; this is the same signed call it would make.
  const found: Subscription[] = [];
  for await (const s of everyItem<Subscription>(client, "/md/subscriptions")) {
    if (s.server.id === serverId && s.symbol === symbol) found.push(s);
  }
  return found;
}

run(async () => {
  const { values: args } = parseArgs({
    options: {
      "key-id": { type: "string" },
      "private-key-file": { type: "string" },
      "server-id": { type: "string" },
      symbol: { type: "string" },
      seconds: { type: "string", default: "30" },
      subscribe: { type: "boolean", default: false },
      "base-url": { type: "string", default: BASE_URL },
    },
  });
  const serverId = args["server-id"];
  const symbol = args.symbol;
  if (!serverId || !symbol) throw new Stop("--server-id and --symbol are needed", 2);
  const seconds = Number(args.seconds);
  const client = new MfxClient(args["base-url"]!, keyId(args["key-id"]), privateKey(args["private-key-file"]));

  // -- step one: the subscription ---------------------------------------------------------------
  if (args.subscribe) {
    // Subscribing again with the same idempotency key returns the same subscription. It outlives
    // this script: it is yours until you unsubscribe, and it counts against your plan.
    await client.write({
      method: "POST",
      path: "/md/subscriptions",
      body: { server_id: serverId, symbol },
      idempotencyKey: `prices-${crypto.randomUUID()}`,
    });
  }
  const found = await mine(client, serverId, symbol);
  const subscription = found[0];
  if (subscription === undefined) {
    console.log(`no subscription for ${symbol} on that server; run again with --subscribe`);
    return;
  }
  // `pending` until the first tick; `streaming`; or `degraded`, with the reason and the moment.
  console.log(`${symbol}: ${subscription.state}`);
  if (subscription.degraded_reason) {
    const reason = subscription.degraded_reason;
    console.log(`  degraded since ${subscription.degraded_since}: ${reason}`);
    console.log(`  -> ${WHAT_TO_DO[reason] ?? "not delivering; the reason is new to this sample"}`);
  }

  // -- step two: the channel, and the events that say when it stops -------------------------------
  let ticks = 0;
  let last = null as Readonly<Record<string, unknown>> | null;
  const stream = await client.stream();
  try {
    await stream.subscribe(Channel.md(serverId, symbol));
    await stream.subscribe(Channel.events);
    const deadline = Date.now() + seconds * 1000;
    const follow = async (): Promise<void> => {
      for (;;) {
        const message = await nextBefore(() => stream.next(), deadline);
        if (message === null) return;
        if (message instanceof ChannelRefused) {
          // About one channel only; the rest of the stream carries on.
          console.log(`refused ${message.channel}: ${message.code} ${message.message}`);
          continue;
        }
        if (message instanceof Resync) continue; // older than the replay window: read over REST
        if (!(message instanceof StreamMessage)) continue;
        const data = message.data;
        if (message.type === "md.subscription_degraded") {
          console.log(`DEGRADED ${String(data.symbol)} since ${String(data.since)}: ${String(data.reason)}`);
          console.log(`  last tick ${String(data.last_tick_at)}`);
          console.log(`  -> ${WHAT_TO_DO[String(data.reason)] ?? "not delivering"}`);
        } else if (message.type === "md.subscription_recovered") {
          const gap = (Date.parse(String(data.recovered_at)) - Date.parse(String(data.since))) / 1000;
          console.log(`RECOVERED ${String(data.symbol)} after ${gap.toFixed(0)} s of ${String(data.reason)}`);
        } else if (message.channel.startsWith("md:")) {
          ticks += 1;
          last = data;
          // bid and ask are decimal strings: keep them as strings, every digit kept.
          if (ticks <= 3) console.log(`  tick bid ${String(data.bid)} ask ${String(data.ask)}`);
        }
      }
    };
    await follow();
  } finally {
    await stream.close();
  }

  const tail = last ? `; last bid ${String(last.bid)} ask ${String(last.ask)}` : "";
  console.log(`${ticks} ticks in ${seconds} s${tail}`);
  if (!ticks) {
    // Silence is not a diagnosis. The subscription is.
    const now = await mine(client, serverId, symbol);
    const state = now[0]?.state ?? "gone";
    console.log(`no tick arrived. The subscription reads ${state}: a shut market is not degraded`);
  }
});
