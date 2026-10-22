"""A webhook receiver that checks every delivery - the runnable half of guides/receiving-webhooks.md.

MFX Hub POSTs each event you registered for to your HTTPS address, signed. This receiver does the
three things every receiver must:

1. **Verify the signature over the body exactly as it arrived**, with ``mfx.verify``. Parsing the
   JSON first and checking a re-serialised copy changes the bytes, and the signature with them.
2. **Answer 2xx quickly**, and do the work afterwards. Anything else, or no answer in time, is a
   failure and the event is sent again later.
3. **De-duplicate on ``MFX-Event-Id``.** Delivery is at least once and in no promised order: the
   same event can arrive twice, and a retried one can arrive after a later one.

Run it (behind your own TLS-terminating proxy; MFX Hub only calls HTTPS addresses):

    python samples/webhook_receiver.py --port 8080 --secret-file secret.txt

During the day after you rotate the secret, both secrets sign each delivery. Pass both:

    python samples/webhook_receiver.py --secret-file new.txt --secret-file old.txt

Check the receiver without MFX Hub (it signs a delivery itself, the way MFX Hub does, and posts it):

    python samples/webhook_receiver.py --self-check
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mfx import WebhookSignatureError, verify

#: Event ids already handled. Bounded: an id older than this many deliveries is not sent again.
SEEN_LIMIT = 10_000


class Receiver(BaseHTTPRequestHandler):
    secrets: list[str] = []
    seen: OrderedDict[str, None] = OrderedDict()
    handled: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            verify(body, self.headers.get("MFX-Signature", ""), self.secrets)
        except WebhookSignatureError as refused:
            self.send_response(400)
            self.end_headers()
            print(f"refused: {refused.reason}")
            return
        self.send_response(204)  # answer first; the work below must not hold the reply
        self.end_headers()
        event_id = self.headers.get("MFX-Event-Id", "")
        if event_id in self.seen:
            print(f"duplicate {event_id}, attempt {self.headers.get('MFX-Delivery-Attempt')}")
            return
        self.seen[event_id] = None
        while len(self.seen) > SEEN_LIMIT:
            self.seen.popitem(last=False)
        event = json.loads(body)
        self.handled.append(event)
        print(f"{event_id} {event.get('type')}")

    def log_message(self, *args: object) -> None:  # quiet: the lines above say what happened
        return


def _sign(body: bytes, secret: str, at: int) -> str:
    """How MFX Hub signs a delivery: HMAC-SHA256, keyed with the secret's text, over `<t>.<body>`."""
    digest = hmac.new(secret.encode(), str(at).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={at},v1={digest}"


def _post(url: str, body: bytes, signature: str, event_id: str) -> int:
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "MFX-Signature": signature,
        "MFX-Event-Id": event_id, "MFX-Event-Type": "webhook.test", "MFX-Delivery-Attempt": "1",
    })  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=5) as answer:  # noqa: S310 - our own loopback
            return answer.status
    except urllib.error.HTTPError as answer:
        return answer.code


def self_check() -> None:
    """Start the receiver on a free local port, send it a good, a duplicate, a tampered and an
    old delivery, and say what it did with each."""
    secret = "a-secret-for-the-self-check-only"
    Receiver.secrets = [secret]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    body = json.dumps({"type": "webhook.test", "schema_version": "1", "id": "1-0"}).encode()
    now = int(time.time())
    results = {
        "good": _post(url, body, _sign(body, secret, now), "1-0"),
        "duplicate": _post(url, body, _sign(body, secret, now), "1-0"),
        "tampered": _post(url, body.replace(b"test", b"tost"), _sign(body, secret, now), "2-0"),
        "ten minutes old": _post(url, body, _sign(body, secret, now - 600), "3-0"),
    }
    server.shutdown()
    print(results)
    assert results == {"good": 204, "duplicate": 204, "tampered": 400, "ten minutes old": 400}
    assert len(Receiver.handled) == 1, "the duplicate was answered and not handled twice"
    print("self-check passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--secret-file", type=Path, action="append", default=[],
                        help="the endpoint's secret as it was shown; twice after a rotation")  # fmt: skip
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if not args.secret_file:
        parser.error("--secret-file is needed (or --self-check)")
    Receiver.secrets = [path.read_text().strip() for path in args.secret_file]
    print(f"listening on :{args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Receiver).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
