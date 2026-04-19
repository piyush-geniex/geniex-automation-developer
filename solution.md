# Solution

## Q1 - Architecture: Parse Success and Content Validity

(a)
*What assumption does this code encode about the relationship between an HTTP 200 response and the content of that response body?*
Supposes the response is the actual targeted page data, while it can also be a JS challenge from Cloudflare, which also comes with a 200 OK response.
Specific about Cloudflare response: JS file.

(b)
*What does the method actually validate before setting success=True?*
1. HTML content above a minimum size set by `config.scraper.min_page_size_bytes`
2. Parsable by BeautifulSoup: Well-structured HTML parsable into a nested data structure of the HTML tags. A caution note: for Client-Side Rendered websites, the actual HTML content may not exist yet and require JS execution.

*What category of page would produce a structurally valid parse result with both price and available as None?*
The Cloudflare JS challenge, which passes the above mentioned checks, will return `None` for price and availability, due to missing selectors.
It will also pass the size check since `curl -v -o response.html "https://www.coinbase.com"` results in a 4,5K HTML file, compressed and obfuscated.


*Now read the docstring on PriceParser.parse(). It contains a statement about what is "expected to have been filtered upstream." What is it expecting, and why does that expectation fail?*
It expects the HTTP client to filter out non-product pages. This fails because the `ErrorClassifier` only looks at non-200 status codes, completely ignoring the 200 OK Cloudflare challenge pages.

(c)

*Given your analysis above: where in the pipeline would correct detection of a non-product page (challenge page, access-denied page) need to occur?*
- Detection needs to happen in the `ErrorClassifier` (inside `http_client.py`) by actually reading the response body of 200 OK requests to spot the Cloudflare challenge signatures.

*Why can't the fix be placed inside PriceParser without modifying worker.py as well?*
- Because `PriceParser` only returns a simple success/failure. If it fails, `worker.py` just assumes the HTML was broken and retries normally, without knowing it actually needs to trigger a `RESOLVE_CAPTCHA` action to get a new token.

## Q2 - Trace the Session
[Reference specific functions and files by name.]

### Step 1
*What cookies does it contain?*
- `cf_clearance`: Proves that a user has passed a JS challenge. It is required for Cloudflare [JS detections](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/javascript-detections/).
- `_cf_bm`: (Note: possibly a typo in the codebase; the standard Cloudflare bot management cookie is `__cf_bm` with a double underscore). This cookie expires after 30 minutes of continuous inactivity by the end user.
- `session_id`: An application-specific session cookie.

*What is the value of "cf_clearance"?*
It contains a timestamp and encrypted data (related to the IP address and browser fingerprint) proving the network passed the challenge. [Reference](https://developers.cloudflare.com/fundamentals/reference/policies-compliances/cloudflare-cookies/)

*Which proxy solved it?*
P-003 got the cf_cleareance cookie


### Step 2

Since the Campaign is starting fresh, it is evaluated to False. Then the worker solves the CAPTCHA and sets the `cf_clearance` cookie.

### Step 3

Since the request is sent through P-001 while the cookie was obtained by P-003, Cloudflare will most likely detect an IP mismatch anomaly.
[what HTTP status code does it use?] 403. It may also ban the IP `"error": "IP_BLOCKED"`.

### Step 4
*What does ErrorClassifier.classify() return for this response?*
Because the status is 200 OK, the code in `worker.py` entirely bypasses `ErrorClassifier`. It hands the challenge page directly to `PriceParser`, which returns `success=True` with no price, falsely marking the job as `COMPLETED`.

### Step 5




## Q3

(a)

Trace of execution:
1. It sees the JOB is PENDING, so it processes it using `_process_job`.
2. _process_job sets the status to IN_PROGRESS.
3. Starts the retry loop...
4. Checks solves consumed against maximum budget per job. It has not exceeded it yet.
5. Acquires a proxy from the pool. (If it fails in doing so, marks the job as failing with a NETWORK_ERROR).
6. Since this is a fresh job, it acquires cookies using `self._session_manager.get_session_cookies(job)`.
7. Since `cf_clearance` is not in cookies, it attempts to solve the Captcha, which fails with a 503.
8. The result of the CAPTCHA solution `solve_result` is None.

*How many times does captcha_solves_used increment?* Once.
*Read worker._solve_captcha() carefully — what does it return when `CaptchaProviderError` is raised?* It returns `None`.
*Read worker._process_job() — what path does execution take after _solve_captcha returns?* It returns `None` implicitly.
*What is J-001's final status?* `IN_PROGRESS` and remains as such.


(b)

*What status do J-001 through J-005 show?*
*Is there an automatic code path that would retry these jobs?* Trace through worker.py and campaign.py to support your answer.

*What action would the operator need to take to get this data collected?*
Manually update the database records to set the statuses back to `PENDING` and restart the campaign worker.

## Q4 - Trace the Token

### Step 1

1. *What ErrorType is assigned?* PROXY_BANNED
2. *What RemediationAction is returned?* ROTATE_PROXY
3. *What does worker.py do in response to this remediation action?*
It rotates the proxy by picking a new unused one from the pool. If the pool is exhausted, the job is marked as PROXY_BANNED.
Else, a new proxy is used and the job's `retry_count` is incremented.


### Step 2

*Is P-001 available for assignment? Why or why not?* Yes. It is free to be used after its session has expired.
*What proxy does rotate() return?* `P-001`


### Step 3


### Step 4


### Step 5


## Q5 - Evaluate the Fix


### Fix A


### Fix B
Invalidating the session does not resolve the root cause (IP mismatch), and even makes matters worse. Requesting a new token will lead to an IP mismatch again, and loop over until CAPTCHA credits are exhausted.

### The Correct Fix


*What is the actual root cause — in one sentence?*
The actual root cause is the IP mismatch between the proxy used to solve the Cloudflare challenge and the proxy used to access the target website. This mismatch causes Cloudflare to repeatedly issue new challenges, which are not properly detected by the ErrorClassifier.

*Which layer(s) need to change, and what specifically needs to change in each?*
Three layers need changing:
1. `worker.py`: Must pass the `proxy_url` down when calling the solver.
2. `captcha_solver.py`: Must switch from `AntiTurnstileTaskProxyLess` to `AntiTurnstileTask` and use the provided proxy IP.
3. `http_client.py`: The `ErrorClassifier` must be upgraded to inspect payload contents.

*What would the fixed ErrorClassifier look like at a conceptual level (you don't need to write code — describe the decision logic)?*
The ErrorClassifier should inspect the response body and detect Cloudflare challenges and classify such responses as `CAPTCHA_REQUIRED`, even if the HTTP status code is 200 OK.

*What would the fixed session restoration flow look like on a retry with a new proxy?*
The flow would look as follow:
1. When a proxy is rotated on retry, the worker acquires a new proxy and finds no clearance token.
2. The worker passes the new proxy's exact IP to the CAPTCHA solver.
3. The solver explicitly binds the new token to that new IP.
4. The worker then attaches the matching token and executes the HTTP request successfully, bypassing Cloudflare without triggering another anomaly.
