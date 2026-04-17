# GenieX Python Automation Assessment — Solutions

**Role:** Python Automation Developer

---

## Q1 — Architecture: Parse Success and Content Validity

### (a)

The worker treats **HTTP 200 as proof that the response body is a real product page worth parsing**. When `parse_result.success` is `True`, it immediately calls `mark_completed()` — so it assumes **200 implies meaningful product HTML**, not an interstitial or block page.

That assumption breaks on Cloudflare-protected sites because Cloudflare often serves a **JavaScript challenge page or interstitial** with **HTTP 200** and HTML that is structurally valid but not product content (challenge scripts, “checking your browser”, etc.).

### (b)

`PriceParser.parse()` validates **structural** integrity only: non-empty HTML, length ≥ `config.scraper.min_page_size_bytes` (1000 bytes), and successful BeautifulSoup parsing. It does **not** verify that the page is actually a product SKU page.

A **Cloudflare challenge page** (or similar gate page) can exceed the minimum size, parse as HTML, and have **no** `span.product-price` / `div.stock-status` matches — yielding `success=True` with both `price` and `available` as `None`.

The docstring says non-product pages are **“expected to have been filtered upstream”** by the HTTP client and error classifier — i.e. **only non-200 or classified errors** should be stopped before parsing. That fails here because **challenge pages return 200**, so they are never filtered.

### (c)

Detection must happen **before** treating the response as a successful fetch — **in `HttpClient`/`ErrorClassifier` or an explicit validation step in `worker.py` after fetch but before `PriceParser.parse()`** (still driven by classification rules, not ad-hoc scraper logic).

`PriceParser` only sees a string of HTML; it **cannot know** whether that HTML came from Cloudflare vs the origin without signals (status, headers, URL, body markers). Any fix that only changes `PriceParser` does not run **before** `parse()` on the 200 path in `worker.py`, so **`worker.py` must participate** (e.g. call a validator or branch on a new classification result).

### (d)

A correct fix adds a **content gate** on the HTTP 200 path: after fetch, inspect **HTML markers** typical of Cloudflare challenges (e.g. `cf-browser-verification`, Turnstile/challenge iframes, `Just a moment`, Ray ID blocks, or absence of product schema) and/or **response headers** (`cf-mitigated`, `server: cloudflare` with challenge patterns). If the page is classified as non-product, **do not** call `mark_completed()`; either retry with remediation or fail with a dedicated error. That logic should live in the **classification / worker orchestration layer** per `AGENTS.md`, not buried inside `PriceParser` alone.

---

## Q2 — Trace the Token

Ground truth: `candidate/seed_jobs.py` (`ALL_PROXIES`). Snapshot time **2025-02-06 09:15 UTC**.

### Step 1

`ErrorClassifier.classify()` **does not inspect the JSON body**. For **any** `status_code == 403` it returns:

| Field | Value |
|--------|--------|
| `error_type` | `ErrorType.PROXY_BANNED` |
| `remediation` | `RemediationAction.ROTATE_PROXY` |

In `worker.py`, when remediation is `ROTATE_PROXY`, the worker calls `proxy_pool.rotate(proxy.id, campaign_id)`. If a replacement proxy is returned, it sets `job.assigned_proxy_id`, increments `retry_count`, and **`continue`s** the retry loop. If `rotate()` returns `None`, it **`mark_failed(ErrorType.PROXY_BANNED)`** and returns.

### Step 2

**P-001**

- **Status:** `HEALTHY` in seed; comments state the **sticky TTL expired** before the snapshot, so `is_sticky_active()` is **False**.
- **Allocation rules:** `_allocate()` skips proxies that are not `HEALTHY`, have `consecutive_failures >= config.proxy.max_consecutive_failures` (3), or are **sticky to another campaign** (`proxy.id in active_assignments` and `assigned_campaign_id != campaign_id` **and** `is_sticky_active()`).

With default seed fields, **P-001 is available**: it is `HEALTHY`, `consecutive_failures` is below threshold, and it is not blocked by an active sticky hold for a *different* campaign in the usual reading of the seed comments.

**P-002**

- **HEALTHY** with `sticky_until` **after** the snapshot (09:25 vs 09:15) — **sticky is still active** for its prior assignment context.

**What `rotate()` returns**

`_allocate()` walks `self._proxies.values()` in **insertion order** (P-001 → P-002 → P-003 → P-004). After P-003 is marked `COOLING_DOWN`, the first **HEALTHY** proxy that passes the filters is **`P-001`**.

So **`rotate("P-003", campaign_id)` returns `P-001`**, not P-002.

*(The question text for Step 3 asks you to “assume P-002” as the next proxy. With seed defaults and `proxy_pool.py` as written, the **first** replacement after P-003 is **P-001**. The classification and remediation behavior is the same on whichever proxy is assigned next.)*

### Step 3 (403 with `SESSION_FINGERPRINT_MISMATCH`)

Again, **`classify()` ignores the JSON payload** for 403:

- Returns **`PROXY_BANNED`** and **`ROTATE_PROXY`** (same as Step 1).

**Remediation:** `rotate()` is invoked for the current proxy (**P-001** per code, or **P-002** if you follow the question’s simplifying assumption). That proxy is set to **`COOLING_DOWN`** via `_mark_cooling_down()` (increments `ban_count`, clears assignment/sticky fields).

### Step 4 — Rotation loop (code order)

Using **`seed_jobs.py` + `proxy_pool._allocate()`** order after each 403 → `ROTATE_PROXY`:

| Step | Rotated out | Why | Healthy proxies left (P-004 already `COOLING_DOWN`) |
|------|-------------|-----|-----------------------------------------------------|
| After P-003 fails | **P-003** | 403 → `ROTATE_PROXY` | **P-001**, **P-002** |
| Worker continues on **P-001**; 403 fingerprint | **P-001** | Same 403 mapping | **P-002** only |
| Worker continues on **P-002**; 403 again | **P-002** | Same 403 mapping | **None** (`P-004` still not `HEALTHY`) |

When **`rotate()`** calls **`_allocate()`** with **no** remaining `HEALTHY` proxies, it returns **`None`**. The worker then **`mark_failed(ErrorType.PROXY_BANNED)`** (see `worker.py` when `new_proxy is None`).

**J-015 final status:** **`FAILED`** with **`PROXY_BANNED`** once the pool cannot supply another proxy.

**`COOLING_DOWN` count:** **P-004** (seed) plus **P-003**, **P-001**, and **P-002** rotated during this sequence → **4** proxies in **`COOLING_DOWN`**.

### Step 5

**IP-banned vs “other” (per response semantics, not classifier behavior)**

- **Actually IP-banned by the site:** **one** clear case — the first response with explicit **`IP_BLOCKED`** (and `cf-ray`).
- **Rotated for other reasons:** **`SESSION_FINGERPRINT_MISMATCH`** is an **identity/session binding** issue (token vs egress path / fingerprint), not necessarily an IP ban — but the classifier still rotates.

**Root cause of pool exhaustion**

`ErrorClassifier` maps **every** 403 to **`PROXY_BANNED` + `ROTATE_PROXY`**. After a proxy change, the worker can still reuse **`cf_clearance`** from `restore_session()` (no new solve), so the next request can fail with **fingerprint mismatch**; each failure **burns another proxy** until the pool is empty.

**Single conceptual change**

For **403**, inspect the **JSON `error` / `message` (or similar)** and treat **`SESSION_FINGERPRINT_MISMATCH`** (and similar) as **`RemediationAction.RESOLVE_CAPTCHA`** (or equivalent: refresh clearance **without** treating the proxy as banned), **not** unconditional `ROTATE_PROXY`.

### Bonus — `captcha_solver.py`

The client uses **`AntiTurnstileTaskProxyLess`**: CapSolver solves **off-proxy**. Cloudflare ties the token to the **client IP path** used with the site.

To align token with the **job’s proxy IP**, switch to a **proxy-backed** Turnstile task (CapSolver types that accept **proxy type/host/port/credentials**) and pass the **same proxy** the worker uses for `HttpClient`, so the solved token is valid for that egress IP.
