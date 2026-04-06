# Assessment Solution (Natural Language Version)

## Q1 — Architecture: Parse Success vs Content Validity

### (a) What assumption is being made, and when does it fail?

In `worker.py`, the system assumes that if `parse_result.success == true`, then the job has worked and can be marked as `COMPLETED`.

In simple terms:  
If we get a 200 response and valid HTML, we assume it’s a real product page.

This assumption breaks on sites protected by Cloudflare. Cloudflare can return a challenge page (like “Just a moment…”) that:
- Still has HTTP 200
- Contains valid HTML

So:
- Fetch layer thinks everything is fine
- Parser thinks HTML is valid
- Job gets marked COMPLETED

But in reality, it’s not a product page.

---

### (b) What does `PriceParser.parse()` actually check?

`PriceParser.parse()` only checks structure, not content.

It validates:
- HTML is not empty
- HTML size is above a minimum threshold
- It can be parsed by BeautifulSoup

It does not check:
- Whether it's actually a product page
- Whether price or availability exists

So if it receives a Cloudflare page:
- Price → None
- Availability → None
- Still returns success = true

---

### (c) Where should detection happen?

Detection should happen before marking the job as completed:
- In the HTTP layer (`http_client.py`)
- Or in the worker (`worker.py`)

We need a separate signal:
“Is this a real product page?”

---

## Q2 — Session Flow

- Cookies include `cf_clearance` from a different proxy
- Worker skips CAPTCHA solving
- Cloudflare detects mismatch → returns 403 or challenge page

If 403:
- Proxy rotated

If 200 challenge:
- Parsed as valid
- Job marked COMPLETED incorrectly

---

## Q3 — Provider Outage

- CAPTCHA solve fails → worker exits early
- Job remains IN_PROGRESS
- No automatic retry

After recovery:
- Manual retry required

---

## Q4 — Proxy Cascade

- All 403 treated as proxy ban
- Even session mismatch triggers rotation
- Proxies keep rotating → all go into cooling
- Eventually job fails

Root cause:
System treats all 403 errors the same

---

## Q5 — Fix Summary

### Fix A
- Improve error classification
- Works better but fails on HTML responses

### Fix B
- Clear session on rotation
- Increases CAPTCHA usage and breaks design

### Correct Approach

Fix:
1. Error classification (distinguish 403 types)
2. Worker logic (don’t complete on challenge page)
3. Session management (handled centrally)

Ideal behavior:
- 200 + challenge → retry
- 403 + session issue → refresh session
- 403 + IP block → rotate proxy
