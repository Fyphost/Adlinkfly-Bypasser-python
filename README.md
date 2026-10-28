# AdLinkFly Resolver API

A production-oriented **Python 3.12 / FastAPI** service that resolves supported
[AdLinkFly](https://codecanyon.net/item/adlinkfly-monetized-url-shortener/11144136)-based
shortened URLs (for example `arolinks.com`) to their final destination.

It combines a fast **cloudscraper** HTTP path with a **Playwright (Chromium)**
browser fallback for JavaScript-heavy or multi-step pages, and returns a
consistent JSON result. CAPTCHA and human-verification challenges are reported
honestly rather than faked.

> ⚠️ **Authorised use only.** This tool automates navigation of intermediate
> pages. Only use it against shortener services you are **permitted** to
> automate (your own, or where you have explicit authorisation), and respect
> each target's Terms of Service and `robots.txt`. The default supported-domain
> allowlist is intentionally small; you are responsible for what you add to it.
> The service will **not** attempt to solve CAPTCHAs or defeat anti-bot systems
> that require human verification — it returns `verification_required` instead.

---

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Requirements](#requirements)
- [Quickstart (local)](#quickstart-local)
- [Quickstart (Docker)](#quickstart-docker)
- [Configuration](#configuration)
- [API reference](#api-reference)
- [Status & HTTP codes](#status--http-codes)
- [Security](#security)
- [Supported shorteners & limitations](#supported-shorteners--limitations)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Features

- **Intelligent resolution engine** — inspects HTTP responses, HTML, JavaScript
  redirects and page metadata; follows validated redirects; extracts the
  destination when possible.
- **cloudscraper fast path** — handles compatible HTTP requests and the
  anti-bot challenges cloudscraper supports, with a fresh session per request.
- **Playwright browser fallback** — drives Chromium for JS-rendered pages,
  countdowns and multi-step "Continue / Get Link / Verify" flows using explicit
  conditions and auto-waiting locators (no arbitrary sleeps).
- **Honest CAPTCHA handling** — detects reCAPTCHA / hCaptcha / Turnstile and
  returns `verification_required` instead of pretending to succeed.
- **Security first** — strict URL validation, SSRF protection (private,
  loopback, link-local and cloud-metadata ranges), IPv4-mapped-IPv6 unwrapping,
  redirect-by-redirect re-validation, API-key auth, and rate limiting.
- **Isolation** — a fresh cloudscraper session and a fresh browser context per
  request, so cookies/state never leak between callers.
- **Operable** — structured JSON logging with secret redaction, a `/health`
  endpoint, configurable timeouts, concurrency limits, and a Docker image.

## How it works

```
         ┌──────────────────────────── POST /api/resolve ───────────────────────────┐
         │                                                                           │
  API key + rate limit ──► URL validation (syntax + DNS + SSRF) ──► supported-domain check
         │                                                                           │
         ▼                                                                           ▼
  ┌─────────────────┐   destination found & valid    ┌───────────────────────────────┐
  │  cloudscraper   │ ─────────────────────────────► │         resolved ✅            │
  │  (HTTP path)    │                                 └───────────────────────────────┘
  └───────┬─────────┘
          │ needs JS / intermediate steps / countdown / challenge
          ▼
  ┌─────────────────┐   analyse page ▸ decide action ▸ click / wait / extract
  │ Playwright      │ ─────────────────────────────────────────────────────────►  resolved ✅
  │ (browser agent) │   CAPTCHA / human step detected  ───────────────────────►  verification_required 🔐
  └─────────────────┘   nav limit / nothing actionable ───────────────────────►  failed ❌
```

Every candidate destination — whether from the HTTP path or the browser — is
re-validated against the SSRF rules before being returned.

## Project structure

```
app/
  main.py          FastAPI app, routes, middleware, error handlers, lifespan
  resolver.py      Orchestration: validation → cloudscraper → browser fallback
  http_client.py   cloudscraper integration (manual, validated redirects)
  browser_agent.py Playwright automation + pure navigation-decision policy
  page_analyzer.py HTML/page-state analysis (BeautifulSoup + pure helpers)
  url_validator.py URL + network (SSRF) validation — pure standard library
  schemas.py       Pydantic request/response models
  config.py        Environment-based settings (pydantic-settings)
  security.py      API-key auth + rate-limit dependencies
  ratelimit.py     Sliding-window rate limiter (pure standard library)
  status.py        Resolution status enum
  exceptions.py    Typed domain exceptions
  logging_config.py Structured logging with secret redaction
tests/             Unit tests + controlled pages for live browser tests
Dockerfile         Container image (Python 3.12 + Chromium)
docker-compose.yml
requirements.txt / requirements-dev.txt
.env.example
```

## Requirements

- Python **3.12+**
- The Python packages in `requirements.txt`
- A **Chromium** browser installed via Playwright (`playwright install chromium`)

## Quickstart (local)

```bash
# 1) Use Python 3.12 (example with pyenv)
pyenv install -s 3.12
pyenv local 3.12

# 2) Create a virtualenv and install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt      # or requirements.txt for runtime only

# 3) Install the Chromium browser for Playwright
playwright install --with-deps chromium

# 4) Configure
cp .env.example .env
#   edit .env and set at least API_KEYS (and SUPPORTED_DOMAINS as needed)

# 5) Run
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open the interactive docs at <http://localhost:8000/docs>.

> For quick local experimentation without auth, set `ENVIRONMENT=development`
> and leave `API_KEYS` empty — auth is then disabled (development only).

## Quickstart (Docker)

```bash
cp .env.example .env            # set API_KEYS etc.
docker build -t adlinkfly-resolver:latest .
docker run --rm -p 8000:8000 --shm-size=1g --env-file .env adlinkfly-resolver:latest
# or:
docker compose up --build
```

The image installs Chromium and its OS dependencies and runs as a non-root
user. `--shm-size=1g` avoids Chromium crashes from the small default `/dev/shm`.

## Configuration

All settings are read from environment variables (or a `.env` file). List/set
values accept a comma-separated string or a JSON array.

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `production` | `development` \| `staging` \| `production`. |
| `LOG_LEVEL` | `INFO` | Logging level. |
| `LOG_JSON` | `true` | Emit structured JSON logs. |
| `API_KEYS` | _(empty)_ | Comma-separated API keys. Required in production. |
| `REQUIRE_API_KEY` | `true` | Enforce API-key auth. Fails closed in production. |
| `API_KEY_HEADER` | `X-API-Key` | Header carrying the API key. |
| `RATE_LIMIT_ENABLED` | `true` | Enable per-identifier rate limiting. |
| `RATE_LIMIT_REQUESTS` | `60` | Requests allowed per window. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate-limit window length. |
| `HTTP_CONNECT_TIMEOUT` | `10` | cloudscraper connect timeout (s). |
| `HTTP_READ_TIMEOUT` | `20` | cloudscraper read timeout (s). |
| `MAX_REDIRECTS` | `10` | Max redirects followed on the HTTP path. |
| `MAX_RESPONSE_BYTES` | `3000000` | Cap on response body read. |
| `HTTP_PHASE_TIMEOUT` | `20` | Wall-clock cap (s) for the cloudscraper phase before falling back to the browser. |
| `CLOUDSCRAPER_DELAY` | `3` | cloudscraper challenge-solving delay (s). |
| `USER_AGENT` | _(Chrome UA)_ | User agent for HTTP + browser. |
| `ENABLE_BROWSER` | `true` | Enable the Playwright fallback. |
| `BROWSER_HEADLESS` | `true` | Run Chromium headless. |
| `BROWSER_NAVIGATION_TIMEOUT_MS` | `20000` | Per-navigation timeout. |
| `BROWSER_MAX_STEPS` | `8` | Max navigation steps per resolve. |
| `BROWSER_COUNTDOWN_MAX_SECONDS` | `20` | Max time to wait out a countdown. |
| `BROWSER_SETTLE_TIMEOUT_MS` | `2500` | Short bounded settle wait after an action (replaces `networkidle`). |
| `BROWSER_TIME_MARGIN_SECONDS` | `2` | Margin reserved so the browser returns before the overall timeout fires. |
| `BROWSER_SLOW_MO_MS` | `0` | Slow-motion delay (debugging). |
| `RESOLVE_TOTAL_TIMEOUT` | `60` | Overall per-request time budget (s). |
| `MAX_CONCURRENT_RESOLVES` | `10` | Global in-flight resolve limit. |
| `MAX_CONCURRENT_BROWSERS` | `2` | Concurrent browser contexts limit. |
| `SUPPORTED_DOMAINS` | `arolinks.com` | Authorised shortener domains (allowlist). |
| `ALLOW_PRIVATE_NETWORKS` | `false` | Relax SSRF checks (dev/test only!). |
| `MAX_URL_LENGTH` | `2048` | Maximum accepted URL length. |
| `CORS_ALLOW_ORIGINS` | _(empty)_ | Allowed CORS origins. |

## API reference

### `POST /api/resolve`

Headers: `X-API-Key: <your key>` (when auth is enabled).

Request body:

```json
{ "url": "https://arolinks.com/example" }
```

Optional: `"include_meta": true` adds a non-sensitive `meta` object
(`resolved_via`, `steps`, `elapsed_ms`).

Successful response (`200`):

```json
{
  "success": true,
  "original_url": "https://arolinks.com/example",
  "destination_url": "https://example.com",
  "status": "resolved",
  "message": "Destination URL resolved successfully"
}
```

Verification-required response (`200`):

```json
{
  "success": false,
  "original_url": "https://arolinks.com/example",
  "destination_url": null,
  "status": "verification_required",
  "message": "Human verification is required to continue"
}
```

Example:

```bash
curl -s -X POST http://localhost:8000/api/resolve \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"url": "https://arolinks.com/example"}'
```

### `GET /health`

Returns service and browser readiness — useful for load balancers:

```json
{
  "status": "ok",
  "app": "AdLinkFly Resolver API",
  "version": "1.0.0",
  "environment": "production",
  "browser_enabled": true,
  "browser_ready": true
}
```

### `GET /`

Redirects to the Swagger UI at `/docs`. OpenAPI JSON is at `/openapi.json`.

## Status & HTTP codes

The `status` field is always one of:

| `status` | Meaning | HTTP code |
|---|---|---|
| `resolved` | Destination found and validated. | `200` |
| `verification_required` | CAPTCHA / human step blocks automation. | `200` |
| `failed` | Automated resolution exhausted without a destination. | `200` |
| `timeout` | Exceeded the overall time budget. | `200` |
| `invalid_url` | Malformed URL, bad scheme, or SSRF-blocked. | `400` |
| `unsupported_domain` | Host not in the allowlist. | `422` |

Transport-level errors share the same JSON shape (`success:false`, `status`,
`message`):

| Condition | `status` | HTTP code |
|---|---|---|
| Missing / invalid API key | `unauthorized` | `401` |
| Rate limit exceeded | `rate_limited` | `429` (+ `Retry-After`) |
| Malformed request body | `invalid_request` | `422` |
| Unexpected server error | `internal_error` | `500` |

**Design note:** a `200` means *"we ran the resolver and here is the verdict"*
(including `verification_required`/`failed`). A `4xx`/`5xx` means the request
itself was rejected or the server erred.

## Security

- **URL validation** — only `http`/`https`, no embedded credentials, length
  capped; hostnames resolved and every IP checked.
- **SSRF protection** — rejects loopback, private, link-local, multicast,
  reserved and non-global addresses, plus the cloud metadata endpoint
  `169.254.169.254` (and the IPv6 variant). IPv4-mapped IPv6 addresses are
  unwrapped so they cannot smuggle a blocked target.
- **Redirect-by-redirect validation** — the HTTP client follows redirects
  manually and re-validates each hop (mitigating DNS-rebinding). The browser
  blocks in-page requests to private/metadata IP literals, and the final
  destination is always re-validated.
- **Authentication** — API-key header with constant-time comparison; fails
  closed in production when enforcement is on but no keys are configured.
- **Rate limiting** — in-process sliding window per API key / client IP. For
  multi-instance deployments, front it with a shared limiter or API gateway.
- **Logging** — structured, with cookies/authorization/API keys redacted.

## Supported shorteners & limitations

- **Supported** — AdLinkFly-based shorteners on domains you place in
  `SUPPORTED_DOMAINS` (default: `arolinks.com`). Detection of page states is
  heuristic and version-tolerant rather than tied to brittle fixed selectors.
- **CAPTCHAs are not solved.** When a reCAPTCHA / hCaptcha / Turnstile or other
  human-verification step is required, the API returns `verification_required`.
  Integrating a third-party, authorised CAPTCHA-solving service is intentionally
  out of scope.
- cloudscraper does **not** bypass every anti-bot system; the browser fallback
  helps with JS/interaction but cannot defeat human verification.
- The destination heuristic prefers strong signals (meta-refresh, JS redirects,
  known link elements) and treats unrelated outbound/ad links as weak
  candidates only.

## Testing

```bash
pip install -r requirements-dev.txt
playwright install --with-deps chromium   # only needed for live browser tests

pytest                 # unit tests (mocked; no network)
RUN_BROWSER_TESTS=1 pytest -m browser   # opt-in live browser tests
ruff check .           # lint
mypy app               # type-check
```

- Unit tests mock the network and browser. The security-critical
  `url_validator`, the `ratelimit` limiter, the `http_client` redirect/SSRF
  handling, the page-analysis helpers and the browser **decision policy** are
  covered by **pure standard-library tests** that run without any third-party
  dependency installed.
- Tests that need `pydantic` / `bs4` / `fastapi` skip automatically if those
  packages are not installed, so the suite never errors in a minimal
  environment.
- Live browser tests (`-m browser`, enabled with `RUN_BROWSER_TESTS=1`) serve
  controlled pages from `tests/pages/` over `127.0.0.1` — they do not depend on
  any third-party shortener staying unchanged.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `browser_ready: false` in `/health` | Chromium not installed — run `playwright install --with-deps chromium`. In Docker this is built in. |
| Chromium crashes in Docker | Increase shared memory: `--shm-size=1g` (already set in compose). |
| Every request returns `401` | Auth is enforced but no/incorrect key — set `API_KEYS` and send `X-API-Key`. |
| Everything returns `verification_required` | The target genuinely requires a CAPTCHA/human step; it cannot be automated. |
| `unsupported_domain` | Add the domain to `SUPPORTED_DOMAINS` (only if authorised). |
| `invalid_url` for a public URL | Check scheme is `http(s)`, no embedded credentials, and DNS resolves to a public IP. |
| `status: "timeout"` | Send `"include_meta": true` to see `resolved_via` (which phase stalled). Tune `HTTP_PHASE_TIMEOUT`, `BROWSER_NAVIGATION_TIMEOUT_MS`, `BROWSER_MAX_STEPS`, `RESOLVE_TOTAL_TIMEOUT`. The resolver splits the budget between a bounded cloudscraper phase and a deadline-bounded browser phase and avoids open-ended `networkidle` waits, so timeouts should be rare and always report their phase. |

## License

[MIT](./LICENSE). Provided "as is". You are responsible for using it lawfully
and only against services you are authorised to automate.
