---
question: "Is a retry safe?"
topic: developers
order: 10
guide: when-a-call-fails
---

Yes. Every write takes an `idempotency_key`, and none of them default it. Retrying with the same key returns the original answer; a fresh key asks for a new order or terminal. This is how a copier avoids a duplicated trade.
