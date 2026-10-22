# Security policy

## Reporting a vulnerability

Please report a vulnerability in these guides and samples, in the MFX Hub SDK, or in the MFX Hub API, privately:

- through GitHub's **Report a vulnerability** button on this repository's Security tab, or
- by email to **security@mfxhub.com**.

Please do not open a public issue, pull request or discussion for a security problem.

Tell us what you found, how to reproduce it, and what an attacker could do with it. Do not include
real broker credentials or another person's data; if a proof needs a key, use one of your own
development keys and say so.

## What to expect

- We acknowledge a report within two business days.
- We tell you what we found and what we will do within ten business days.
- We fix what is confirmed and say when the fix is released. We credit you in the release notes if
  you want us to.

## Supported versions

The guides and samples on the default branch are maintained; fixes to the SDK itself are made to the
latest release of `mfx-sdk`.

## Handling credentials

The samples never print an API private key or a broker password, and the SDK never stores or logs one: `SigningKey` and `Secret`
render as `***` everywhere and cannot be pickled. If you believe a key has been exposed, write to
security@mfxhub.com so it can be revoked at once, and use a new one.
