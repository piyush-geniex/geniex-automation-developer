# Solution

---

## Q1 — Architecture: Parse Success and Content Validity

### (a)

Looking at `worker.py`, there's a clear implicit assumption baked into the control flow: if the HTTP response comes back as 200, the worker trusts that the body is a real product page and sends it straight to `PriceParser.parse()`. The `ErrorClassifier` only ever gets called for non-200 responses.

```python
if response.status_code != 200:
    error_event = self._classifier.classify(response, job_id=job.id)
    ...

# HTTP 200 — parse the response body
parse_result = self._parser.parse(response.body, url=job.url)
```

The problem is that Cloudflare doesn't always use non-200 status codes when blocking or challenging a request. When a `cf_clearance` cookie is missing, expired, or bound to a different IP than the one making the request, Cloudflare can serve a JS challenge page with HTTP 200. The body is perfectly valid HTML — usually around 12–14 KB — but it's Cloudflare's challenge JavaScript, not the product page the worker was expecting. Since it arrives as a 200, the error classifier never gets a chance to see it, and the worker happily passes it along to the parser.

### (b)

`PriceParser.parse()` only checks structural integrity before declaring success. It returns `success=True` as long as:

- The HTML isn't empty or `None`
- It's at least 1000 bytes (`min_page_size_bytes` from config)
- `BeautifulSoup` can parse it without throwing an exception

A Cloudflare challenge page clears all three of these checks easily — it's ~14 KB of well-formed HTML. Once structural validation passes, the parser tries to find `span.product-price` and `div.stock-status` in the DOM. Neither exists in a challenge page, so both `_extract_price()` and `_extract_availability()` return `None`. The final result is `ParseResult(success=True, price=None, available=None)`.

Campaign C-003 in `seed_jobs.py` is the proof. All 10 jobs reached `COMPLETED`, but 7 of them (J-104 through J-110) have `price=None`, `available=None`, and `raw_html_size=14200`. The comments in the seed data confirm these were Cloudflare JS challenge pages served as HTTP 200. The campaign ends with a 100% success rate but only 30% price coverage — a misleading picture.

The docstring on `PriceParser` (line 40–45) actually acknowledges this gap explicitly:

> Does not distinguish between product pages and non-product pages (challenge pages, access-denied pages, maintenance pages) — those are expected to have been filtered upstream by the HTTP client before the response body reaches this method.

So the parser is saying: "I don't check whether this is actually a product page — someone upstream should have done that." But that upstream filtering only happens for non-200 responses. Challenge pages that arrive as 200 fall through the crack entirely.

### (c)

The detection needs to happen in `worker.py`, between the `status_code == 200` check and the `parse()` call. That's the only place where you can both identify a challenge page and trigger the right remediation (invalidating the stale `cf_clearance` and re-solving the CAPTCHA). You could also expand `ErrorClassifier` to handle 200-with-challenge scenarios, but either way the worker needs to be involved.

You can't fix this inside `PriceParser` alone. If the parser returned `success=False` for challenge pages, the worker would treat it as a generic "structural parse failure" — it would just bump `retry_count`, loop back, and send the exact same request with the same stale cookies through the same proxy. You'd get the same challenge page again and again until retries run out. The correct response to a challenge page is to clear the old `cf_clearance` and re-solve the CAPTCHA, and that flow lives in the worker, not the parser.

Beyond the practical issue, there's also an architectural one. The parser's job is to extract data from product pages, not to figure out what kind of page it's looking at. Content classification — "is this a product page or a Cloudflare interstitial?" — is a concern that belongs upstream, tied to the session/CAPTCHA logic that `worker.py` orchestrates.

---

## Q2 — Trace the Session

### Step 1

`_process_job(J-001)` starts, attempt 0.

The worker calls `session_manager.get_session_cookies(J-001)`. J-001 is assigned to `ACC-001`, so the method returns a deep copy of ACC-001's cookie jar. Looking at the seed data, ACC-001 has three cookies:

- `cf_clearance` = `"6Yz3mP8nRkXvQ1sL-1707210000-0-AY3zxNBP9wK7jT2qSm..."` — and the comment in `seed_jobs.py` is very specific about this: *"This cf_clearance token was solved by P-003 during the C-003 campaign run. It encodes the Cloudflare verification context for P-003's IP address. P-003 is the proxy that obtained this clearance; subsequent requests using this cookie must originate from the same IP for Cloudflare to honour it."*
- `session_id` = `"sess_acc001_cf_legacy"`
- `_cf_bm` = `"xK9pLmN2qRvS4tUw6yZa8bCd0eF"`

So the key thing here is: the `cf_clearance` was obtained through P-003, not P-001.

### Step 2

The worker checks `if "cf_clearance" not in cookies`. Well, `cf_clearance` is definitely in there — it was in ACC-001's cookie jar. So the condition is `False`, and the worker skips CAPTCHA solving entirely. It trusts that the existing token is good to go, because `AGENTS.md` says clearance cookies are "session-scoped, not IP-scoped" and can be safely reused across proxies.

### Step 3

The request goes out through P-001, carrying the `cf_clearance` cookie that was originally solved by P-003. But Cloudflare checks the incoming IP against the one that solved the challenge. P-001's IP doesn't match P-003's IP. Cloudflare rejects the clearance and serves a JS challenge page back — with HTTP 200, not 403. This is the same behavior that created the false positives in Campaign C-003: valid HTML, around 12 KB, no product content.

### Step 4

`http_client.execute()` returns a response with `status_code=200`. Since it's a 200, the `ErrorClassifier` is never called. No error event is generated.

The challenge page HTML (~12 KB) goes straight to `PriceParser.parse()`. It passes structural validation — it's non-empty, well above the 1000-byte minimum, and BeautifulSoup parses it fine. `success=True`. The CSS selectors for price and availability find nothing: `price=None`, `available=None`.

The worker sees `parse_result.success == True` and goes down the success path. It calls `store_session()` (persisting the cookies — including the stale `cf_clearance`), `record_success()` on P-001, and finally `job.mark_completed(parse_result)`.

J-001 is now `COMPLETED` with null price and availability data. The system just recorded a Cloudflare challenge page as a successful product fetch.

### Step 5

Fifteen minutes later. P-001's `sticky_until` was `"2025-02-06T09:05:00+00:00"` — that's already 10+ minutes expired by now.

A second worker calls `proxy_pool.acquire(campaign_id="C-002")`. The pool doesn't have an existing assignment for C-002, so `_allocate("C-002")` runs. It iterates over the proxies:

- P-001 is `HEALTHY`, and its stickiness to C-001 has expired. It's free to be reallocated. P-001 gets assigned to C-002.

So `acquire()` returns P-001.

### Final state

C-001 is `COMPLETED`. All 5 jobs went through P-001 with the same stale `cf_clearance`, each received the same challenge page, each was silently marked `COMPLETED` with null data. `success_rate()` = 100%, `price_coverage()` = 0%.

C-002 has just started — its worker got P-001 assigned and is beginning to process jobs.

On the dashboard, C-001 looks like a complete success. An operator looking at J-001 sees `COMPLETED` with `price=None`. According to `AGENTS.md`, that's valid business data — the SKU is "currently unlisted." The operator has no reason to suspect anything is wrong. In reality, the system never even reached the product page; it was fed Cloudflare challenge HTML and treated it as ground truth.

---

## Q3 — Predict the Failure

### (a)

J-001 is a fresh job with no cookies and no `cf_clearance`. Here's what happens:

The worker sets `job.status = IN_PROGRESS` and enters the retry loop. On attempt 0, the budget check passes (`0 >= 5` is false), a proxy is acquired, and `get_session_cookies()` returns cookies without `cf_clearance` (per the question's premise). Since there's no clearance token, the worker calls `_solve_captcha()`.

Inside `_solve_captcha()`:
1. The budget guard at the top passes: `0 >= 5` → false.
2. `captcha_solves_used` is incremented to 1.
3. `solve_turnstile()` raises `CaptchaProviderError("503 Service Unavailable")`.
4. The `except CaptchaProviderError` handler catches it, logs a warning, and returns `None`.

Here's the important thing: `_solve_captcha()` does not call `mark_exhausted()` in the error-handling path. The only place `mark_exhausted()` gets called is in the budget-check guard at the very top. A provider error just results in `return None` — no status change on the job.

Back in `_process_job()`, the code sees `solve_result is None` and hits `return`, exiting the entire function. The comment there says "Budget exhausted inside _solve_captcha" — but that's misleading. The budget wasn't exhausted; the provider just failed. Either way, `_process_job()` is done.

J-001 ends up in `IN_PROGRESS` status with `captcha_solves_used=1` and `retry_count=0`. It was never moved to any terminal state. The same thing happens for J-002 through J-005 — each gets one failed solve attempt and gets left in `IN_PROGRESS`.

### (b)

When CapSolver comes back at 14:10 UTC, the operator checks the dashboard and sees all 5 jobs stuck in `IN_PROGRESS`.

There's no automatic retry path. `worker.run()` only picks up jobs in `PENDING` or `RETRYING` status:

```python
if job.status not in (JobStatus.PENDING, JobStatus.RETRYING):
    continue
```

`IN_PROGRESS` isn't in that set. If you re-invoke the worker on C-001, it will skip all 5 jobs. The campaign is stuck in limbo — not completed, not failed, just frozen. The campaign status was never finalized either, since `all_terminal` is `False` (IN_PROGRESS isn't terminal).

The operator would need to manually reset the jobs back to `PENDING` and re-dispatch the campaign. There's no self-healing code path for this situation.

### (c)

The question asks how to distinguish these scenarios after the fact, using data on `Job`, `JobResult`, and `Campaign`.

For Scenario A (provider down), the most telling signal is that `job.error_log` is empty. `CaptchaProviderError` gets caught inside `_solve_captcha()` and logged to the application log, but it's never turned into an `ErrorEvent` and never appended to `job.error_log`. The system simply never reaches the HTTP request phase, so there are no classified errors to record. You'd also see low `retry_count` (0 per campaign dispatch, since the job exits before any retry logic runs) and low `captcha_solves_used` (1 per dispatch).

For Scenario B (proxies blocked), `job.error_log` would be full of `ErrorEvent` entries with `error_type=PROXY_BANNED` and `http_status=403`. Each 403 response goes through the classifier and gets recorded. `retry_count` and `captcha_solves_used` would both be higher because the system is actually completing CAPTCHA solves successfully and then getting blocked on the subsequent request.

| Field | Scenario A (Provider Down) | Scenario B (Proxies Blocked) |
|---|---|---|
| `error_log` | Empty | ErrorEvents with PROXY_BANNED / 403 |
| `retry_count` | 0 per dispatch | Multiple per dispatch |
| `captcha_solves_used` | 1 per dispatch | Multiple per dispatch |

The clearest differentiator is `error_log`. If it's empty on an `EXHAUSTED` job, the system never got to make an HTTP request — that points to infrastructure failure (CAPTCHA provider down, network issues). If it has 403 entries, the target site was actively blocking requests.

Worth noting: there's an observability gap here. `CaptchaProviderError` doesn't leave any trace in the job's persisted data — only in application logs. If those logs aren't available, the operator can only infer Scenario A from the absence of error entries, which is a weak signal. Ideally, provider failures would be recorded as structured events on the job model too.

---

## Q4 — Trace the Token

### Step 1

The worker sends a request for J-015 through P-003. The target site comes back with:

```
HTTP 403
{"cf-ray": "7f3a1b2c3d4e5f6a", "error": "IP_BLOCKED", "message": "This IP address has been blocked."}
```

`ErrorClassifier.classify()` sees `status == 403` and returns `ErrorType.PROXY_BANNED` with `RemediationAction.ROTATE_PROXY`. The classifier doesn't look at the response body at all — every 403 gets the same treatment regardless of whether it's `IP_BLOCKED`, `SESSION_FINGERPRINT_MISMATCH`, or anything else.

The worker receives `ROTATE_PROXY` and calls `proxy_pool.rotate("P-003", campaign_id)`.

### Step 2

`rotate()` first marks P-003 as `COOLING_DOWN` (`ban_count` goes from 2 to 3, `assigned_campaign_id` cleared, `sticky_until` cleared). Then it pops the C-002 assignment and calls `_allocate()` to find a replacement.

Looking at the seed data for what's available:

- P-001: `HEALTHY`, but its `sticky_until` is `"2025-02-06T09:05:00+00:00"` — expired 10 minutes ago. Its `assigned_campaign_id` is `"C-001"`, but since the stickiness has expired, it's not reserved. P-001 is available.
- P-002: `HEALTHY`, `sticky_until` is `"2025-02-06T09:25:00+00:00"` — still active (we're at 09:15). It's assigned to C-001, and since its stickiness is active and it belongs to a different campaign, it gets skipped.

So `rotate()` returns P-001.

(The question tells us to assume P-002 from Step 3 onward. This could happen if P-001 was already used up or rotated out in a slightly different scenario. Going with P-002 as instructed.)

### Step 3

With P-002 assigned, the worker calls `_solve_captcha()`, gets a fresh Turnstile token, and sends the request through P-002.

The site comes back with:

```
HTTP 403
{"error": "SESSION_FINGERPRINT_MISMATCH", "message": "Session identity is inconsistent."}
```

`ErrorClassifier.classify()` sees 403 and does exactly the same thing as before: `PROXY_BANNED`, `ROTATE_PROXY`. It doesn't distinguish `SESSION_FINGERPRINT_MISMATCH` from `IP_BLOCKED` at all.

The worker calls `proxy_pool.rotate("P-002", campaign_id)`. P-002 goes to `COOLING_DOWN`. But P-002 wasn't IP-banned — this was a session/fingerprint mismatch. The proxy itself is perfectly healthy.

### Step 4

After P-002 is rotated, `_allocate()` looks for the next available proxy. At this point, P-001 would be the only healthy option (assuming it wasn't rotated earlier). P-001 gets allocated.

The request through P-001 runs into the same kind of problem — the session state still carries fingerprint data tied to a different proxy — and gets another 403 with `SESSION_FINGERPRINT_MISMATCH`. The classifier does its thing: `PROXY_BANNED`, `ROTATE_PROXY`. P-001 goes to `COOLING_DOWN`.

Now every proxy in the pool is down:

| Proxy | Status | Actual reason |
|---|---|---|
| P-001 | COOLING_DOWN | Session mismatch — not actually banned |
| P-002 | COOLING_DOWN | Session mismatch — not actually banned |
| P-003 | COOLING_DOWN | Legitimately IP-blocked |
| P-004 | COOLING_DOWN | Already was (from account-sharing detection in seed data) |

`rotate()` calls `_allocate()`, finds no healthy proxies, returns `None`. The worker hits the `new_proxy is None` branch and calls `job.mark_failed(ErrorType.PROXY_BANNED)`.

J-015's final status: `FAILED` with `error_type=PROXY_BANNED`. All 4 proxies are in `COOLING_DOWN`.

### Step 5

Of the three proxies that entered `COOLING_DOWN` during this sequence, only P-003 was actually IP-banned by the target site. P-002 and P-001 were rotated because of `SESSION_FINGERPRINT_MISMATCH` — a session/token problem, not an IP problem. These were healthy proxies that got burned for the wrong reason.

The root cause is that `ErrorClassifier` lumps every 403 into `PROXY_BANNED` and prescribes `ROTATE_PROXY`, no matter what the response body says. Token and session mismatches — things that could be fixed by just re-solving the CAPTCHA on the same proxy — instead trigger proxy rotation. And because `restore_session()` preserves the stale `cf_clearance` across proxy changes (based on the incorrect assumption that it's session-scoped, not IP-scoped), each new proxy inherits the same bad token and hits the same mismatch. The cascade eats through the whole pool.

The fix is conceptually straightforward: the classifier should read the 403 response body and distinguish IP-level bans (`IP_BLOCKED`, `ACCESS_DENIED`) from token/session issues (`SESSION_FINGERPRINT_MISMATCH`, `TOKEN_INVALID`, `TOKEN_EXPIRED`, `CAPTCHA_REQUIRED`). IP bans should still get `ROTATE_PROXY`. Token/session failures should get a different remediation — something like `RESOLVE_CAPTCHA` — that tells the worker to invalidate the stale `cf_clearance`, re-solve the CAPTCHA on the same proxy, and retry without wasting the proxy.

---

## Q5 — Evaluate the Fix

### Fix A — Response Body Inspection in `ErrorClassifier`

#### (a)

Fix A partially addresses the problem. The idea is right — inspect the 403 body to classify by cause instead of treating all 403s the same. For `TOKEN_INVALID`, `TOKEN_EXPIRED`, and `CAPTCHA_REQUIRED`, it returns `RESOLVE_CAPTCHA` instead of `ROTATE_PROXY`. That's the correct behavior for those errors.

But the error-code list is incomplete. The error that actually drove the Q4 cascade — `SESSION_FINGERPRINT_MISMATCH` — isn't in the set `("TOKEN_INVALID", "TOKEN_EXPIRED", "CAPTCHA_REQUIRED")`. It falls through to the default: `PROXY_BANNED` / `ROTATE_PROXY`. So for the exact scenario in Q4, Fix A doesn't change anything. P-002 and P-001 would still be rotated out for fingerprint mismatches.

Fix A captures some token-related 403s but misses the specific failure that caused the cascade.

#### (b)

Fix A introduces a dependency on the response body being JSON with a consistent `error` field. Looking at how `HttpClient.execute()` works, it only attempts JSON parsing when the response has `Content-Type: application/json`. If the target site (or Cloudflare) returns a 403 with an HTML body — which is what WAF block pages and challenge pages usually look like — then `response.json_body` will be `None`.

In Fix A's code, that means `body = response.json_body or {}` becomes `body = {}`, and `error_code = body.get("error", "")` returns `""`. An empty string doesn't match any token-error code, so the fix falls through to the default: `PROXY_BANNED` / `ROTATE_PROXY`. Right back where we started.

Since Cloudflare's WAF and interstitial pages are typically HTML, Fix A would silently fail in exactly the cases that matter most.

### Fix B — Invalidate Session on Proxy Rotation

#### (a)

Fix B takes a different approach and partially addresses the cascade — but in a roundabout way. The idea is: every time a proxy is rotated, clear the session state so the stale `cf_clearance` doesn't follow the job to the next proxy. The new proxy would then trigger a fresh CAPTCHA solve, get a token bound to its own IP, and (if its IP isn't banned) succeed.

For the Q4 scenario, this would help. After P-003 is correctly rotated for `IP_BLOCKED`, the session is cleared. P-002 gets a fresh token for its own IP. If P-002 is healthy, the request works and the cascade never starts.

But Fix B doesn't address the underlying misclassification. If a `SESSION_FINGERPRINT_MISMATCH` happens for some other reason (not caused by a stale token from a rotated proxy), the classifier would still call it `PROXY_BANNED` and rotate the proxy unnecessarily. Fix B reduces the damage of the cascade but doesn't fix the classification logic that triggers it.

There's also a practical bug in Fix B's implementation. It calls `invalidate_session(campaign_id)`, but `SessionManager.invalidate_session()` takes a `job_id` — its internal `_sessions` dict is keyed by job ID. Passing a campaign ID would just look up the wrong key and find nothing. And even if you fixed the argument, `invalidate_session()` only removes the persisted session snapshot. It doesn't clear `cf_clearance` from the account's cookie jar. So on the next attempt, `restore_session()` falls back to `get_session_cookies()`, which returns `account.cookies` — still containing the stale `cf_clearance`.

#### (b)

Assuming Fix B worked as intended, every proxy rotation would force a CAPTCHA re-solve. For a job that needs 3 rotations before finding a working proxy:

- Initial request: solve CAPTCHA → `captcha_solves_used = 1`
- 403 → rotate #1, session cleared, re-solve → `captcha_solves_used = 2`
- 403 → rotate #2, session cleared, re-solve → `captcha_solves_used = 3`
- 403 → rotate #3, session cleared, re-solve → `captcha_solves_used = 4`
- Request succeeds

That's 4 solves out of a budget of 5. If the job needed a 4th rotation, it would hit the budget limit and be marked `EXHAUSTED` — even if the 5th proxy would have worked fine. Fix B essentially trades one cascade for another: instead of running out of proxies, you run out of CAPTCHA budget.

#### (c)

`AGENTS.md` is explicit about session ownership:

> Session state — including cookies, browser headers, and clearance tokens — is managed exclusively by `SessionManager`. Callers must not manipulate session components directly.

Fix B puts `invalidate_session()` inside `ProxyPool.rotate()`. That gives `ProxyPool` a direct dependency on `SessionManager` and makes it responsible for managing session state — which is exactly what `AGENTS.md` says not to do.

Does it matter? I think so. Proxy management and session management are separate concerns. Whether you need to clear the session after a proxy rotation depends on the reason for the rotation (IP ban vs. token mismatch), and that's a decision that belongs in the orchestration layer (`worker.py`), not in the proxy pool. Coupling these two layers together means any future change to session invalidation logic (e.g., clearing `cf_clearance` but keeping `session_id`) would require touching both `SessionManager` and `ProxyPool.rotate()`.

### (d) — The Correct Fix

The root cause, in one sentence: the classifier treats all 403 responses as IP-level bans requiring proxy rotation, while `cf_clearance` cookies (which are actually IP-scoped, despite what `AGENTS.md` claims) are carried over to new proxies unchanged, causing cascading session mismatches that burn through every healthy proxy in the pool.

The fix needs changes in multiple layers:

1. **`ErrorClassifier` in `http_client.py`** needs to look at the 403 response body, not just the status code. If there's a JSON body with an `error` field, use it to differentiate. IP-level bans (`IP_BLOCKED`, `ACCESS_DENIED`) → `ROTATE_PROXY`. Token/session issues (`TOKEN_INVALID`, `TOKEN_EXPIRED`, `CAPTCHA_REQUIRED`, `SESSION_FINGERPRINT_MISMATCH`) → a new `RESOLVE_CAPTCHA` remediation. For non-JSON bodies or unrecognized errors, default to `ROTATE_PROXY` as a safe fallback, but the worker should still clear the session in those cases to avoid stale-token cascades.

2. **`worker.py`** needs to handle `RESOLVE_CAPTCHA` as a remediation action. When it gets this signal: invalidate the current `cf_clearance` through `SessionManager` (not directly), re-solve the CAPTCHA on the same proxy, and retry. This preserves the healthy proxy and only costs one extra CAPTCHA solve. The worker also needs a new `SessionManager` method to clear `cf_clearance` from the account's cookie jar, since `invalidate_session()` alone only removes the session snapshot.

3. **`worker.py` (content validation)** should also check HTTP 200 responses for Cloudflare challenge page signatures before sending them to the parser. Look for known markers (`<title>Just a moment...</title>`, Turnstile JavaScript references, etc.). If it's a challenge page disguised as a 200, treat it the same as a token failure: clear `cf_clearance` and re-solve.

4. **`AGENTS.md`** needs to be corrected. The claim that `cf_clearance` is "session-scoped, not IP-scoped" is wrong — the seed data proves it, and real Cloudflare behavior confirms it. Fixing the docs prevents the next developer from making the same assumption and introducing the same class of bug.

This approach keeps the architecture clean: `ErrorClassifier` owns the classification, `worker.py` orchestrates the remediation, and `SessionManager` is the only thing that touches session state. No layer steps outside its responsibility.
