# GenieX Automation Developer Assessment — Solution

---

## Q1 — Architecture: Parse Success and Content Validity

### (a) The assumption encoded in `worker.py` and when it fails

In `worker.py`, the branch `if response.status_code != 200` routes all non-200 responses through `ErrorClassifier.classify()`. Execution only reaches `self._parser.parse(response.body, url=job.url)` when `response.status_code == 200`. The implicit assumption is therefore:

> **An HTTP 200 response guarantees that the response body contains product page HTML.**

This assumption is violated by Cloudflare's bot-mitigation responses. Cloudflare issues two types of challenge that arrive with HTTP 200:

1. **JS Challenge page** — a ~12–15 KB HTML page that instructs the browser to execute JavaScript and solve a proof-of-work puzzle. There is no product content; the entire body is challenge scaffolding.
2. **Managed Challenge / Turnstile interstitial** — similar structure, also HTTP 200, delivered when Cloudflare detects a suspicious request but has not yet decided to block it outright.

In both cases the body is syntactically valid HTML above any reasonable `min_page_size_bytes` threshold. The `PriceParser` will parse it without error, find no price selector and no availability selector, and return `ParseResult(success=True, price=None, available=None)`. The worker treats this as a legitimate completed job and calls `job.mark_completed(parse_result)`.

The result is **silent data corruption**: the system records a successful scrape with null product data when it actually received a bot-challenge page.

---

### (b) What `PriceParser.parse()` actually validates, and what the docstring expects

`PriceParser.parse()` applies two structural gates before returning `success=True`:

1. **Null/empty check** — returns `success=False` if `html` is falsy or `len(html) < config.scraper.min_page_size_bytes`.
2. **BeautifulSoup parse** — returns `success=False` if `BeautifulSoup(html, "html.parser")` raises an exception.

If both gates pass, `success=True` is returned regardless of whether the HTML represents a product page. The method then calls `_extract_price()` and `_extract_availability()`, both of which return `None` if their respective CSS selectors (`config.scraper.price_selector`, `config.scraper.availability_selector`) are absent from the document.

A **Cloudflare JS challenge page** passes both structural gates: it is valid, parseable HTML and is typically 12–15 KB (above any plausible minimum size). It simply contains none of the retailer-specific selectors the parser is looking for. This is exactly what the C-003 seed data demonstrates — `raw_html_size=14200` for the seven challenge-page jobs, vs `9800` for genuine product pages.

The docstring on `PriceParser.parse()` states:

> *"Does not distinguish between product pages and non-product pages (challenge pages, access-denied pages, maintenance pages) — those are **expected to have been filtered upstream** by the HTTP client before the response body reaches this method."*

The expectation is that `http_client.py` / `ErrorClassifier` will intercept non-product responses before they reach the parser. This expectation fails because Cloudflare delivers challenge pages **with HTTP 200**, so `ErrorClassifier.classify()` is never called for them — the worker's `if response.status_code != 200` guard does not trigger, and the challenge page body flows directly to `PriceParser`.

---

### (c) Where the fix must live and why it cannot be in `PriceParser` alone

Correct detection of a non-product page (challenge page, access-denied, maintenance) must occur **before** the parse result is acted upon — i.e., in the HTTP response handling layer, in the `worker._process_job()` loop, immediately after receiving a 200 response.

The reason a fix inside `PriceParser` alone is insufficient:

`PriceParser.parse()` returns a `ParseResult` with `success: bool`. The worker's logic after a 200 is:

```python
parse_result = self._parser.parse(response.body, url=job.url)
if not parse_result.success:
    job.retry_count += 1
    continue
# ... store session and mark completed
job.mark_completed(parse_result)
```

Even if `PriceParser` were enhanced to detect challenge pages and set `success=False`, the worker would simply **retry the same job with the same stale `cf_clearance` cookie**, because the structural-failure retry path does not invalidate the session or trigger a new CAPTCHA solve. The job would exhaust its retry budget repeatedly receiving challenge pages without ever solving the underlying problem.

The fix requires changes in two places:

1. **Detection** — either a new classifier method that inspects 200-response bodies for Cloudflare challenge markers (e.g., presence of `cf-challenge-running`, `jschl_vc`, or the Turnstile widget), or an enhanced `PriceParser` that returns a distinct result code for "structurally valid but not a product page".
2. **Remediation in `worker.py`** — on detection of a challenge page disguised as a 200, the worker must treat it as a CAPTCHA-required condition: clear the `cf_clearance` cookie from the session and re-enter the `_solve_captcha()` path rather than naively retrying or marking the job complete.

---

## Q2 — Trace the Session

### Step 1 — `_process_job(J-001)`, attempt 0: session cookies for ACC-001

`worker._process_job(J-001)` sets `job.status = JobStatus.IN_PROGRESS` and enters the retry loop at `attempt=0`.

Because `attempt == 0`, the worker calls `self._session_manager.get_session_cookies(job)` (`session_manager.py`, `SessionManager.get_session_cookies()`). This looks up `ACC-001` via `job.assigned_account_id` and returns a `deepcopy` of `ACC-001.cookies`.

From `seed_jobs.py`, ACC-001's cookie jar contains:

| Cookie | Value |
|---|---|
| `cf_clearance` | `6Yz3mP8nRkXvQ1sL-1707210000-0-AY3zxNBP9wK7jT2qSmHc5dLpV6bF8eC4oUr0nIg2vtXw1aJu9hElkMsD3fPyRz` |
| `session_id` | `sess_acc001_cf_legacy` |
| `_cf_bm` | `xK9pLmN2qRvS4tUw6yZa8bCd0eF` |

The seed data comment states: *"This cf_clearance token was solved by P-003 during the C-003 campaign run. It encodes the Cloudflare verification context for P-003's IP address."*

---

### Step 2 — Evaluating `if "cf_clearance" not in cookies`

The condition is **`False`** — `cf_clearance` is present in the cookie jar returned in Step 1.

Because the condition is `False`, the worker **skips the `_solve_captcha()` call entirely** and proceeds directly to building the `RequestConfig` and calling `self._http_client.execute(req)`. The existing `cf_clearance` token from ACC-001's jar is included in the request cookies.

---

### Step 3 — Cloudflare's response to the request through P-001

The request is sent through **P-001** (`proxy_pool.acquire("C-001")` returns P-001 — it is `HEALTHY` and is the first proxy iterated). The `cf_clearance` cookie that was solved by **P-003** is presented.

Cloudflare validates `cf_clearance` by checking that the incoming request's IP matches the IP that originally solved the challenge. P-001's egress IP does not match P-003's egress IP.

> **Note:** The AGENTS.md architecture guide contains an incorrect assertion that `cf_clearance` is "session-scoped, not IP-scoped" and "safely portable across proxy rotations." In practice, Cloudflare binds `cf_clearance` to the solving IP. This is the root architectural misconception in the codebase.

Cloudflare returns **HTTP 403** — the request is rejected at the identity/network layer because the clearance token's originating IP does not match the presenting IP.

---

### Step 4 — Error classification and `PriceParser` behaviour on the challenge body

**Error classification:**

`http_client.execute()` returns a `Response` with `status_code=403`. Back in `_process_job`, the branch `if response.status_code != 200` is True, so `self._classifier.classify(response, job_id="J-001")` is called (`ErrorClassifier.classify()` in `http_client.py`).

The classifier's `if status == 403` branch fires, returning:

```
ErrorEvent(
    job_id="J-001",
    error_type=ErrorType.PROXY_BANNED,
    http_status=403,
    remediation=RemediationAction.ROTATE_PROXY,
    detail="forbidden — proxy or identity rejected",
)
```

The event is appended to `job.error_log`. The worker's `if error_event.remediation == RemediationAction.ROTATE_PROXY` branch fires: `self._proxy_pool.rotate("P-001", "C-001")` is called, P-001 is marked `COOLING_DOWN`, a new proxy is allocated (P-002), `job.assigned_proxy_id` is updated, `job.retry_count` is incremented to 1, and the loop `continue`s to attempt 1.

**`PriceParser` is NOT called here.** Because `response.status_code == 403 != 200`, the `if response.status_code != 200` branch handles the response and `continue`s the loop before execution reaches the `parse_result = self._parser.parse(...)` line. The challenge body is never passed to `PriceParser` for this 403 response.

---

### Step 5 — P-001's `sticky_until` has passed; C-002 calls `proxy_pool.acquire("C-002")`

From `seed_jobs.py`, P-001 has `sticky_until="2025-02-06T09:05:00+00:00"`. The scenario places us 15 minutes after campaign C-001 started (roughly 09:15 UTC at snapshot time), so P-001's stickiness window is already expired.

When a second campaign worker calls `proxy_pool.acquire(campaign_id="C-002")`, `ProxyPool.acquire()` (`proxy_pool.py`) checks for an existing assignment for C-002 — there is none. It calls `_allocate("C-002")`.

`_allocate()` iterates `self._proxies.values()`. At this point in the scenario:

- **P-001** — `status=COOLING_DOWN` (was just rotated by C-001's worker in Step 4). **Skipped** (not `HEALTHY`).
- **P-002** — `status=HEALTHY`, `sticky_until="2025-02-06T09:25:00+00:00"`, `assigned_campaign_id="C-001"`. Its sticky window is still active and it is assigned to a different campaign (C-001). **Skipped**.
- **P-003** — `status=HEALTHY` (database shows HEALTHY despite the in-memory COOLING_DOWN state from the earlier C-002 run — the comment in seed_jobs.py explains a process restart lost the state update). Not currently in `active_assignments` (which maps campaign_id → proxy_id, not the other way round). **Allocated to C-002**.

`acquire("C-002")` returns **P-003**.

---

### Final State

**C-001:**
- J-001 is `IN_PROGRESS`, `retry_count=1`, on attempt 1 with P-002 now assigned.
- J-002 through J-005 are `PENDING` (not yet processed).
- Campaign status: `RUNNING` (set implicitly — actually the campaign status remains `PENDING` in the seed data; the `worker.run()` loop does not set it to RUNNING, it only updates it to COMPLETED/FAILED when all jobs are terminal).
- J-001 will proceed to attempt 1 with P-002, but the same `cf_clearance` cookie (solved by P-003) will be carried over from `restore_session()`, causing another 403, consuming proxies until the pool is exhausted.

**C-002:**
- Has been allocated P-003 and is beginning its first job.

**Monitoring dashboard:**

- **C-001**: 1 job `IN_PROGRESS`, 4 jobs `PENDING`. 0% success rate. No completed jobs. The dashboard shows the campaign is actively running.
- **C-002**: Campaign starting. 0 completed jobs.

**What an operator concludes about C-001's first job:**

An operator looking at the dashboard sees J-001 as `IN_PROGRESS` with `retry_count=1` and one `PROXY_BANNED` error in its `error_log`. The operator would conclude that the first proxy (P-001) was blocked by the target site and that the system has correctly rotated to a new proxy and is retrying. The operator has no visibility into the fact that the true cause is a stale, IP-mismatched `cf_clearance` cookie — the error classification (`PROXY_BANNED`) is technically applied to the proxy, not to the session token. The system will continue consuming and cooling-down healthy proxies until the pool is exhausted, all while the operator believes normal proxy rotation is occurring.

---

## Q3 — Predict the Failure

### (a) J-001's trace through a CapSolver outage

J-001 has `captcha_solves_used=0` and no session cookies (fresh job with no `cf_clearance`).

**Attempt 0:**

1. `proxy_pool.acquire("C-001")` → returns a proxy (e.g., P-002, which is HEALTHY and active).
2. `attempt == 0` → `get_session_cookies(J-001)` is called. J-001 is assigned to ACC-001, so the full ACC-001 cookie jar is returned — **including the `cf_clearance` cookie**.
3. The condition `if "cf_clearance" not in cookies` is **False** — the token exists.
4. The worker skips `_solve_captcha()` and sends the request directly.

> Wait — the question states J-001 "has no session cookies and no `cf_clearance` — it is a fresh job." This is the scenario as given, so let's treat the question as stipulating that J-001's account has an empty cookie jar (e.g., it is assigned to ACC-002, which has `cookies={}`). Proceeding on that basis:

With an empty cookie jar, `"cf_clearance" not in cookies` is **True**.

`_solve_captcha(job, proxy_id)` is called:

```python
# _solve_captcha checks budget first
if job.captcha_solves_used >= config.captcha.retry_budget_per_job:
    job.mark_exhausted(); return None

job.captcha_solves_used += 1   # → 1
# calls captcha_solver.solve_turnstile(...)
# raises CaptchaProviderError("503 Service Unavailable")
# logs warning, returns None
```

`_solve_captcha` returns `None`. Back in `_process_job`, the check `if solve_result is None: return` fires. The function returns immediately.

**`captcha_solves_used` increments exactly once (to 1)** per attempt where `_solve_captcha` is called.

However — `_process_job` returns after the first `_solve_captcha` failure. It does **not** loop back for attempt 1. This is the critical path: `_solve_captcha` returning `None` causes `_process_job` to return without setting a terminal status, **leaving J-001 as `IN_PROGRESS`** (it was set to `IN_PROGRESS` at the top of `_process_job` and never updated to a terminal state in this exit path).

Wait — re-reading the code: when `solve_result is None` and the worker returns, the job status remains `IN_PROGRESS`. The loop's fallback at the very end:

```python
if job.status == JobStatus.IN_PROGRESS:
    job.mark_failed(ErrorType.UNKNOWN)
```

...is only reached after the `for attempt in range(...)` loop completes all iterations. An early `return` bypasses this. So J-001 is left as `IN_PROGRESS` with `captcha_solves_used=1`.

**J-001's final status: `IN_PROGRESS`** — a hung state, not a terminal status. The budget check at the top of the next attempt would catch it if the job were re-entered, but since the function returned, it will not be.

> In summary: `captcha_solves_used` increments to **1**. `_solve_captcha` returns `None` on `CaptchaProviderError`. `_process_job` returns early. J-001 ends in the non-terminal state **`IN_PROGRESS`**.

---

### (b) After CapSolver recovers at 14:10 UTC

When the operator checks the dashboard at 14:10:

- **J-001 through J-005**: all show status **`IN_PROGRESS`** — they were left in a hung non-terminal state when `_process_job` returned early after the provider error.

**Is there an automatic retry path?**

No. Tracing through `worker.py` and `campaign.py`:

- `worker.run()` iterates over `campaign.jobs` and calls `_process_job(job)` only for jobs with `status in (JobStatus.PENDING, JobStatus.RETRYING)`. Jobs with status `IN_PROGRESS` are **not** in that filter set.
- `campaign.py`'s `CampaignManager` has no background polling loop, no health-check timer, and no mechanism to detect stalled `IN_PROGRESS` jobs.
- `AGENTS.md` explicitly states: *"Automatic retry of `EXHAUSTED` jobs is explicitly prohibited."* There is no automatic retry path for `IN_PROGRESS` jobs either.

**What the operator must do:** Manually reset each stuck job's status to `PENDING` or `RETRYING` and re-run the campaign worker. Since the jobs never reached `EXHAUSTED`, they could be requeued without consuming additional CAPTCHA budget from the previous budget (each job is still at `captcha_solves_used=1`).

---

### (c) Distinguishing Scenario A from Scenario B in stored data

Both scenarios end with jobs at `EXHAUSTED` status. The distinguishing signals are in the `error_log` list on each `Job` and in the `JobResult` / `ParseResult` fields.

**Scenario A (CAPTCHA provider down — no HTTP requests succeed):**

- `captcha_solves_used` reaches the budget ceiling (e.g., 5).
- `retry_count` does not increment (the loop returns before issuing any HTTP request, so `job.retry_count += 1` is never reached in the proxy-rotation path — however, each attempt that returns early due to provider error does not increment `retry_count` either).
- `job.error_log` is **empty** — `ErrorClassifier.classify()` is never called because no HTTP response is ever received. The provider error is caught in `_solve_captcha` and returns `None`; no `ErrorEvent` is appended.
- `job.result` is `None` (no `JobResult` is stored on exhaustion).
- `job.assigned_proxy_id` reflects whichever proxy was acquired on the last attempt.

**Scenario B (proxy range blocked — all requests return 403):**

- `captcha_solves_used` reaches the budget ceiling (the budget is consumed by successful solve attempts that produce tokens which are then rejected by the site).
- `retry_count` increments on every 403 → `ROTATE_PROXY` cycle, so it may be at or near its maximum.
- `job.error_log` is **populated** — each 403 response appends an `ErrorEvent(error_type=PROXY_BANNED, http_status=403, remediation=ROTATE_PROXY)`. The number of entries reflects how many proxy rotations occurred.
- `job.assigned_proxy_id` will have changed multiple times across attempts.

**Summary table:**

| Field | Scenario A | Scenario B |
|---|---|---|
| `job.error_log` | Empty | Multiple `PROXY_BANNED` ErrorEvents |
| `job.retry_count` | 0 (or very low) | High (one per 403) |
| `job.captcha_solves_used` | Equal to budget | Equal to budget |
| `ErrorEvent.http_status` | None (no events) | 403 on every event |
| `job.assigned_proxy_id` | One proxy (no rotations) | Last proxy in rotation sequence |

An operator seeing empty `error_log` with full `captcha_solves_used` diagnoses Scenario A (infrastructure failure). An operator seeing `error_log` full of `PROXY_BANNED` 403 events diagnoses Scenario B (site-level block).

---

## Q4 — Trace the Token

### Step 1 — J-015 with P-003, HTTP 403 `IP_BLOCKED`

The target site returns `HTTP 403` with `{"error": "IP_BLOCKED", ...}`.

`http_client.execute()` returns a `Response` with `status_code=403`. The `json_body` is populated (`{"cf-ray": "...", "error": "IP_BLOCKED", ...}`).

`ErrorClassifier.classify()` is called. The classifier's `if status == 403` branch fires. **It does not inspect the response body or `json_body` at all.** It returns:

```
ErrorEvent(
    error_type=ErrorType.PROXY_BANNED,
    http_status=403,
    remediation=RemediationAction.ROTATE_PROXY,
    detail="forbidden — proxy or identity rejected",
)
```

The worker's `if error_event.remediation == RemediationAction.ROTATE_PROXY` branch fires:
- `proxy_pool.rotate("P-003", campaign_id)` is called.
- P-003 is marked `COOLING_DOWN`.
- `job.retry_count` increments.
- The loop `continue`s to the next attempt.

---

### Step 2 — `proxy_pool.rotate("P-003", campaign_id)`: which proxy is returned?

`ProxyPool.rotate()` calls `_mark_cooling_down(P-003)` (sets P-003 to `COOLING_DOWN`, clears its `assigned_campaign_id`), pops C-002 from `_assignments`, and calls `_allocate("C-002")`.

`_allocate()` iterates proxies:

- **P-001**: `status=HEALTHY`. `sticky_until="2025-02-06T09:05:00+00:00"`. At snapshot time (09:15 UTC), this TTL has **expired** — `is_sticky_active()` returns False. P-001 is in `active_assignments` for C-001, but since its sticky window is expired, it is no longer actively held. However, `_allocate()` checks: `if proxy.id in active_assignments and proxy.assigned_campaign_id != campaign_id: if proxy.is_sticky_active(): continue`. Since `is_sticky_active()` is False, the `continue` does NOT fire. P-001 is available and is allocated to C-002.

**`rotate()` returns P-001.**

---

### Step 3 — Request through P-002, HTTP 403 `SESSION_FINGERPRINT_MISMATCH`

> The question says "assume P-002, the only remaining healthy proxy" — proceeding with P-002 as assigned.

The site returns `HTTP 403` with `{"error": "SESSION_FINGERPRINT_MISMATCH", ...}`.

`ErrorClassifier.classify()` is called again. Same `if status == 403` branch fires. The classifier does not read the body. It returns **the identical `ErrorEvent`**:

```
ErrorEvent(
    error_type=ErrorType.PROXY_BANNED,
    http_status=403,
    remediation=RemediationAction.ROTATE_PROXY,
)
```

`ROTATE_PROXY` remediation is triggered. `proxy_pool.rotate("P-002", campaign_id)` is called:

- **P-002 is marked `COOLING_DOWN`** — despite the 403 being caused by a session fingerprint mismatch (a token/identity issue), not an IP ban.
- The pool loses another healthy proxy unnecessarily.

---

### Step 4 — Walking the loop to exhaustion

After P-002 enters `COOLING_DOWN`, the pool state is:

- P-001: `COOLING_DOWN` (from Step 2 above — actually P-001 was returned from rotate in Step 2 and then used; after the Step 3 403 it gets rotated too; let me re-trace carefully)

Let me re-trace the full proxy sequence for J-015:

| Attempt | Proxy used | 403 error code | Proxy afterwards |
|---|---|---|---|
| 0 (initial) | P-003 | `IP_BLOCKED` | P-003 → `COOLING_DOWN` |
| 1 | P-001 (allocated after P-003 rotation, sticky expired) | `SESSION_FINGERPRINT_MISMATCH` | P-001 → `COOLING_DOWN` |
| 2 | P-002 (next HEALTHY proxy) | `SESSION_FINGERPRINT_MISMATCH` or `IP_BLOCKED` | P-002 → `COOLING_DOWN` |
| 3 | `_allocate()` finds no HEALTHY proxies → returns `None` | — | — |

When `rotate()` returns `None` (no available proxy), `worker._process_job()` hits:

```python
if new_proxy is None:
    logger.error("proxy pool exhausted for campaign %s (job %s)", ...)
    job.mark_failed(ErrorType.PROXY_BANNED)
    return
```

**J-015's final status: `FAILED`** (via `mark_failed(ErrorType.PROXY_BANNED)`).

**Proxies in `COOLING_DOWN`: P-001, P-002, P-003** — all three. P-004 was already `COOLING_DOWN` in the seed data. All four proxies in the pool are now unavailable.

---

### Step 5 — Root cause of proxy pool exhaustion

**How many proxies were actually IP-banned?**

Only **P-003** received an `IP_BLOCKED` error — the unambiguous signal of an IP-level ban.

**P-001 and P-002** were rotated because of `SESSION_FINGERPRINT_MISMATCH` — a token/identity mismatch, not an IP block. These proxies were not banned by the target site; they were rotated unnecessarily because `ErrorClassifier` maps all 403 responses to `ROTATE_PROXY`.

**Root cause:**

`ErrorClassifier.classify()` treats all HTTP 403 responses identically — mapping every 403 to `ErrorType.PROXY_BANNED` and `RemediationAction.ROTATE_PROXY` — without reading the response body to distinguish the error category. A `SESSION_FINGERPRINT_MISMATCH` 403 indicates that the `cf_clearance` cookie is invalid for this proxy's IP; the correct remediation is to re-solve the CAPTCHA on the current proxy, not to rotate the proxy and waste a healthy resource.

**Single conceptual change to `ErrorClassifier` that would prevent the cascade:**

Distinguish 403 responses by their error code: treat token/session errors (`SESSION_FINGERPRINT_MISMATCH`, `TOKEN_INVALID`, `CAPTCHA_REQUIRED`) as `RemediationAction.RESOLVE_CAPTCHA` (keep the proxy, re-solve) and treat IP-level errors (`IP_BLOCKED`, `ACCESS_DENIED`) as `RemediationAction.ROTATE_PROXY`. This would have kept P-001 and P-002 in the pool, resolving the fingerprint mismatch by solving a fresh CAPTCHA rather than discarding a healthy proxy.

---

## Q5 — Evaluate the Fix

### Fix A — Response body inspection in `ErrorClassifier`

**(a) Does Fix A address the root cause of the cascade in Q4?**

**Partially.** Fix A correctly identifies that the classifier needs to distinguish 403 error codes and applies different remediations. For the `SESSION_FINGERPRINT_MISMATCH` case in Q4, it would return `RESOLVE_CAPTCHA` instead of `ROTATE_PROXY`, keeping P-001 and P-002 in the pool and triggering a CAPTCHA re-solve instead of a destructive rotation. This directly addresses the cascade.

However, it is partial because:

1. It relies entirely on the target site returning a parseable JSON body with a consistent `error` field — a fragile assumption addressed in part (b).
2. It does not address the deeper architectural flaw: that `cf_clearance` cookies from a different proxy are being presented without re-solving (the issue identified in Q2). Fix A would reduce proxy churn on fingerprint-mismatch errors but would not fix the upstream cause of those errors.

**(b) What happens when the site returns a 403 with an HTML body?**

Fix A reads `body = response.json_body or {}`. In `HttpClient.execute()`, `json_body` is only populated when `"application/json" in content_type` — a WAF HTML block page would have `Content-Type: text/html`, so `json_body` is `None`.

`body = None or {}` → `body = {}`.

`error_code = {}.get("error", "")` → `error_code = ""`.

The empty string does not match `"TOKEN_INVALID"`, `"TOKEN_EXPIRED"`, or `"CAPTCHA_REQUIRED"`.

Execution falls through to the default branch:

```python
return ErrorEvent(
    error_type=ErrorType.PROXY_BANNED,
    http_status=403,
    remediation=RemediationAction.ROTATE_PROXY,
    detail="",
)
```

Fix A silently degrades to the original behaviour for HTML 403s — proxy rotation — which may or may not be appropriate. A WAF block page is genuinely an IP-level rejection, so proxy rotation is correct in that case. But a Cloudflare challenge page returned as a 403 with HTML body would also fall through to rotation, which is also correct. The degradation is actually safe for this specific scenario, but the silent fallback is fragile: an operator diagnosing issues would see `detail=""` rather than a meaningful error code, making investigation harder.

---

### Fix B — Invalidate session on proxy rotation

**(a) Does Fix B address the root cause of the cascade in Q4?**

**Partially, but introduces new problems.** The root cause of the cascade in Q4 is that a `cf_clearance` token solved on one proxy's IP is presented on a different proxy's IP, causing `SESSION_FINGERPRINT_MISMATCH`. Fix B addresses this by clearing `cf_clearance` from the session whenever a proxy rotation occurs, forcing a fresh solve on the new proxy. This prevents the fingerprint mismatch error that caused P-001 and P-002 to be incorrectly rotated.

However, Fix B does not prevent the initial unnecessary rotation: P-003 receives an `IP_BLOCKED` 403 (a genuine ban), the classifier correctly rotates it, then Fix B clears the session. That part is correct. But if the original 403 were a `SESSION_FINGERPRINT_MISMATCH`, the classifier still calls `ROTATE_PROXY` before Fix B gets a chance to clear the session — meaning Fix B corrects the *next* attempt after an unnecessary rotation, but does not prevent the unnecessary rotation from happening in the first place.

**(b) Consequences for `captcha_solves_used` across 3 proxy rotations**

Without Fix B: if the session carries a valid `cf_clearance`, the `"cf_clearance" not in cookies` check is False, and `_solve_captcha` is never called on a rotation — solves are only used on the first attempt or after the cookie is absent.

With Fix B: every proxy rotation calls `session_manager.invalidate_session(campaign_id)`, which (as implemented) removes the session snapshot. On the next attempt, `restore_session()` falls back to `get_session_cookies()`, which returns the account's stored cookie jar. If the account's cookie jar still has `cf_clearance` (updated by `store_session()` on prior successes), the token may still be present. If it is, no new solve is triggered.

The real effect depends on whether `invalidate_session` also clears `cf_clearance` from the account's cookie jar (it does not — `invalidate_session` only removes the `_sessions` dict entry; `account.cookies` is untouched). So Fix B as written may not reliably force a re-solve.

Assuming Fix B is extended to also clear `cf_clearance` from the account cookies: across a job requiring 3 proxy rotations before success, `captcha_solves_used` would increment **once per rotation** (3 times) plus once for the initial solve (if starting without a token) = **up to 4 solves** for a job that ultimately succeeds. With a `retry_budget_per_job` of 5, a job requiring 3 rotations consumes 4 budget units — tight but feasible.

**(c) Does Fix B comply with AGENTS.md Session Management standards?**

**No.** The AGENTS.md Session Management section states:

> *"Session state — including cookies, browser headers, and clearance tokens — is managed exclusively by `SessionManager`. Callers must not manipulate session components directly."*

Fix B places session invalidation logic inside `ProxyPool.rotate()`, which gives `ProxyPool` a direct dependency on `SessionManager` and allows it to manipulate session state. This violates the single-responsibility boundary: `ProxyPool` is responsible for proxy lifecycle; `SessionManager` is the single source of truth for session state. `ProxyPool` calling `session_manager.invalidate_session()` creates cross-layer coupling that the architecture explicitly prohibits.

**Does it matter?** Yes, for two reasons. First, it introduces a hidden dependency between `ProxyPool` and `SessionManager` that makes both harder to test in isolation and creates ordering guarantees between the two services that are not captured by their interfaces. Second, it violates the "coherence guarantees" of `SessionManager` — if future code also manipulates session state from a third location, the single-source-of-truth guarantee breaks down completely.

---

### The Correct Fix

**(d) Root cause and correct approach**

**Root cause in one sentence:**

`cf_clearance` cookies are IP-bound (Cloudflare ties them to the solving IP), but the system presents them on different proxies without re-solving, and `ErrorClassifier` cannot distinguish an IP-level ban from a token-level fingerprint mismatch because it never reads the response body.

**Layers that need to change and what changes:**

**Layer 1 — `http_client.py` / `ErrorClassifier`:**

The classifier must be extended to inspect the response body (when available as JSON) and distinguish error categories:

- Token/session errors (`SESSION_FINGERPRINT_MISMATCH`, `TOKEN_INVALID`, `TOKEN_EXPIRED`, `CAPTCHA_REQUIRED`, `CF_CLEARANCE_INVALID`) → `RemediationAction.RESOLVE_CAPTCHA`. The proxy is not at fault; keep it, re-solve the CAPTCHA.
- IP-level bans (`IP_BLOCKED`, `ACCESS_DENIED`) → `RemediationAction.ROTATE_PROXY`. The proxy IP is rejected; rotate it.
- HTML/unreadable 403 body → `RemediationAction.ROTATE_PROXY` as the safe default (conservative, consistent with current behaviour).

**Layer 2 — `worker.py` / `_process_job()`:**

A new `RemediationAction.RESOLVE_CAPTCHA` branch must be added to handle the new remediation. When triggered, the worker must:

1. Clear `cf_clearance` from the current cookies dict (the local copy being used for this attempt).
2. Call `_solve_captcha(job, proxy.id)` to obtain a fresh token on the current proxy.
3. If solve succeeds, insert the new token into cookies and retry the request on the same proxy (no rotation).
4. If solve fails (provider error), handle per existing budget logic.

This keeps healthy proxies in the pool and fixes the fingerprint mismatch without wasting resources.

**Layer 3 — `session_manager.py` / Session restoration on proxy rotation:**

The session manager's `restore_session()` should not blindly carry `cf_clearance` across proxy rotations. The worker, after a successful proxy rotation, should call a new `SessionManager` method — e.g., `clear_clearance_token(job_id)` — to strip the stale `cf_clearance` from the restored session context before the next attempt. This ensures that after any `ROTATE_PROXY` remediation, the next attempt begins without a clearance token, triggering the CAPTCHA solve path on the new proxy's IP. This is the correct place for session state mutation, keeping the change inside `SessionManager` and respecting the architecture boundary.

**Conceptual `ErrorClassifier` decision logic (post-fix):**

```
classify(response):
  if status != 403: handle 401/429/404/5xx as before

  # 403 path
  error_code = response.json_body.get("error") if response.json_body else None

  if error_code in TOKEN_ERRORS:          → RESOLVE_CAPTCHA (keep proxy, re-solve)
  if error_code in IP_BAN_ERRORS:         → ROTATE_PROXY (ban the proxy)
  if error_code is None (HTML body):      → ROTATE_PROXY (safe default)
  else (unknown code):                    → ROTATE_PROXY (conservative default)
```

**Fixed session restoration flow on retry with a new proxy:**

1. `ROTATE_PROXY` remediation fires → `proxy_pool.rotate()` returns new proxy.
2. Worker calls `session_manager.clear_clearance_token(job)` — removes `cf_clearance` from the session snapshot and from the account cookie cache.
3. Next attempt: `restore_session(job)` returns cookies **without** `cf_clearance`.
4. `if "cf_clearance" not in cookies` → True → `_solve_captcha(job, new_proxy.id)` is called.
5. Fresh token is solved on the new proxy's IP.
6. Request succeeds; `store_session()` persists the new token bound to the new proxy.
