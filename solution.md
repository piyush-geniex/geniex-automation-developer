# Solution

## Q1 — Architecture: Parse Success and Content Validity

### (a)
The worker currently treats `HTTP 200` as “good content.”  
So once a request returns 200, it goes straight into parsing and can be marked completed if parsing succeeds.

That assumption breaks on Cloudflare-protected sites because Cloudflare can return a challenge/interstitial page with **status 200**. In that case, transport-level success is true, but content-level success is false. The HTML is valid, but it is not a real product page.

### (b)
`PriceParser.parse()` only checks structural validity before returning `success=True`:
- response is not empty
- response is above minimum size
- BeautifulSoup can parse it

It does **not** verify “this is definitely a product page.”  
Because of that, any well-formed non-product page (Cloudflare challenge, access/holding page, maintenance page) can return `success=True` with `price=None` and `available=None`.

The docstring says these pages are expected to be filtered upstream. That expectation fails because upstream classification is based on status code, and a Cloudflare challenge can come back as `HTTP 200`, so it slips through and gets treated as normal product HTML.

### (c)
Detection of challenge/access-denied content needs to happen **before** parse success is accepted by the worker, in the fetch/classification stage for 200 responses.

Putting the fix only in `PriceParser` is not enough, because `worker.py` currently interprets parser success as a terminal success path (`mark_completed`). If parser behavior changes (for example, introducing a “content invalid” outcome), worker logic must also be updated to route that result into retry/remediation instead of completion.

### (d)
A proper fix is to add content-validation for `HTTP 200` responses in the upstream classification flow, then let the worker handle that outcome through remediation logic (not completion). The validator should look for Cloudflare challenge signals such as `cf-chl-` markers, `challenge-platform` assets, Turnstile/challenge containers, challenge script paths, and known page text like “Checking your browser” or “Just a moment,” with optional header hints like `server: cloudflare` and `cf-ray`. Only responses that pass both transport and content checks should continue to product extraction. This prevents challenge HTML from being counted as successful product parses.

---

## Q2 — Trace the Token

### Step 1
For `HTTP 403` with `IP_BLOCKED`, `ErrorClassifier.classify()` returns:
- `ErrorType.PROXY_BANNED`
- `RemediationAction.ROTATE_PROXY`

`worker.py` responds by rotating the proxy, incrementing retry count, and retrying with the replacement proxy (or failing if no replacement exists).

### Step 2
`P-001` is available for reassignment because it is `HEALTHY` and its sticky window has expired in seed data.  
So when `rotate("P-003", campaign_id)` is called, `P-003` is moved to `COOLING_DOWN`, the campaign assignment is cleared, and `_allocate()` returns the next eligible healthy proxy. With current order/state, that is `P-001`.

### Step 3
For the second `403` (`SESSION_FINGERPRINT_MISMATCH`), classifier behavior is still the same because all 403s are handled identically:
- `ErrorType.PROXY_BANNED`
- `RemediationAction.ROTATE_PROXY`

So the active proxy in that step (as given in the prompt, `P-002`) also gets rotated into `COOLING_DOWN`.

### Step 4
From here, each new 403 keeps causing another rotation:
1. Current proxy is rotated out and set to `COOLING_DOWN`
2. Healthy available pool gets smaller
3. Eventually `rotate()` cannot allocate a replacement and returns `None`
4. Worker marks the job failed (`ErrorType.PROXY_BANNED`)

Final outcome:
- `J-015` ends in **`FAILED`**
- proxies in `COOLING_DOWN` become **4 total** (`P-001`, `P-002`, `P-003`, plus already-cooling `P-004`)

### Step 5
During this sequence:
- Truly IP-banned proxies: **1** (the explicit `IP_BLOCKED` event)
- Rotated for other causes: **the rest**

The core issue is that `403 -> PROXY_BANNED -> ROTATE_PROXY` is too broad. It treats IP bans and session/token identity mismatches as the same class of failure, which drains the proxy pool.

The conceptual fix is to split 403 classification into at least two buckets:
- real IP ban -> rotate proxy
- session/token/fingerprint mismatch -> refresh identity/token path (without automatically cooling the proxy)

**Bonus:** In `captcha_solver.py`, the solver uses `AntiTurnstileTaskProxyLess`, so solve IP can differ from request IP. To pass Cloudflare IP checks after rotation, use a proxy-aware Turnstile task and pass the same proxy endpoint/credentials used for the actual request so token issuance and request egress share identity.
