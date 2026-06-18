# Security

Chrome remote debugging gives powerful control over the browser profile. Keep it local.

Recommended defaults:

- Bind to `127.0.0.1` only.
- Use a dedicated automation profile.
- Do not expose the debugging port to a network.
- Do not log cookies, tokens, local storage, or authenticated page dumps.
- Use disposable profiles for untrusted pages and CI.
