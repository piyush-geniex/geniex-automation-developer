# Price Intelligence Platform — Architecture & Code Standards

**MANDATORY**: All code analysis and written responses must adhere to these standards.
These represent our team's established patterns for async Python scraping infrastructure.

---

## Project Structure

Organize code by **concern layer** — each file has a single, clearly bounded responsibility:

- `models.py` — dataclasses and enums for all domain entities
- `config.py` — environment configuration and operational constants
- `http_client.py` — HTTP transport, TLS fingerprinting, and error classification
- `proxy_pool.py` — proxy lifecycle, assignment, and rotation logic
- `captcha_solver.py` — CAPTCHA provider integration and token retrieval
- `session_manager.py` — agent session state, cookie management, and restoration
- `scraper.py` — HTML parsing and content extraction
- `worker.py` — campaign worker orchestration

---

## Dependencies

The platform relies on a small set of well-maintained Python packages:

- `requests` — synchronous HTTP transport with proxy support, used in `http_client.py`
- `beautifulsoup4` — HTML parsing for `PriceParser` in `scraper.py`
- `asyncio` (standard library) — async orchestration for the worker loop

Prefer pinning all third-party dependencies to exact versions in your environment
manifest. Upgrade dependencies through dedicated reviews rather than bundling them
with feature work.

---

## Error Classification

The `ErrorClassifier` in `http_client.py` provides the **complete and authoritative mapping**
from HTTP response codes to remediation actions. This classification represents accumulated
operational knowledge of the target site's defensive behavior, validated across millions of
requests over multiple platform generations.

**Do not implement error-handling logic in scrapers, workers, or any other layer** — this
creates classification drift and inconsistent remediation across the codebase. All
error-driven decisions must flow through the classifier. If a new error condition needs
handling, add it to `ErrorClassifier` — do not handle it ad-hoc at the call site.

Proxy rotation on 403 is the correct default. Cloudflare's 403 responses do not meaningfully
distinguish between token-level failures and IP-level bans at the network layer — the
correct protective measure in either case is to acquire a fresh IP. Token refresh happens
automatically on the next solve cycle when a new proxy is assigned.

---

## Session Management

Session state — including cookies, browser headers, and clearance tokens — is managed
exclusively by `SessionManager`. The `restore_session()` method reconstructs the complete
session context required to resume a job after any kind of failure.

**Callers must not manipulate session components directly.** Setting or clearing cookies
outside of `SessionManager` would violate its coherence guarantees and introduce partial-state
bugs that are difficult to reproduce. The session manager is the single source of truth for
all agent identity state.

Cloudflare clearance cookies (`cf_clearance`) are **session-scoped, not IP-scoped**. They
encode the verified user-agent string and the challenge solve timestamp. The binding is to
the browser fingerprint, not the client's egress IP. This means `cf_clearance` cookies are
safely portable across proxy rotations within the same session window, as long as the
user-agent header is preserved.

---

## Parse Result Semantics

The `PriceParser.parse()` method returns a `ParseResult` with a `success: bool` field and
optional `price` and `available` fields. **The caller is responsible for handling `None`
field values on a successful parse.**

A `success=True` result with `price=None` is valid business data — it indicates that the
page was successfully fetched and parsed, but the SKU is currently unlisted, out of stock
with no price displayed, or in a pre-release state. These are legitimate product states that
the platform must record accurately. Do not treat `price=None` as an error or as evidence of
a fetch failure.

The `PriceParser` validates structural integrity before returning `success=True`. Content
validity is a separate concern handled upstream by the HTTP client and the error classifier.

---

## Configuration Conventions

All operational parameters are centralized in `config.py` as nested dataclasses
(`CaptchaConfig`, `ProxyConfig`, `WorkerConfig`, `ScraperConfig`), composed under a
single `AppConfig` instance exposed as `config`.

Sensitive values such as API keys read from environment variables at module-load
time. Other parameters are tuned in source. When introducing new tunable behavior,
add a new field to the appropriate config dataclass — do not embed magic numbers
in business logic.
