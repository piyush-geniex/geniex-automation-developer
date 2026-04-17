# Q1 — Architecture: Parse Success and Content Validity

The `candidate/` directory implements a multi-layer pipeline:

```
HTTP fetch → error classification → HTML parse → mark job complete
```

Each layer is designed to have a single responsibility. Read `worker.py`, `http_client.py`, and `scraper.py` carefully before answering.

> *Hint: the docstring on `PriceParser.parse()` is load-bearing — read it twice.*

---

**(a)**

`worker.py` calls `parse_result = self._parser.parse(response.body, url=job.url)` and, when `parse_result.success` is `True`, proceeds directly to `job.mark_completed(parse_result)`.

What assumption does this code encode about the relationship between an HTTP 200 response and the content of that response body?

When is this assumption violated in the context of a Cloudflare-protected website? Be specific about what Cloudflare sends in those cases and what HTTP status code it uses.

---

**(b)**

Look at campaign **C-003** in `seed_jobs.py`. It shows:
- 10 of 10 jobs in status `COMPLETED`
- 7 of those 10 jobs have `parse_result.price = None` and `parse_result.available = None`

According to `candidate/AGENTS.md`, a `success=True` parse result with `price=None` is **valid business data** representing a currently unlisted SKU.

Read `scraper.py` carefully — specifically `PriceParser.parse()`. What does the method actually validate before setting `success=True`? What category of page would produce a structurally valid parse result with both `price` and `available` as `None`?

Now read the docstring on `PriceParser.parse()`. It contains a statement about what is "expected to have been filtered upstream." What is it expecting, and why does that expectation fail?

---

**(c)**

Given your analysis above: where in the pipeline would correct detection of a non-product page (challenge page, access-denied page) need to occur?

Why can't the fix be placed inside `PriceParser` without modifying `worker.py` as well?

---

**(d)**

In one paragraph (3-5 sentences), describe what a correct fix would look like. Where would the validation live, and **what specific signals in a Cloudflare challenge response would you inspect** to distinguish it from a real product page?
