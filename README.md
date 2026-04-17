# GenieX Python Automation Assessment

Welcome. This is a take-home assessment for the **Python Automation Developer** role at GenieX.

You are looking at a price intelligence platform that scrapes product availability and pricing from a Cloudflare-protected retailer website. The codebase is complete and operational — or so it appears.

Your task is to read it carefully and answer two questions.

---

## What this system does

The platform runs **scraping campaigns** — each campaign is a batch of product page URLs to fetch. For each URL, a worker:

1. Acquires a proxy from the pool
2. Restores the session context for the job
3. Solves a Cloudflare Turnstile CAPTCHA if no clearance token is present
4. Executes the HTTP request
5. Classifies any error and applies the appropriate remediation
6. Parses the response for price and availability data
7. Marks the job complete or failed

Campaign results are aggregated and reported as a success rate.

---

## Repository layout

```
candidate/
├── AGENTS.md          Architecture & code standards (read this first)
├── config.py          Operational configuration
├── models.py          Domain types and entities
├── http_client.py     HTTP transport and error classification
├── proxy_pool.py      Proxy lifecycle and assignment
├── captcha_solver.py  CAPTCHA provider integration
├── session_manager.py Agent session state and restoration
├── scraper.py         HTML parsing and price extraction
├── worker.py          Campaign worker orchestration
└── seed_jobs.py       Seed data representing current system state
```

The two question files (`Q1-parse-and-validate.md` and `Q2-trace-the-token.md`) are in the repository root.

---

## Your task

Read the codebase. Then answer both questions in a file called `solution.md`.

There is no code to write. This is a reading and reasoning exercise. Each question requires reading the code carefully — the included `AGENTS.md` describes the system's intended architecture.

---

## Submission

1. Fork this repository
2. Add your answers in `solution.md`
3. Raise a pull request with your name, email address, and the role you are applying for in the PR description

**Estimated time for assessment**: **90 minutes**.

---

## Notes

- You may use any tools or IDE you prefer
- The seed data in `seed_jobs.py` represents the actual state of a running system — treat it as ground truth
