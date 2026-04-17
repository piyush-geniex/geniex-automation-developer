"""
Campaign worker — orchestrates proxy acquisition, session restoration,
CAPTCHA solving, HTTP fetch, error classification, and result parsing
for a single campaign's job queue.

Workers are stateless with respect to proxy identity: they call
proxy_pool.acquire() before each request cycle and do not cache
proxy references between attempts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .captcha_solver import CaptchaProviderError, CaptchaSolver, SolveResult
from .config import config
from .http_client import ErrorClassifier, HttpClient, NetworkError, RequestConfig
from .models import (
    Campaign,
    CampaignStatus,
    ErrorType,
    Job,
    JobStatus,
    RemediationAction,
)
from .proxy_pool import ProxyPool
from .scraper import PriceParser
from .session_manager import SessionManager

logger = logging.getLogger(__name__)

# Cloudflare Turnstile configuration for the target retailer
_TARGET_SITE_KEY = "0x4AAAAAAA_cf5783b292e68e9"
_TARGET_BASE_URL = "https://www.target-retailer.com"


class Worker:
    """
    Processes all pending jobs in a campaign sequentially.

    For each job:
      1. Acquire proxy from pool (or reuse sticky assignment)
      2. Restore session context (cookies, user-agent)
      3. Solve CAPTCHA if no cf_clearance is present in the session
      4. Execute the HTTP request
      5. Classify any non-200 response
      6. Apply remediation (rotate proxy, backoff, skip, abort)
      7. Parse response body on 200
      8. Mark job COMPLETED (parse succeeded) or continue retrying
    """

    def __init__(
        self,
        campaign: Campaign,
        proxy_pool: ProxyPool,
        session_manager: SessionManager,
        captcha_solver: Optional[CaptchaSolver] = None,
        http_client: Optional[HttpClient] = None,
        parser: Optional[PriceParser] = None,
    ) -> None:
        self._campaign = campaign
        self._proxy_pool = proxy_pool
        self._session_manager = session_manager
        self._captcha_solver = captcha_solver or CaptchaSolver()
        self._http_client = http_client or HttpClient()
        self._parser = parser or PriceParser()
        self._classifier = ErrorClassifier()

    async def run(self) -> Campaign:
        """Process all PENDING and RETRYING jobs in the campaign."""
        logger.info(
            "worker starting campaign %s (%d jobs)",
            self._campaign.id, len(self._campaign.jobs),
        )

        for job in self._campaign.jobs:
            if job.status not in (JobStatus.PENDING, JobStatus.RETRYING):
                continue
            await self._process_job(job)
            await asyncio.sleep(config.worker.inter_request_delay)

        terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.EXHAUSTED}
        all_terminal = all(j.status in terminal for j in self._campaign.jobs)
        any_completed = any(j.status == JobStatus.COMPLETED for j in self._campaign.jobs)

        if all_terminal:
            self._campaign.status = (
                CampaignStatus.COMPLETED if any_completed else CampaignStatus.FAILED
            )

        return self._campaign

    async def _process_job(self, job: Job) -> None:
        job.status = JobStatus.IN_PROGRESS

        for attempt in range(config.worker.max_retries_per_job + 1):
            # Check budget before every attempt
            if job.captcha_solves_used >= config.captcha.retry_budget_per_job:
                logger.warning(
                    "job %s exhausted captcha budget (%d/%d)",
                    job.id,
                    job.captcha_solves_used,
                    config.captcha.retry_budget_per_job,
                )
                job.mark_exhausted()
                return

            proxy = self._proxy_pool.acquire(self._campaign.id)
            if proxy is None:
                logger.error(
                    "no proxies available for campaign %s (job %s attempt %d)",
                    self._campaign.id, job.id, attempt,
                )
                job.mark_failed(ErrorType.NETWORK_ERROR)
                return

            job.assigned_proxy_id = proxy.id

            # Session context: restore_session() preserves cf_clearance —
            # see session_manager.py and AGENTS.md for why cf_clearance is
            # safely portable across proxy rotations.
            cookies = self._session_manager.restore_session(job)

            # Solve CAPTCHA only when no clearance token is in the session
            if "cf_clearance" not in cookies:
                solve_result = await self._solve_captcha(job, proxy.id)
                if solve_result is None:
                    # Budget exhausted or provider error inside _solve_captcha
                    return
                cookies["cf_clearance"] = solve_result.token

            req = RequestConfig(
                url=job.url,
                proxy_url=(
                    f"http://{proxy.username}:{proxy.password}"
                    f"@{proxy.host}:{proxy.port}"
                ),
                cookies=cookies,
                user_agent=self._session_manager.get_user_agent(job),
                headers={},
                timeout=config.worker.request_timeout,
            )

            try:
                response = self._http_client.execute(req)
                response.proxy_id = proxy.id
            except NetworkError as exc:
                logger.warning("network error on job %s attempt %d: %s", job.id, attempt, exc)
                job.retry_count += 1
                continue

            # Non-200 — classify and apply remediation
            if response.status_code != 200:
                error_event = self._classifier.classify(response, job_id=job.id)
                job.error_log.append(error_event)
                logger.info(
                    "job %s attempt %d: %d → %s → %s",
                    job.id, attempt,
                    response.status_code,
                    error_event.error_type.value,
                    error_event.remediation.value,
                )

                if error_event.remediation == RemediationAction.ROTATE_PROXY:
                    new_proxy = self._proxy_pool.rotate(proxy.id, self._campaign.id)
                    if new_proxy is None:
                        logger.error(
                            "proxy pool exhausted for campaign %s (job %s)",
                            self._campaign.id, job.id,
                        )
                        job.mark_failed(ErrorType.PROXY_BANNED)
                        return
                    job.assigned_proxy_id = new_proxy.id
                    job.retry_count += 1
                    continue

                if error_event.remediation == RemediationAction.BACKOFF:
                    await asyncio.sleep(30)
                    job.retry_count += 1
                    continue

                if error_event.remediation == RemediationAction.SKIP:
                    job.mark_failed(ErrorType.NOT_FOUND)
                    return

                # ABORT or unhandled — terminal failure
                job.mark_failed(error_event.error_type)
                return

            # HTTP 200 — parse the response body
            parse_result = self._parser.parse(response.body, url=job.url)

            if not parse_result.success:
                # Structural parse failure (malformed HTML, page too small)
                logger.warning(
                    "structural parse failure for job %s attempt %d",
                    job.id, attempt,
                )
                job.retry_count += 1
                continue

            # Parse succeeded — store session state and mark complete
            self._session_manager.store_session(
                job,
                cookies,
                proxy.id,
                self._session_manager.get_user_agent(job),
            )
            self._proxy_pool.record_success(proxy.id)
            job.mark_completed(parse_result)
            logger.info(
                "job %s completed: price=%s available=%s",
                job.id, parse_result.price, parse_result.available,
            )
            return

        # Exhausted retry loop without completing or failing explicitly
        if job.status == JobStatus.IN_PROGRESS:
            job.mark_failed(ErrorType.UNKNOWN)

    async def _solve_captcha(
        self, job: Job, proxy_id: str
    ) -> Optional[SolveResult]:
        """
        Solve the Cloudflare Turnstile for this job's URL.

        Every solve attempt — success, provider error, or timeout —
        increments captcha_solves_used. This is the correct behavior
        per the unified budget model (see AGENTS.md).

        Returns None if the budget is exhausted, after marking the job EXHAUSTED.
        """
        if job.captcha_solves_used >= config.captcha.retry_budget_per_job:
            job.mark_exhausted()
            return None

        logger.info("solving captcha for job %s (proxy=%s)", job.id, proxy_id)

        # Increment before attempting — every attempt consumes budget
        job.captcha_solves_used += 1

        try:
            return await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._captcha_solver.solve_turnstile(
                    _TARGET_BASE_URL,
                    _TARGET_SITE_KEY,
                    job_id=job.id,
                ),
            )
        except CaptchaProviderError as exc:
            logger.warning(
                "captcha provider error for job %s: %s (solves_used=%d/%d)",
                job.id, exc, job.captcha_solves_used,
                config.captcha.retry_budget_per_job,
            )
            return None
