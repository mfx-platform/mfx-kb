"""Run your own Expert Advisor on a terminal - the runnable half of guides/running-your-own-ea.md.

The whole path, in the order you meet it:

1. **Upload the source.** One ``.mq5`` and the ``.mqh`` files it includes, as text. Never a
   compiled ``.ex5``: MFX Hub builds it for your farm's MetaTrader, after checking the source.
2. **Wait for the verdict and read the report.** Green is approved. Amber is held for a person to
   look at. Red is refused, and the report gives the line and what to change.
3. **Attach it** to a terminal on a farm that runs customer code. MFX Hub restarts the terminal
   with your expert on the chart you name, and follows that as an operation.
4. **The trial.** Your expert runs with trading off while MFX Hub measures what it costs: processor,
   memory, chart objects, what it writes, the orders it would have sent and whether its heartbeat
   arrives. A green trial goes live on a demo account by itself. A real-money account waits for the
   code sent to your representative.

Run it against dev:

    python samples/expert_advisor.py --key-id mfxk_... --private-key-file key.txt \
        --terminal-id <uuid> --symbol EURUSD --timeframe M1 --detach

Without --terminal-id it uploads, waits for the verdict and prints the report, and attaches nothing.
Your key needs ``ea:read`` and ``ea:write``.
"""

from __future__ import annotations

import argparse
import os
import asyncio
import hashlib
import uuid
from pathlib import Path

from mfx import MfxClient, SigningKey

BASE_URL = "https://api.dev.mfxhub.com/v1"
HERE = Path(__file__).resolve().parent
#: How often the sample reads the trial while it runs. It lasts about half an hour.
TRIAL_POLL_S = 30

def private_key(args: argparse.Namespace) -> str:
    """The API private key: the file named by --private-key-file, else MFX_PRIVATE_KEY."""
    if args.private_key_file is not None:
        return str(args.private_key_file.read_text(encoding="utf-8").strip())
    key = os.environ.get("MFX_PRIVATE_KEY")
    if not key:
        raise SystemExit("give --private-key-file, or set MFX_PRIVATE_KEY")
    return key.strip()



async def run(client: MfxClient, args: argparse.Namespace) -> None:
    source = args.ea_file.read_text(encoding="utf-8")
    # The same bundle uploaded again answers the version it already made, so a key made from the
    # bundle's own hash makes a re-run of this sample cost nothing.
    bundle = hashlib.sha256(source.encode()).hexdigest()[:16]
    version = await client.expert_advisors.upload(
        args.name, {args.ea_file.name: source}, idempotency_key=f"upload-{bundle}"
    )
    print(f"uploaded {version.name} v{version.version}: {version.state.value}")

    version = await client.expert_advisors.wait_for_verdict(version.id, timeout_s=900)
    verdict = version.verdict.value if version.verdict else None
    print(f"verdict: {verdict} ({version.state.value})")
    if verdict is None:
        print("the checks have not finished; run again later")
        return
    report = await client.expert_advisors.report(version.id)
    for finding in report.findings:
        where = f"{finding.file}:{finding.line_from}" if finding.file else "-"
        print(f"  {finding.colour.value:5} {finding.rule_id:7} {where:20} {finding.why}")
    if version.state.value != "approved" or args.terminal_id is None:
        return

    accepted = await client.terminals.attach_expert_advisor(
        args.terminal_id,
        version.id,
        symbol=args.symbol,
        timeframe=args.timeframe,
        reason="run my expert advisor",
        idempotency_key=f"attach-{uuid.uuid4()}",
    )
    operation = await client.terminals.wait_for_operation(args.terminal_id, accepted.operation_id)
    print(f"attach: {operation.state.value}")
    if operation.state.value != "SUCCEEDED":
        return

    # The trial is read on the version, under the terminal it runs on. Its figures are filled in
    # when it ends; a trial that does not pass ends with the expert taken off the terminal.
    trial = None
    while True:
        version = await client.expert_advisors.get(version.id)
        here = next((t for t in version.terminals if str(t.terminal_id) == str(args.terminal_id)), None)
        trial = getattr(here, "trial", None) or trial if here is not None else trial
        if here is None or here.state.value != "trial":
            break
        await asyncio.sleep(TRIAL_POLL_S)
    # A trial that did not pass ends with the expert taken off the terminal; the version keeps it
    # as its last trial, with the reason, and the report carries what it found.
    last = getattr(version, "last_trial", None)
    if trial is None or here is None:
        trial = getattr(last, "trial", None) or trial
    if trial is not None:
        print(f"trial: {trial.state.value}, verdict {trial.verdict.value if trial.verdict else None}")
        for figure in trial.figures:
            grade = getattr(figure.grade, "value", figure.grade)
            print(f"  {figure.label:28} {figure.display or '-':16} {grade}")
    if here is None:
        reason = getattr(getattr(last, "ended_reason", None), "value", None)
        print(f"now: not attached ({reason or 'taken off'})")
        judged = (await client.expert_advisors.report(version.id)).trial
        for finding in getattr(judged, "findings", None) or []:
            print(f"  {finding.colour.value:5} {finding.rule_id:7} {finding.evidence or ''}")
    else:
        print(f"now: {here.state.value}")

    if args.detach and here is not None:
        accepted = await client.terminals.detach_expert_advisor(
            args.terminal_id,
            reason="sample finished",
            idempotency_key=f"detach-{uuid.uuid4()}",
            # Going live was itself a restart, and a terminal takes one operation at a time with a
            # pause between: queue=True lets the platform start the detach when it may.
            queue=True,
        )
        operation = await client.terminals.wait_for_operation(args.terminal_id, accepted.operation_id)
        print(f"detach: {operation.state.value}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--key-id", default=os.environ.get("MFX_KEY_ID"), help="or MFX_KEY_ID")
    p.add_argument("--private-key-file", type=Path, help="or the key itself in MFX_PRIVATE_KEY")
    p.add_argument("--ea-file", type=Path, default=HERE / "ea" / "mfx_sample_ea.mq5")
    p.add_argument("--name", default="mfx-sample-ea")
    p.add_argument("--terminal-id", help="a terminal on a farm that runs customer code")
    p.add_argument("--symbol", default="EURUSD", help="the broker's own name for the chart's symbol")
    p.add_argument("--timeframe", default="M1")
    p.add_argument("--detach", action="store_true", help="take the expert off again at the end")
    p.add_argument("--base-url", default=BASE_URL)
    return p


async def main() -> None:
    args = parser().parse_args()
    key = SigningKey(private_key(args))
    async with MfxClient(args.base_url, args.key_id, key) as client:
        await run(client, args)


if __name__ == "__main__":
    asyncio.run(main())
