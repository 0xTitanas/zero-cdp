# Security Policy

Chrome remote debugging is powerful: anyone who can reach a debugging endpoint can effectively control that browser profile. ZeroCDP is a small client for controlled local automation; it is not a sandbox or a browser hardening layer.

## Supported versions

| Version | Security support |
| --- | --- |
| `0.2.x` | Current supported line |
| `< 0.2.0` | Best-effort only |

## Reporting a vulnerability

Please do not publish exploit details in a public issue.

Preferred path:

1. Use GitHub private vulnerability reporting for this repository if it is available.
2. If private vulnerability reporting is not available, open a public issue with only a brief, non-sensitive summary and ask for a private disclosure channel.
3. Include the affected version, operating system, Chrome/Chromium version, a minimal reproduction, and whether a Chrome remote-debugging endpoint was exposed beyond `127.0.0.1`.

Do not include cookies, tokens, personal browser data, screenshots of authenticated pages, or raw dumps from authenticated sessions.

## Security posture

ZeroCDP:

- talks to a Chrome/Chromium DevTools Protocol endpoint chosen by the caller or launched by ZeroCDP;
- uses `127.0.0.1` defaults for local debugging;
- creates disposable temporary Chrome profiles by default in `launch_chrome()` when no profile is supplied;
- rejects launch-mode overrides for ownership-critical flags such as `--remote-debugging-port`, `--remote-debugging-address`, and `--user-data-dir`;
- does not request, persist, or manage user credentials.

Callers remain responsible for protecting browser profiles, debug ports, authenticated page data, logs, screenshots, and any automation scripts that interact with sensitive pages.

For operational guidance, see [docs/security.md](docs/security.md).
