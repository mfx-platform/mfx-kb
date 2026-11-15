// Follow one terminal's account live: a snapshot, then the marks that move it - the runnable half
// of guides/following-an-account-live.md, in TypeScript.
//
// It subscribes to one terminal's account channel, merges what arrives into a single current view,
// and prints a line whenever a number moves:
//
//     node samples/ts/account_live.ts --key-id mfxk_... \
//         --private-key-file C:\secure\mfx-dev.key --terminal 0191f0c2-...
//
// Your key needs the scope acct:read, and the terminal must be yours.
//
// The point of the sample is the merge, so it is worth saying what it is not doing. It does not
// ask for the account over REST and it does not poll: everything printed below arrived on the
// stream. It does not rebuild its view from each message either - a marks message sets three
// numbers on one position, which is what makes this affordable at a 250 ms refresh rate and with
// hundreds of positions open.
//
// LiveAccount below applies the four rules in the guide. The loop is what using it looks like in
// full: apply every event, draw when something changed, and resume when it says you are behind.
//
// Amounts arrive as decimal strings ("10012.40"), never numbers, so they are printed as they came
// and compared exactly. Do the same: a balance through a float is a balance that can be wrong in
// the last place.

import { parseArgs } from "node:util";

import { Channel, ChannelRefused, MfxClient, Resync, StreamMessage, type StreamEvent } from "@mfxhub/sdk";

import { BASE_URL, Dec, Stop, keyId, nextBefore, privateKey, run } from "./common.ts";

/** What the guide calls stale: nothing at all for 15 s with positions open, three missed
 * keep-alives. A client that draws its own screen has to notice it on its own. */
const STALE_AFTER_S = 15;
/** As the account messages name them: `margin_free`, not `free_margin`. */
const FIGURES = ["balance", "equity", "margin", "margin_free", "margin_level"] as const;
const SNAPSHOT = "account.snapshot";
const MARKS = "account.marks";
/** Carried by a message but not part of the account's figures. */
const NOT_A_FIGURE = new Set(["kind", "type", "seq", "base", "as_of", "terminal_id", "schema_version", "marks"]);
/** A snapshot's fields that belong to the position set; only another snapshot replaces them. */
const SET = new Set(["positions", "pending", "pending_as_of", "open_positions_count"]);
/** What a mark carries after its ticket, in order. */
const MARKED = ["price_current", "profit", "swap"] as const;

type Row = Record<string, unknown>;

interface AccountView {
  readonly seq: number | null;
  readonly base: number | null;
  readonly figures: Readonly<Row>;
  readonly positions: ReadonlyMap<number, Row>;
}

/**
 * The stream's ordering rules, applied to the events handed to it: every snapshot replaces the
 * view; a marks message applies only to the snapshot it names, in order, to tickets the view has;
 * anything else means the view is behind, and only a snapshot puts it right.
 */
class LiveAccount {
  view: AccountView | null = null;
  behind = false;

  readonly terminalId: string;

  constructor(terminalId: string) {
    this.terminalId = terminalId;
  }

  apply(event: StreamEvent): AccountView | null {
    if (event instanceof ChannelRefused || event instanceof Resync) {
      if (!event.channel.endsWith(this.terminalId)) return null;
      // The channel is gone, or the replay window passed: what is held may be arbitrarily old.
      this.view = null;
      this.behind = true;
      return null;
    }
    if (!(event instanceof StreamMessage)) return null;
    const named = event.data.terminal_id;
    if (named !== undefined && named !== null && String(named) !== this.terminalId) return null;
    if (!event.channel.endsWith(this.terminalId)) return null;
    const kind = event.type ?? String(event.data.kind ?? "");
    if (kind === SNAPSHOT) return this.draw(event);
    if (kind === MARKS) return this.mark(event);
    return null;
  }

  private draw(message: StreamMessage): AccountView {
    const base = baseOf(message);
    const positions = new Map<number, Row>();
    for (const row of rows(message.data.positions)) {
      if ("ticket" in row) positions.set(Number(row.ticket), { ...row });
    }
    this.behind = false;
    this.view = { seq: message.seq, base: base ?? message.seq, figures: figures(message.data), positions };
    return this.view;
  }

  private mark(message: StreamMessage): AccountView | null {
    const view = this.view;
    if (view === null) {
      this.behind = true;
      return null;
    }
    const base = baseOf(message);
    const marks = readMarks(message.data.marks);
    const gapped = view.seq !== null && message.seq !== null && message.seq > view.seq + 1;
    if (
      (base !== null && base !== view.base) ||
      gapped ||
      marks === null ||
      marks.some(([ticket]) => !view.positions.has(ticket))
    ) {
      this.behind = true;
      return null;
    }
    const positions = new Map([...view.positions].map(([ticket, row]) => [ticket, { ...row }]));
    for (const [ticket, values] of marks) {
      const row = positions.get(ticket)!;
      MARKED.forEach((name, i) => (row[name] = values[i]));
    }
    this.view = {
      seq: message.seq ?? view.seq,
      base: view.base,
      figures: { ...view.figures, ...figures(message.data) },
      positions,
    };
    return this.view;
  }
}

function baseOf(message: StreamMessage): number | null {
  for (const value of [message.base, message.data.base]) {
    if (value !== null && value !== undefined) return Number(value);
  }
  return null;
}

function figures(data: Readonly<Row>): Row {
  return Object.fromEntries(Object.entries(data).filter(([name]) => !NOT_A_FIGURE.has(name) && !SET.has(name)));
}

function rows(value: unknown): Row[] {
  return Array.isArray(value) ? value.filter((row): row is Row => typeof row === "object" && row !== null) : [];
}

/** `marks` as [ticket, [price_current, profit, swap]] pairs, or null if a mark is not that shape.
 * An empty list is a keep-alive and applies cleanly; an unreadable one is not an empty one. */
function readMarks(value: unknown): [number, unknown[]][] | null {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) return null;
  const out: [number, unknown[]][] = [];
  for (const mark of value) {
    if (!Array.isArray(mark) || mark.length !== 4) return null;
    out.push([Number(mark[0]), mark.slice(1)]);
  }
  return out;
}

function figuresLine(view: AccountView): string {
  return FIGURES.filter((name) => name in view.figures)
    .map((name) => `${name}=${String(view.figures[name])}`)
    .join("  ");
}

/** Each open position's floating profit, worst first - which is the order anyone looks in. */
function positionsLine(view: AccountView): string {
  const profit = (row: Row): Dec => Dec.maybe(row.profit) ?? Dec.of("0");
  return [...view.positions.values()]
    .sort((a, b) => profit(a).compare(profit(b)))
    .map(
      (row) =>
        `${String(row.symbol ?? "?")}#${String(row.ticket)} ${String(row.side ?? "?")}` +
        ` ${String(row.volume ?? "?")} @ ${String(row.price_current ?? "?")} (${String(row.profit ?? "?")})`,
    )
    .join("  ");
}

/** Follow until stopped, or for `seconds`; the number of messages applied. */
async function follow(client: MfxClient, terminalId: string, seconds: number): Promise<number> {
  const channel = Channel.account(terminalId);
  const live = new LiveAccount(terminalId);
  const started = Date.now();
  let lastAt = Date.now();
  let said = false;
  let applied = 0;

  const stream = await client.stream();
  // Staleness cannot be noticed in the message loop, because the thing being noticed is the
  // absence of a message: it takes its own timer.
  const watchdog = setInterval(() => {
    const quiet = (Date.now() - lastAt) / 1000;
    if (quiet > STALE_AFTER_S && !said && live.view !== null && live.view.positions.size) {
      said = true;
      console.log(`! stale - nothing for ${quiet.toFixed(0)}s with positions open; these are not live`);
    }
  }, 1000);
  const deadline = seconds ? started + seconds * 1000 : Number.POSITIVE_INFINITY;
  try {
    await stream.subscribe(channel);
    console.log(`following ${terminalId} on ${channel} - the first snapshot arrives on subscribe`);
    for (;;) {
      const event = await nextBefore(() => stream.next(), deadline);
      if (event === null) break;
      const updated = live.apply(event);
      if (updated === null) {
        if (event instanceof Resync) console.log("! the replay window passed; asking for a fresh snapshot");
        if (live.behind) {
          // Rule 3. The answer on this channel is a snapshot, never a replay, so this costs one
          // message and puts the view back on its feet at once.
          console.log("! behind - a message could not be applied; resuming");
          await stream.resume(channel);
        }
        continue;
      }
      // Any message counts, including a keep-alive carrying no marks at all: that is how a still
      // account proves it is still connected.
      lastAt = Date.now();
      said = false;
      applied += 1;
      const elapsed = String(Math.floor((Date.now() - started) / 1000)).padStart(5);
      const kind = updated.seq === updated.base ? "snapshot" : "marks   ";
      console.log(`[${elapsed}s] ${kind} seq=${updated.seq} ${figuresLine(updated)}`);
      if (updated.positions.size) console.log(`          ${positionsLine(updated)}`);
      if (seconds && Date.now() - started >= seconds * 1000) break;
    }
  } finally {
    clearInterval(watchdog);
    await stream.close();
  }
  return applied;
}

run(async () => {
  const { values: args } = parseArgs({
    options: {
      "key-id": { type: "string" },
      "private-key-file": { type: "string" },
      terminal: { type: "string" },
      seconds: { type: "string", default: "0" },
      "base-url": { type: "string", default: BASE_URL },
    },
  });
  if (!args.terminal) throw new Stop("--terminal is needed", 2);
  const seconds = Number(args.seconds);
  const client = new MfxClient(args["base-url"]!, keyId(args["key-id"]), privateKey(args["private-key-file"]));
  const heard = await follow(client, args.terminal, seconds);
  if (seconds) {
    // The snapshot arrives on subscribe, so a bounded run that heard nothing at all is a fault.
    console.log(`${heard} message(s) in ${seconds} s`);
    return heard ? 0 : 1;
  }
});
