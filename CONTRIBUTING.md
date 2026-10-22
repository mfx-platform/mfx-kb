# Contributing to the MFX Hub knowledge base

Thank you for helping. A correction to a guide, a clearer explanation or a sample that does
something useful are all welcome.

## Before you start

- **A security problem?** Do not open an issue. Follow [SECURITY.md](SECURITY.md).
- **A question about your account, a terminal or the API?** Write to support@mfxhub.com. The
  documentation is at [docs.mfxhub.com](https://docs.mfxhub.com).
- **A guide that is wrong or unclear?** Open an issue naming the guide and the step, what you
  expected and what happened. Leave out API keys, broker logins and passwords, and anything else
  that identifies an account.

## What a change needs

- **Every sample runs.** Samples are run against MFX Hub's development environment in CI, so a
  sample must work exactly as the guide that quotes it says. `python -m compileall samples` passes.
- **A guide quotes real files.** A guide links to its sample by relative path
  (`../samples/name.py`), and CI fails if the file does not exist.
- **Secrets stay out.** Samples read the API key from the environment or a file you name, and ask
  for a broker password at a prompt. Never put a key, a login or a password in a sample, a guide or
  an issue.
- **Plain words.** Write for someone who has not used MFX Hub before: what to do, what they will
  see, and what to do when it goes wrong.

## Signing off your commits

Every commit must carry a Developer Certificate of Origin sign-off: a statement that you wrote the
change, or otherwise have the right to submit it under this project's licence. The full text is at
[developercertificate.org](https://developercertificate.org). Add it with `-s`:

```bash
git commit -s -m "docs: say which farm the first terminal is created on"
```

A pull request with an unsigned commit cannot be merged; `git commit --amend -s` or
`git rebase --signoff` fixes it.

## How a pull request lands

The knowledge base is written in a private repository alongside the platform it describes, and
each release is published here as a single commit. So a pull request here is not merged directly:

1. We review it here, in public, as usual.
2. Once it is approved, we apply it to the next release, keeping you as the commit's author and
   your sign-off.
3. When that release is published, we close the pull request with a link to it.

## Licence

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), the same as the rest of the project.

## Code of conduct

Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).
