// What every TypeScript sample here shares: the API key, the labels, a prompt, exact decimals, and
// one way to end. Each sample is still read top to bottom on its own; this file only keeps the
// plumbing out of the way.

import { readFileSync } from "node:fs";
import { createInterface, type Interface } from "node:readline";

import { MfxApiError, type MfxClient, Page } from "@mfxhub/sdk";

export const BASE_URL = process.env.MFX_BASE_URL ?? "https://api.dev.mfxhub.com/v1";

/** A sample stops here, with a sentence for the reader. Exit 1, like an error. */
export class Stop extends Error {
  readonly code: number;
  constructor(message: string, code = 1) {
    super(message);
    this.code = code;
  }
}

/** Ends the sample the same way whatever went wrong, so a runner reads every sample alike. */
export function run(main: () => Promise<number | void>): void {
  main().then(
    (code) => finish(code ?? 0),
    (error: unknown) => {
      if (error instanceof Stop) {
        process.stderr.write(`${error.message}\n`);
        finish(error.code);
        return;
      }
      if (error instanceof MfxApiError) {
        // The platform's own code and words, and its status: what was refused and why.
        process.stderr.write(`${error.name}: ${apiText(error)}\n`);
      } else {
        process.stderr.write(`${error instanceof Error ? (error.stack ?? error.message) : String(error)}\n`);
      }
      finish(1);
    },
  );
}

/**
 * End with `code` once what was printed has been handed over. Nothing forces the exit: a sample
 * closes its own stream (`stream.close()` ends a read that waits), so the process ends by itself.
 */
function finish(code: number): void {
  closePrompts();
  process.exitCode = code;
}

/** The next event, or null once `deadline` (epoch ms) has passed. */
export async function nextBefore<T>(next: () => Promise<T | null>, deadline: number): Promise<T | null> {
  const left = deadline - Date.now();
  if (left <= 0) return null;
  let timer: NodeJS.Timeout | undefined;
  const late = new Promise<null>((resolve) => {
    timer = setTimeout(() => resolve(null), left);
  });
  try {
    return await Promise.race([next(), late]);
  } finally {
    clearTimeout(timer);
  }
}

/** An API refusal in one line: `placement_error (409): <the platform's words>`. */
export function apiText(error: MfxApiError): string {
  return `${error.code} (${error.status}): ${error.message}`;
}

/** The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY. */
export function privateKey(file: string | undefined): string {
  if (file !== undefined) return readFileSync(file, "utf8").trim();
  const key = process.env.MFX_PRIVATE_KEY;
  if (!key) throw new Stop("give --private-key-file, or set MFX_PRIVATE_KEY");
  return key.trim();
}

/** The API key id: --key-id, else MFX_KEY_ID. */
export function keyId(given: string | undefined): string {
  const id = given ?? process.env.MFX_KEY_ID;
  if (!id) throw new Stop("give --key-id, or set MFX_KEY_ID");
  return id;
}

/** A value the sample needs from the environment, named in the guide. */
export function env(name: string): string {
  const value = process.env[name];
  if (!value) throw new Stop(`set ${name}`);
  return value;
}

/**
 * Labels for every terminal a sample asks for: MFX_TERMINAL_LABELS, a JSON object, when set.
 * Optional. Labels are yours to sort and find terminals by; nothing here needs one.
 */
export function terminalLabels(): Record<string, string> {
  const raw = JSON.parse(process.env.MFX_TERMINAL_LABELS || "{}") as Record<string, unknown>;
  return Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, String(value)]));
}

/** A short random string, so one run's idempotency keys belong together. */
export function runId(): string {
  return crypto.randomUUID().replaceAll("-", "").slice(0, 8);
}

export const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

/** How long to wait for a terminal you retired to read RETIRED. A drain takes seconds; this is only
 * the point at which the sample stops waiting and says so. */
export const RETIRE_WAIT_S = 120;
export const RETIRE_POLL_MS = 2000;

/**
 * Wait until a terminal you retired reads RETIRED, or is gone. False if not within `waitS`.
 *
 * Retiring is accepted at once and the terminal reads DRAINING; it keeps its broker session until
 * its cell has stopped it and it reads RETIRED. On a login whose server allows only so many
 * sessions, wait for RETIRED before you start another terminal on that login.
 */
export async function waitUntilRetired(
  client: MfxClient,
  terminalId: string,
  waitS = RETIRE_WAIT_S,
  pollMs = RETIRE_POLL_MS,
): Promise<boolean> {
  const deadline = Date.now() + waitS * 1000;
  for (;;) {
    try {
      const terminal = await client.terminals.get(terminalId);
      if (terminal.state === "RETIRED") return true;
    } catch (error: unknown) {
      if (error instanceof MfxApiError && error.status === 404) return true; // nothing left to hold a session
      throw error;
    }
    if (Date.now() >= deadline) return false;
    await sleep(pollMs);
  }
}

/** Every item of a paged list, following `next_cursor`. There is no total. */
export async function* everyItem<T>(
  client: MfxClient,
  path: string,
  query: Record<string, string | undefined> = {},
): AsyncGenerator<T, void, undefined> {
  let cursor: string | undefined;
  for (;;) {
    const body = (await client.read({ method: "GET", path, query: { ...query, cursor } })) as {
      items?: T[];
      next_cursor?: string | null;
    };
    yield* body.items ?? [];
    if (!body.next_cursor) return;
    cursor = body.next_cursor;
  }
}

/** One page of a list, as the SDK's own Page. */
export async function onePage<T>(
  client: MfxClient,
  path: string,
  query: Record<string, string | number | undefined> = {},
): Promise<Page<T>> {
  const body = (await client.read({ method: "GET", path, query })) as {
    items?: T[];
    next_cursor?: string | null;
    returned?: number;
  };
  return new Page<T>(body.items ?? [], body.next_cursor ?? null, body.returned ?? null);
}

// -- prompts -----------------------------------------------------------------------------------

let lines: Interface | null = null;
const waiting: ((line: string) => void)[] = [];
const ready: string[] = [];

function reader(): Interface {
  if (lines === null) {
    lines = createInterface({ input: process.stdin, terminal: false });
    lines.on("line", (line) => {
      const next = waiting.shift();
      if (next) next(line);
      else ready.push(line);
    });
    lines.on("close", () => {
      for (const next of waiting.splice(0)) next("");
    });
  }
  return lines;
}

function closePrompts(): void {
  lines?.close();
  lines = null;
}

/**
 * One line typed at a prompt. With `hidden`, what is typed is not shown: for a password, which is
 * never an argument, an environment variable or a file, so it reaches no shell history, no process
 * list and no CI log.
 */
export async function ask(question: string, hidden = false): Promise<string> {
  process.stderr.write(question);
  if (hidden && process.stdin.isTTY) return typedUnseen();
  reader();
  const line = ready.shift();
  if (line !== undefined) return line;
  return new Promise((resolve) => waiting.push(resolve));
}

function typedUnseen(): Promise<string> {
  return new Promise((resolve) => {
    const input = process.stdin;
    let typed = "";
    input.setRawMode(true);
    input.resume();
    input.setEncoding("utf8");
    const onData = (chunk: string): void => {
      for (const char of chunk) {
        if (char === "\r" || char === "\n" || char === "\u0004") {
          input.setRawMode(false);
          input.pause();
          input.off("data", onData);
          process.stderr.write("\n");
          resolve(typed);
          return;
        }
        if (char === "\u0003") process.exit(130);
        typed = char === "\u007f" || char === "\b" ? typed.slice(0, -1) : typed + char;
      }
    };
    input.on("data", onData);
  });
}

// -- exact decimals ------------------------------------------------------------------------------

/**
 * A decimal string, exactly. Prices, volumes and money arrive as strings and stay exact here:
 * `0.07 * 3` as numbers is `0.21000000000000002`, and a sizing rule or a balance must never
 * carry that error.
 */
export class Dec {
  readonly units: bigint;
  readonly scale: number;

  private constructor(units: bigint, scale: number) {
    this.units = units;
    this.scale = scale;
  }

  static of(text: string | number | null | undefined): Dec {
    const value = String(text ?? "").trim();
    const match = /^([+-]?)(\d*)(?:\.(\d*))?$/.exec(value);
    if (match === null || (match[2] === "" && (match[3] ?? "") === "")) {
      throw new RangeError(`${JSON.stringify(value)} is not a decimal`);
    }
    const fraction = match[3] ?? "";
    const units = BigInt(`${match[1]}${match[2] || "0"}${fraction}`);
    return new Dec(units, fraction.length);
  }

  /** A decimal string as a Dec, or null if it was not one. */
  static maybe(text: unknown): Dec | null {
    try {
      return Dec.of(text as string);
    } catch {
      return null;
    }
  }

  private at(scale: number): bigint {
    return this.units * 10n ** BigInt(scale - this.scale);
  }

  compare(other: Dec): number {
    const scale = Math.max(this.scale, other.scale);
    const a = this.at(scale);
    const b = other.at(scale);
    return a < b ? -1 : a > b ? 1 : 0;
  }

  minus(other: Dec): Dec {
    const scale = Math.max(this.scale, other.scale);
    return new Dec(this.at(scale) - other.at(scale), scale);
  }

  times(other: Dec): Dec {
    return new Dec(this.units * other.units, this.scale + other.scale);
  }

  isPositive(): boolean {
    return this.units > 0n;
  }

  /**
   * How many whole `step`s fit in `this / divisor`, as a multiple of `step`, rounded **down**:
   * rounding up would risk more than the budget, the one direction a sizing rule must never take.
   */
  dividedDownTo(divisor: Dec, step: Dec): Dec {
    // this / (divisor * step), floored, then times step; every value scaled to integers first.
    const scale = Math.max(this.scale, divisor.scale + step.scale);
    const top = this.at(scale);
    const bottom = divisor.units * step.units * 10n ** BigInt(scale - divisor.scale - step.scale);
    const steps = top / bottom; // BigInt division truncates; both are positive here
    return new Dec(steps * step.units, step.scale);
  }

  toString(): string {
    const negative = this.units < 0n;
    const digits = (negative ? -this.units : this.units).toString().padStart(this.scale + 1, "0");
    const whole = digits.slice(0, digits.length - this.scale);
    const fraction = this.scale ? `.${digits.slice(digits.length - this.scale)}` : "";
    return `${negative ? "-" : ""}${whole}${fraction}`;
  }
}

/** The larger of two decimals, kept as it was written. */
export const maxDec = (a: Dec, b: Dec): Dec => (b.compare(a) > 0 ? b : a);
export const minDec = (a: Dec, b: Dec): Dec => (b.compare(a) < 0 ? b : a);

// -- times -----------------------------------------------------------------------------------------

const two = (n: number): string => String(n).padStart(2, "0");

/** `2026-11-06 09:30`, in UTC. */
export function minuteUtc(at: string | Date): string {
  const d = new Date(at);
  return `${d.getUTCFullYear()}-${two(d.getUTCMonth() + 1)}-${two(d.getUTCDate())} ${two(d.getUTCHours())}:${two(d.getUTCMinutes())}`;
}

/** `09:30:12`, in UTC. */
export function secondUtc(at: string | Date): string {
  const d = new Date(at);
  return `${two(d.getUTCHours())}:${two(d.getUTCMinutes())}:${two(d.getUTCSeconds())}`;
}
