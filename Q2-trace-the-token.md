# Q2 — Trace the Token

This question traces a specific execution path through four files. Use `seed_jobs.py` as ground truth for the starting state.

> *Suggested reading order: `http_client.py` (ErrorClassifier) → `proxy_pool.py` (rotate) → `seed_jobs.py` proxy comments.*

**Starting state**: A worker is retrying job **J-015** for a campaign that has been running through several proxies. The proxy pool's `acquire()` method returns **P-003** — which shows `status = HEALTHY` in the seed data.

---

**Step 1**

The worker sends a request through P-003. The target site returns:

```
HTTP 403
{"cf-ray": "7f3a1b2c3d4e5f6a", "error": "IP_BLOCKED", "message": "This IP address has been blocked."}
```

Read `ErrorClassifier.classify()` in `http_client.py`.

- What `ErrorType` is assigned?
- What `RemediationAction` is returned?
- What does `worker.py` do in response to this remediation action?

---

**Step 2**

`proxy_pool.rotate("P-003", campaign_id)` is called. P-003 is marked `COOLING_DOWN`.

The pool needs to assign the next available proxy. Read `proxy_pool.py` — specifically how it selects the next available proxy. Look at `seed_jobs.py` for the current state of P-001 and P-002.

- Is P-001 available for assignment? Why or why not?
- What proxy does `rotate()` return?

---

**Step 3**

The worker now has a new proxy assigned (assume **P-002**, the only remaining healthy proxy). It calls `_solve_captcha()`, gets a fresh token, and retries the request.

The site returns:

```
HTTP 403
{"error": "SESSION_FINGERPRINT_MISMATCH", "message": "Session identity is inconsistent."}
```

Read `ErrorClassifier.classify()` again.

- What does it return for this 403?
- What remediation action is triggered?
- What happens to P-002 in the proxy pool?

---

**Step 4**

Walk through the loop from this point. At each rotation, describe:
- Which proxy is being rotated out and why
- What the pool's available proxy count is
- What happens when `rotate()` has no available proxy to return

What is J-015's final status? How many proxies are in `COOLING_DOWN` state?

---

**Step 5**

Of the proxies that entered `COOLING_DOWN` during this sequence, how many were actually IP-banned by the target site? How many were rotated for other reasons?

What is the root cause of the proxy pool exhaustion? What single conceptual change to `ErrorClassifier` would prevent this cascade?

**Bonus:** Read `captcha_solver.py`. If your fix triggers a fresh CAPTCHA solve after a proxy rotation, what specifically would you change about the call to the CAPTCHA provider to ensure the new token will pass Cloudflare's IP check?
