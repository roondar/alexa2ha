# Alexa to Home Assistant Shopping List Sync

This service polls the Alexa shopping list, forwards incomplete items to a
Home Assistant webhook, and marks them complete in Alexa only after Home
Assistant accepts them.

## Requirements

- Docker and Docker Compose
- Python 3.14.7 for local development and CI
- Home Assistant with a webhook automation and the `todo.shopping_list`
  entity
- `alexa_media_player` v5.15.6 or newer, which exports an
  `alexapy.cookies` JSON file

Pickle cookie files are deliberately rejected. This avoids executing an
untrusted serialization format; regenerate the cookie file with the current
Alexa integration if necessary.

## Configuration

Copy `.env.example` to `.env` and set at least:

```dotenv
HA_WEBHOOK_URL=http://homeassistant.local:8123/api/webhook/your-webhook-id
AMAZON_URL=https://www.amazon.fr
COOKIE_PATH=/cookie.cookies
```

The service validates URLs, the cookie file, logging, and all positive numeric
settings before starting. The default poll interval is 60 seconds. The
optional `--interval` command-line argument overrides
`POLL_INTERVAL_SECONDS`.

`STATE_PATH` points to a small SQLite registry. Once Home Assistant confirms an
item, its Amazon identifier is stored before the Amazon completion call. If
Amazon is temporarily unavailable, the next cycle retries completion without
posting a duplicate item to Home Assistant. `HEARTBEAT_PATH` is updated after
successful cycles and is used by the Docker healthcheck.

## Docker Compose

Set the host cookie path and start the service:

```bash
export ALEXA_COOKIES_PATH=/srv/home-assistant/config/.storage/alexa_media.cookies
docker compose up -d --build
docker compose logs -f scraper
```

Compose stores the SQLite state in a named volume and runs the image as an
unprivileged user. It does not mount the source tree into the production
container. The Home Assistant automation can use:

```yaml
alias: Alexa shopping list
trigger:
  - platform: webhook
    webhook_id: your-webhook-id
    allowed_methods: [POST]
    local_only: true
action:
  - action: todo.add_item
    target:
      entity_id: todo.shopping_list
    data:
      item: "{{ trigger.json.name }}"
mode: single
```

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env  # edit paths and URLs
python main.py --interval 60
```

Run the checks with `ruff check .`, `mypy main.py`, and `python -m pytest -q`.

## Reliability and security behavior

- Every HTTP operation has connect/read timeouts. Amazon GET/PUT requests use
  bounded retries for 429 and transient 5xx responses; the webhook POST is not
  automatically retried because that could duplicate an item.
- SIGTERM and SIGINT stop the polling loop promptly, which makes Docker
  restarts graceful.
- Cookie contents and webhook payloads are never logged. Keep `.env` and the
  cookie file outside version control.
- The SQLite registry is intended for one running instance. Exactly-once
  delivery cannot be guaranteed if the process crashes in the tiny interval
  between Home Assistant's response and the SQLite commit; Home Assistant-side
  idempotency is required for that absolute guarantee.

## Releases and container tags

The GitHub Actions workflow runs checks for pull requests and for pushes to
`main` and `beta/*`. It publishes to GHCR only for the following refs:

- `vX.Y.Z` publishes `X.Y.Z`, `X.Y`, `X`, and `latest`, then creates a stable
  GitHub Release with generated notes.
- `vX.Y.Z-alpha.N`, `vX.Y.Z-beta.N`, and `vX.Y.Z-rc.N` publish only the exact
  version tag and create a GitHub prerelease. They never move `latest`.
- `beta/<name>` publishes `beta-<name>`,
  `beta-<name>-sha-<12-character-sha>`, and the shared `beta` tag. Branch names
  use letters, numbers, `.`, `_`, and `-`; the Docker slug is lower-case.

The shared `beta` tag is intentionally mobile. Beta workflows use a common
concurrency group so a newer beta push cancels an older one before it can
replace the alias. The SHA tag remains available to pin an exact build.

Tags beginning with `v` that do not match one of the formats above fail before
GHCR login or publication. Do not create release tags from an unverified local
working tree: run the checks, commit to `main`, and then create and push the
version tag, for example:

```bash
git tag v1.2.3
git push origin v1.2.3
```
