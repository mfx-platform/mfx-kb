---
question: "Do you store my broker password?"
topic: security
order: 10
guide: changing-a-terminals-password
---

It travels once, over TLS, into a vault and is used by reference when your terminal logs in. No API returns it, the SDK renders it as `***` in every log line and traceback, and it cannot be printed by accident.
