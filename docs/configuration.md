# Configuration

BareCDP can be configured through JSON, environment variables, or direct Python constructor arguments.

Use `python -m bare_cdp --write-default-config bare-cdp.json` to generate a starter file.

Environment variables override JSON values:

- `BARE_CDP_HOST`
- `BARE_CDP_PORT`
- `BARE_CDP_WS_URL`
- `BARE_CDP_CHROME`
- `BARE_CDP_USER_DATA_DIR`
- `BARE_CDP_HEADLESS`
- `BARE_CDP_TIMEOUT`
