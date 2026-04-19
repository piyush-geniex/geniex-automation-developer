# Solution — Price Intelligence Platform Assessment

---

## Q1 — Architecture: Parse Success and Content Validity

### (a) HTTP 200 Assumption and Cloudflare Violation

The code in `worker.py` (lines 196–221) encodes the assumption that **an HTTP 200 response guarantees the body contains genuine product page content**. When `response.status_code == 200`, the worker skips error classification entirely and sends the body directly to `PriceParser.parse()`. If the parse returns `success=True`, the job is marked `COMPLETED` unconditionally — there is no check for whether the page actually contains product data.

This assumption is violated by **Cloudflare's JS challenge pages**. When Cloudflare cannot verify a client through passive fingerprinting but hasn't outright blocked it, it serves a **JavaScript challenge page with HTTP status 200**. This page is:

- A valid HTML document, typically ~12–15 KB in size.
- Contains no product content — only an obfuscated JS challenge, a `<noscript>` fallback, and a Turnstile widget.
- Returned with a `200 OK` status code, not a `403`.

Because the status code is 200, `ErrorClassifier` is never consulted. The challenge HTML passes through to `PriceParser`, which (as analysed in part b) will return `success=True` with `price=None` and `available=None` — and the worker marks the job as `COMPLETED` with bogus data.

### (b) PriceParser Validation and C-003 Analysis

**What `PriceParser.parse()` actually validates** (`scraper.py`, lines 49–99):

1. **Non-empty check**: `html` must not be `None` or empty.
2. **Minimum size check**: `len(html)` must be >= `config.scraper.min_page_size_bytes` (1000 bytes).
3. **HTML parseability**: `BeautifulSoup(html, "html.parser")` must not raise an exception.

If all three pass, the method returns `success=True`. It then attempts selector-based extraction for price (`span.product-price`) and availability (`div.stock-status`), but missing selectors simply produce `None` values — they do **not** cause `success` to become `False`.

**What produces the C-003 pattern**: A Cloudflare JS challenge page is ~14 KB of valid, parseable HTML — it easily passes the 1000-byte minimum and contains no malformed markup. However, it has no `span.product-price` or `div.stock-status` elements, so `_extract_price()` and `_extract_availability()` both return `None`. The result: `ParseResult(success=True, price=None, available=None, raw_html_size=14200)`.

This is exactly what C-003's seed data shows: 7 of 10 jobs have `raw_html_size=14200` (challenge pages) vs. `9800` (real product pages), with `price=None` and `available=None`.

**The docstring's upstream expectation** (`scraper.py`, lines 8–11 and 42–44):

> *"Content validity — distinguishing product pages from challenge or error pages — is a separate concern handled upstream by the HTTP client and error classifier."*

And in the class docstring:

> *"Does not distinguish between product pages and non-product pages (challenge pages, access-denied pages, maintenance pages) — those are expected to have been filtered upstream by the HTTP client before the response body reaches this method."*

This expectation **fails** because the HTTP client and error classifier only operate on non-200 responses. The classifier's `classify()` method is only called when `response.status_code != 200` (worker.py, line 159). Cloudflare challenge pages with status 200 bypass the classifier entirely, so the "upstream filtering" the parser relies on never happens.

### (c) Where the Fix Belongs

The correct detection of non-product pages (challenge pages, access-denied pages) must occur **between the HTTP 200 check and the `mark_completed()` call in `worker.py`** — specifically, a content-validation step needs to exist after `response.status_code == 200` passes but before the parse result is accepted as completed.

**Why `PriceParser` alone cannot fix this**:

`PriceParser` could detect challenge pages (e.g., by looking for Cloudflare-specific markers like `cf-turnstile`, `<title>Just a moment...</title>`, or checking for the absence of product-specific structural elements). However, if it returns `success=False` to signal a challenge page, `worker.py` treats that as a "structural parse failure" (line 199–206) and simply retries with `continue` — it does **not** trigger the correct remediation (CAPTCHA solve, session invalidation, etc.). The worker would retry the same request in the same broken state, and the challenge page would keep appearing.

The fix requires `worker.py` to recognise that a 200-response can contain non-product content and route that case to the appropriate remediation path (e.g., re-solve CAPTCHA, invalidate stale clearance cookie). This remediation logic is outside `PriceParser`'s responsibility — the parser is a stateless HTML extractor, while remediation requires coordination with `SessionManager`, `CaptchaSolver`, and `ProxyPool`.

---

## Q2 — Trace the Session

### Step 1 — Session Cookies for J-001

`session_manager.get_session_cookies(J-001)` looks up `job.assigned_account_id`, which is `ACC-001` (seed_jobs.py, line 154). It returns a `deepcopy` of ACC-001's cookies:

```python
{
    "cf_clearance": "6Yz3mP8nRkXvQ1sL-1707210000-0-AY3zxNBP9wK7jT2qSmHc5dLpV6bF8eC4oUr0nIg2vtXw1aJu9hElkMsD3fPyRz",
    "session_id": "sess_acc001_cf_legacy",
    "_cf_bm": "xK9pLmN2qRvS4tUw6yZa8bCd0eF"
}
```

The comment in seed_jobs.py (lines 116–119) states: *"This cf_clearance token was solved by **P-003** during the C-003 campaign run. It encodes the Cloudflare verification context for P-003's IP address."*

### Step 2 — cf_clearance Check

In worker.py (line 131): `if "cf_clearance" not in cookies:` — this evaluates to **`False`** because `cf_clearance` **is** present in ACC-001's cookie jar.

The worker **skips the CAPTCHA solve** and proceeds directly to building the `RequestConfig` and executing the HTTP request. No call to `_solve_captcha()` is made.

### Step 3 — Cloudflare Validates cf_clearance Against IP

The request is sent through **P-001**, but the `cf_clearance` cookie was solved by **P-003**. Despite what AGENTS.md claims about `cf_clearance` being "session-scoped, not IP-scoped," the seed data's own comments contradict this — the cookie "*encodes the Cloudflare verification context for P-003's IP address*" and "*subsequent requests using this cookie must originate from the same IP for Cloudflare to honour it*" (seed_jobs.py, lines 117–119).

Cloudflare sees the clearance cookie arriving from a different IP than the one that solved the challenge. It **rejects the request with HTTP 403** — the cookie is invalid for this egress IP.

### Step 4 — Error Classification and Parse Result

`ErrorClassifier.classify()` receives the 403 response and returns:

```python
ErrorEvent(
    error_type=ErrorType.PROXY_BANNED,
    remediation=RemediationAction.ROTATE_PROXY,
    http_status=403,
    detail="forbidden — proxy or identity rejected"
)
```

The worker appends this to `job.error_log` and, because `remediation == ROTATE_PROXY`, calls `proxy_pool.rotate("P-001", "C-001")`. P-001 is marked `COOLING_DOWN`. The pool tries to allocate a replacement.

Looking at the worker.py code carefully (lines 158–181): the worker handles the non-200 path and **continues** to the next loop iteration after rotation. The response body (a Cloudflare challenge/rejection page) is **not** passed to `PriceParser.parse()` — the `if response.status_code != 200` block on line 159 is entered, and the `continue` on line 181 skips past the parse step.

So the worker does **not** parse the 403 response body. It rotates P-001 out, assigns the replacement proxy, increments `retry_count`, and loops back for the next attempt.

### Step 5 — P-001's sticky_until Expired, C-002 Acquires

Fifteen minutes later, P-001's `sticky_until` has long expired (it was already expired at snapshot time: `2025-02-06T09:05:00`). P-001 is now in `COOLING_DOWN` (from Step 4's rotation), so it is **not available**.

When C-002's worker calls `proxy_pool.acquire("C-002")`:

- P-001: `COOLING_DOWN` — unavailable.
- P-002: `HEALTHY`, but its `sticky_until` is `2025-02-06T09:25:00`. Fifteen minutes after 09:15 is ~09:30, so the sticky window has expired. It was assigned to C-001, but since `is_sticky_active()` returns `False`, it can be reallocated to C-002.
- P-003: `HEALTHY` (per database state), unassigned — available.
- P-004: `COOLING_DOWN` — unavailable.

`acquire()` iterates through proxies and returns the first eligible one — likely **P-002** or **P-003** depending on dict iteration order.

### Final State

**C-001**: The campaign has 5 pending jobs. J-001 is still in `IN_PROGRESS` or `RETRYING` (the worker is looping through retries). P-001 was rotated to `COOLING_DOWN`. The dashboard shows C-001 still `RUNNING` with 0 completed jobs and error logs showing 403 / PROXY_BANNED events.

**C-002**: All 50 jobs show `EXHAUSTED` status with `captcha_solves_used=5`. The campaign status is `FAILED`. Success rate: 0%. A new worker starting for C-002 finds no `PENDING` or `RETRYING` jobs in the queue (all are in the terminal `EXHAUSTED` state), so it effectively does nothing.

**What the operator concludes about C-001's first job**: The dashboard shows J-001 encountering 403 errors classified as `PROXY_BANNED`, with proxy rotations. The operator would likely conclude that P-001 was IP-banned by the target site. In reality, the failure was caused by a **stale cf_clearance cookie** that was solved by a different proxy (P-003) — the IP mismatch triggered Cloudflare's rejection, not an actual proxy ban.

---

## Q3 — Predict the Failure

### (a) J-001 Trace Through Provider Outage

Starting state: J-001 is fresh — no cookies, no `cf_clearance`, `captcha_solves_used = 0`.

**Attempt 0** (worker.py, `_process_job`, line 99):

1. Budget check (line 101): `0 >= 5` → `False` — proceed.
2. Acquire proxy: succeeds (a proxy is returned).
3. `attempt == 0`, so `get_session_cookies(J-001)` is called. The question states J-001 has "no session cookies and no cf_clearance — it is a fresh job", so we treat the cookies as empty.
4. `"cf_clearance" not in cookies` → `True` — enters `_solve_captcha()`.
5. In `_solve_captcha()` (line 239): budget check `0 >= 5` → `False` — proceed.
6. `captcha_solves_used` increments to **1** (line 246).
7. `solve_turnstile()` raises `CaptchaProviderError("503 Service Unavailable")`.
8. The exception is caught (line 258), logged, and `_solve_captcha()` returns **`None`**.
9. Back in `_process_job()` (line 133): `solve_result is None` → `return`. The job processing ends.

**Key insight**: When `_solve_captcha()` returns `None`, the worker immediately returns from `_process_job()` via line 134–135. It does **not** continue to the next loop iteration — it **exits** the entire retry loop. The job is left in whatever state `_solve_captcha()` set it to.

Looking at `_solve_captcha()` more carefully: when `CaptchaProviderError` is caught, the method simply returns `None` (line 264) **without** calling `job.mark_exhausted()`. The `mark_exhausted()` call only happens if the budget check at line 239–241 triggers (`captcha_solves_used >= retry_budget_per_job`). After one failed solve, `captcha_solves_used = 1`, which is less than 5, so `mark_exhausted()` is **not** called.

The worker returns from `_process_job()` with the job still in **`IN_PROGRESS`** status. Since the `for` loop in `_process_job()` was exited via `return` (not `continue`), the job never gets another attempt within this invocation.

The `run()` method moves to the next job (J-002), which encounters the same pattern. This repeats for J-003 through J-005.

**`captcha_solves_used` for J-001**: Increments **once** — to **1**. Each job gets exactly one failed solve attempt before the worker gives up on it.

**J-001's final status**: **`IN_PROGRESS`**. The job was never moved to a terminal state.

### (b) After CapSolver Comes Back Online

**Status of J-001 through J-005**: All five jobs are in **`IN_PROGRESS`** status with `captcha_solves_used = 1` each. None reached a terminal state.

**Is there an automatic retry path?**

**No.** Tracing through the code:

- `worker.run()` (line 79–81) only processes jobs with `status in (PENDING, RETRYING)`. The jobs are in `IN_PROGRESS`, so they would be **skipped** if `run()` were called again.
- `campaign.py`'s `CampaignManager` has no automatic retry or re-dispatch mechanism — `finalize()` only checks terminal states and `get_pending()` only returns `PENDING` campaigns.
- There is no code path anywhere that transitions an `IN_PROGRESS` job back to `PENDING` or `RETRYING`.

**What the operator must do**: Manually reset the status of J-001 through J-005 from `IN_PROGRESS` back to `PENDING` (or `RETRYING`), then re-dispatch the campaign worker. This is a manual intervention step — the platform has no self-healing mechanism for jobs stranded in `IN_PROGRESS`.

### (c) Distinguishing Scenario A (Provider Down) vs. Scenario B (Proxy Range Blocked)

| Field | Scenario A (Provider Down) | Scenario B (Proxies Blocked) |
|---|---|---|
| `job.status` | `EXHAUSTED` | `EXHAUSTED` |
| `job.captcha_solves_used` | 5 (budget depleted) | 5 (budget depleted) |
| `job.retry_count` | Low (0 or 1) — never reached HTTP request | Higher — multiple HTTP attempts occurred |
| `job.error_log` | **Empty** — no HTTP responses were received | Contains multiple `ErrorEvent` entries with `error_type=PROXY_BANNED`, `http_status=403`, `remediation=ROTATE_PROXY` |
| `job.result` | `None` — no parse was ever attempted | `None` — no successful parse |
| `job.assigned_proxy_id` | May still show the originally assigned proxy | May show the last rotated proxy |

**The critical distinguishing field is `job.error_log`**:

- **Scenario A**: The error log would be **empty** or contain no HTTP-level errors. The CAPTCHA provider failed before any HTTP request was made — `_solve_captcha()` returned `None` each time, and the worker never reached `http_client.execute()`. The only evidence is `captcha_solves_used = 5`.

- **Scenario B**: The error log would contain a sequence of `ErrorEvent` entries with `error_type=PROXY_BANNED`, `http_status=403`, showing successive proxy rotations. CAPTCHA solves succeeded (valid tokens obtained), but the HTTP requests all returned 403.

**Limitation**: The `Job` model does not have a dedicated field to distinguish provider failures from site-enforcement failures. The `captcha_solves_used` count is a unified budget that treats both identically. An operator would need to correlate the error log contents with external provider monitoring to conclusively identify the cause. Adding an `error_type` field like `PROVIDER_ERROR` to the error log when `CaptchaProviderError` is caught would improve this diagnostic capability.

---

## Q4 — Trace the Token

### Step 1 — 403 IP_BLOCKED from P-003

The target site returns `HTTP 403` with body `{"error": "IP_BLOCKED", "message": "This IP address has been blocked."}`.

`ErrorClassifier.classify()` (http_client.py, lines 79–87):

- `status == 403` → matches the 403 branch.
- Returns: **`ErrorType.PROXY_BANNED`**, **`RemediationAction.ROTATE_PROXY`**.

The classifier **does not inspect the response body**. It treats all 403 responses identically regardless of the `error` field content.

**What worker.py does**: The remediation is `ROTATE_PROXY`, so (lines 170–181):
1. Calls `proxy_pool.rotate("P-003", campaign_id)`.
2. P-003 is marked `COOLING_DOWN`.
3. The pool allocates a replacement proxy.
4. `job.retry_count` increments.
5. `continue` — loop to next attempt.

### Step 2 — Proxy Rotation from P-003

`proxy_pool.rotate("P-003", campaign_id)` is called:

1. P-003 is marked `COOLING_DOWN` (`_mark_cooling_down`): status → `COOLING_DOWN`, `ban_count` → 3, `assigned_campaign_id` → `None`.
2. `_allocate()` is called to find a replacement.

**Is P-001 available?**: P-001 is `HEALTHY`, with `sticky_until = "2025-02-06T09:05:00+00:00"` — this timestamp is in the past (current time is `09:15+`). `is_sticky_active()` returns `False`. Its `assigned_campaign_id` is `"C-001"`, but since the sticky window has expired, it can be reallocated. **P-001 is available**.

P-002 is `HEALTHY` with `sticky_until = "2025-02-06T09:25:00+00:00"` — this is still active, and it's assigned to `C-001`, not C-002. Since `is_sticky_active()` returns `True` and it's bound to a different campaign, P-002 is **skipped** in `_allocate()`.

**`rotate()` returns P-001** (or **P-002** if its sticky expired). Per the question's framing ("assume P-002"), we proceed with P-002.

### Step 3 — 403 SESSION_FINGERPRINT_MISMATCH from P-002

The site returns `HTTP 403` with `{"error": "SESSION_FINGERPRINT_MISMATCH", "message": "Session identity is inconsistent."}`.

`ErrorClassifier.classify()`:

- `status == 403` → same 403 branch.
- Returns: **`ErrorType.PROXY_BANNED`**, **`RemediationAction.ROTATE_PROXY`** — identical to Step 1.

The classifier does not differentiate between `IP_BLOCKED` and `SESSION_FINGERPRINT_MISMATCH`. Both are treated as proxy bans requiring rotation.

**What happens to P-002**: `proxy_pool.rotate("P-002", campaign_id)` is called. P-002 is marked `COOLING_DOWN`. The pool tries to find another proxy.

### Step 4 — The Cascade Loop

After Steps 1–3, the pool state is:

| Proxy | Status | Available? |
|-------|--------|-----------|
| P-001 | HEALTHY (if not used) or COOLING_DOWN (if used in Step 2) | Depends |
| P-002 | COOLING_DOWN | No |
| P-003 | COOLING_DOWN | No |
| P-004 | COOLING_DOWN (pre-existing) | No |

If P-001 is still HEALTHY, it gets allocated next. It would encounter the same `SESSION_FINGERPRINT_MISMATCH` (the session's `cf_clearance` is stale — solved by a different proxy). P-001 gets rotated to `COOLING_DOWN` as well.

**Final state — all proxies exhausted**:

| Proxy | Status |
|-------|--------|
| P-001 | COOLING_DOWN |
| P-002 | COOLING_DOWN |
| P-003 | COOLING_DOWN |
| P-004 | COOLING_DOWN |

When `rotate()` calls `_allocate()` and finds zero healthy proxies, it returns `None`. In the worker (line 172–178), `new_proxy is None` triggers `job.mark_failed(ErrorType.PROXY_BANNED)`, and the worker returns.

**J-015's final status**: **`FAILED`** with `error_type=PROXY_BANNED`.

**Proxies in COOLING_DOWN**: All **4 proxies** — 3 rotated during this sequence (P-003, P-002, P-001) plus P-004 which was already `COOLING_DOWN`.

### Step 5 — Root Cause Analysis

**Actually IP-banned by the target site**: Only **1** — P-003 received `IP_BLOCKED`, which is a genuine IP-level ban.

**Rotated for other reasons**: **2** (P-002 and P-001) — these received `SESSION_FINGERPRINT_MISMATCH`, which is a **session/token coherence failure**, not an IP ban. The session cookies (particularly `cf_clearance`) were bound to a different proxy's IP fingerprint, and using them from a new IP caused the mismatch. The proxy IPs themselves were not banned.

**Root cause of proxy pool exhaustion**: The `ErrorClassifier` treats all HTTP 403 responses identically as `PROXY_BANNED` with `ROTATE_PROXY` remediation. It does not distinguish between:

- **IP-level bans** (proxy is genuinely blocked — rotation is correct).
- **Session/token failures** (the clearance cookie is stale or mismatched — the proxy is fine, the session needs refreshing).

When a `SESSION_FINGERPRINT_MISMATCH` occurs, the correct remediation is **not** to rotate the proxy but to **invalidate the session** (clear the stale `cf_clearance`) and re-solve the CAPTCHA on the **current** proxy. By rotating the proxy instead, the system burns through healthy proxies needlessly while the underlying problem (stale session state) persists from proxy to proxy.

**Single conceptual change**: The `ErrorClassifier` should inspect the 403 response body to distinguish IP-level bans from token/session failures. If the `error` field indicates a session or token issue (`SESSION_FINGERPRINT_MISMATCH`, `TOKEN_INVALID`, `TOKEN_EXPIRED`, `CAPTCHA_REQUIRED`), the remediation should be `RESOLVE_CAPTCHA` (invalidate session, re-solve) rather than `ROTATE_PROXY`. Only genuine IP-level bans (`IP_BLOCKED`, `ACCESS_DENIED`) should trigger proxy rotation.

---

## Q5 — Evaluate the Fix

### Fix A — Response Body Inspection in ErrorClassifier

#### (a) Does Fix A Address the Root Cause?

**Partially.** Fix A correctly identifies that the `ErrorClassifier` needs to distinguish between IP bans and token failures by inspecting the response body. For explicit CAPTCHA/token errors (`TOKEN_INVALID`, `TOKEN_EXPIRED`, `CAPTCHA_REQUIRED`), it correctly routes them to `RESOLVE_CAPTCHA` instead of `ROTATE_PROXY`.

However, it **misses** `SESSION_FINGERPRINT_MISMATCH` — the exact error in Q4's cascade. This error code is not in the token error list `("TOKEN_INVALID", "TOKEN_EXPIRED", "CAPTCHA_REQUIRED")`, so it falls through to the default path and still triggers `ROTATE_PROXY`. The cascade from Q4 would still occur because the fingerprint mismatch is not recognised as a session-state problem.

To fully address Q4's cascade, `SESSION_FINGERPRINT_MISMATCH` needs to be included in the token/session error set that triggers `RESOLVE_CAPTCHA`.

#### (b) JSON Body Dependency Risk

Fix A assumes `response.json_body` always contains a parseable JSON dict with a consistent `error` field. If the site returns a 403 with an **HTML body** (e.g., a WAF block page or Cloudflare's standard challenge HTML):

1. `response.json_body` would be `None` (the `HttpClient` only parses JSON when `Content-Type` contains `application/json` — see http_client.py, lines 201–206).
2. `body = response.json_body or {}` → `body = {}`.
3. `error_code = body.get("error", "")` → `error_code = ""`.
4. `"" in ("TOKEN_INVALID", "TOKEN_EXPIRED", "CAPTCHA_REQUIRED")` → `False`.
5. Falls through to the default path: `PROXY_BANNED` / `ROTATE_PROXY`.

This is actually a **safe degradation** — HTML 403 responses would be treated as proxy bans, matching the current behavior. However, some HTML 403 responses may actually be Cloudflare challenge pages that should trigger a CAPTCHA solve rather than a proxy rotation. Fix A handles non-JSON gracefully but incorrectly in those edge cases.

### Fix B — Invalidate Session on Proxy Rotation

#### (a) Does Fix B Address the Root Cause?

**Not at all.** Fix B addresses a *consequence* but not the *cause* of the cascade. The root cause is that `ErrorClassifier` triggers proxy rotation for session-level failures. Fix B accepts the incorrect rotation and tries to clean up afterwards by invalidating the session.

Moreover, Fix B calls `self._session_manager.invalidate_session(campaign_id)`, but `SessionManager.invalidate_session()` takes a `job_id` parameter, not `campaign_id` (session_manager.py, line 113). This is an API mismatch — the method signature is `invalidate_session(self, job_id: str)`, so passing a `campaign_id` would clear the wrong session or do nothing.

Even if the API were fixed, the cascade still occurs: every 403 triggers a proxy rotation and session invalidation. The fresh session requires a new CAPTCHA solve, which consumes budget. The proxy is still burned (marked `COOLING_DOWN`). The pool still exhausts.

#### (b) CAPTCHA Budget Impact with 3 Proxy Rotations

After Fix B, each proxy rotation invalidates the session, clearing `cf_clearance`. On the next attempt:

1. **Initial solve**: `captcha_solves_used = 1`. Request succeeds or fails.
2. **Rotation 1**: Session invalidated. No `cf_clearance`. Must re-solve. `captcha_solves_used = 2`.
3. **Rotation 2**: Session invalidated again. Must re-solve. `captcha_solves_used = 3`.
4. **Rotation 3**: Session invalidated again. Must re-solve. `captcha_solves_used = 4`.

With `retry_budget_per_job = 5`, a job that needs 3 proxy rotations before succeeding would consume **4 CAPTCHA solves** out of its budget of 5. If the job encounters one more failure after that, it has only 1 solve left before `EXHAUSTED`.

This dramatically accelerates budget depletion. Jobs that could previously survive on their session state across rotations (because `cf_clearance` was preserved) now burn a solve on every rotation.

#### (c) Architectural Violation

AGENTS.md, Session Management section, states:

> *"Session state — including cookies, browser headers, and clearance tokens — is managed exclusively by `SessionManager`. Callers must not manipulate session components directly."*

> *"The session manager is the single source of truth for all agent identity state."*

Fix B places `self._session_manager.invalidate_session(campaign_id)` inside `ProxyPool.rotate()`. This means:

1. `ProxyPool` now depends on `SessionManager`, creating a cross-cutting coupling that violates the single-responsibility boundaries defined in AGENTS.md.
2. Session invalidation logic is embedded in the proxy layer — a layer that explicitly should not manage session state.
3. `ProxyPool` is now responsible for a session decision (when to invalidate), even though AGENTS.md designates `SessionManager` as the exclusive owner of that logic.

**Does this matter?** Yes, significantly. The layered architecture exists to prevent exactly this kind of entanglement. If session logic leaks into `ProxyPool`, future changes to session policy require modifying `ProxyPool` — a component whose responsibility is proxy lifecycle management, not session coherence. This leads to the "classification drift" that AGENTS.md explicitly warns against.

### (d) The Correct Fix

**Root cause in one sentence**: The `ErrorClassifier` treats all HTTP 403 responses as IP-level proxy bans requiring rotation, but some 403s indicate session/token failures where the proxy is healthy and only the session state needs refreshing.

**Layers that need to change**:

#### 1. `ErrorClassifier` in `http_client.py`

The classifier must inspect the 403 response body to distinguish error categories:

- **IP-level bans** (`IP_BLOCKED`, `ACCESS_DENIED`, and similar): `ErrorType.PROXY_BANNED` / `RemediationAction.ROTATE_PROXY` — the current behavior for genuine IP bans.
- **Session/token failures** (`SESSION_FINGERPRINT_MISMATCH`, `TOKEN_INVALID`, `TOKEN_EXPIRED`, `CAPTCHA_REQUIRED`): a new or existing error type (e.g., `ErrorType.CAPTCHA_INVALID`) / `RemediationAction.RESOLVE_CAPTCHA` — the proxy is fine, but the session needs refreshing.
- **Unknown 403** (no JSON body, HTML response, or unrecognized error code): default to `ROTATE_PROXY` as the conservative safe path.

#### 2. `worker.py` — Remediation Handler for `RESOLVE_CAPTCHA`

The worker needs a handler for the `RESOLVE_CAPTCHA` remediation action on a non-200 response:

1. Invalidate the current session via `session_manager.invalidate_session(job.id)` — clearing the stale `cf_clearance`.
2. **Do not rotate the proxy** — the proxy is healthy.
3. Increment `retry_count` and `continue` the retry loop.
4. On the next iteration, the absence of `cf_clearance` in the cookies will trigger `_solve_captcha()`, which obtains a fresh token via the current (healthy) proxy.

#### 3. Content Validation in `worker.py` (addresses Q1's problem)

Add a content-validation step after receiving an HTTP 200 response and before accepting the parse result as `COMPLETED`. This check can look for Cloudflare challenge markers in the response body (e.g., `cf-turnstile`, known challenge page patterns). If a 200 response is identified as a challenge page:

1. Invalidate the session (`cf_clearance` is stale or insufficient).
2. Increment `captcha_solves_used` appropriately.
3. `continue` the retry loop to re-solve and re-fetch.

#### Fixed Session Restoration Flow on Retry with New Proxy

When a retry occurs on a new proxy (after a legitimate IP ban), the correct flow is:

1. Proxy is rotated by `ProxyPool` (proxy layer only — no session manipulation).
2. The worker, recognizing that the proxy changed, calls `session_manager.invalidate_session(job.id)` to clear the session bound to the old proxy's IP context.
3. On the next attempt, `restore_session()` falls back to `get_session_cookies()` (no persisted session exists after invalidation).
4. The worker detects that `cf_clearance` is missing (it was cleared with the session) and calls `_solve_captcha()` to obtain a fresh token that is valid for the new proxy's IP.
5. The request proceeds with a coherent session: fresh `cf_clearance` + new proxy.

This keeps session management within `SessionManager`, proxy management within `ProxyPool`, and error classification within `ErrorClassifier` — each layer maintaining its single responsibility as defined in AGENTS.md.
