"""
CAPTCHA provider integration for the price intelligence platform.

Wraps the CapSolver Turnstile API. Worker code calls solve_turnstile()
to obtain a fresh cf_clearance-bound token for a given target URL.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

from .config import config

logger = logging.getLogger(__name__)


# CapSolver task types
# https://docs.capsolver.com/guide/captcha/Turnstile.html
_TASK_TYPE = "AntiTurnstileTaskProxyLess"

_API_BASE = "https://api.capsolver.com"


class CaptchaProviderError(Exception):
    """Raised on provider-side failures (auth, 5xx, malformed responses)."""


@dataclass
class SolveResult:
    token: str
    elapsed_seconds: float
    task_id: str


class CaptchaSolver:
    """
    Minimal CapSolver client for Cloudflare Turnstile challenges.

    Submits a Turnstile task to the provider and polls until the token is
    ready. Returns a SolveResult containing the cf_clearance token to inject
    into the request cookie jar.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or config.captcha.api_key
        self._poll_interval = config.captcha.poll_interval_seconds
        self._timeout = config.captcha.task_timeout_seconds

    def solve_turnstile(
        self,
        website_url: str,
        website_key: str,
        job_id: str = "",
    ) -> SolveResult:
        """
        Solve a Cloudflare Turnstile challenge for the given URL.

        Submits an AntiTurnstileTaskProxyLess task to CapSolver — the provider
        executes the challenge from its own infrastructure and returns the
        resulting token. Caller injects the token as `cf_clearance` in the
        request cookie jar.
        """
        start = time.monotonic()

        create_payload = {
            "clientKey": self._api_key,
            "task": {
                "type": _TASK_TYPE,
                "websiteURL": website_url,
                "websiteKey": website_key,
            },
        }

        resp = requests.post(f"{_API_BASE}/createTask", json=create_payload, timeout=15)
        if resp.status_code >= 500:
            raise CaptchaProviderError(f"{resp.status_code} {resp.reason}")
        body = resp.json()
        if body.get("errorId"):
            raise CaptchaProviderError(body.get("errorDescription", "createTask failed"))

        task_id = body["taskId"]
        logger.info("captcha task %s submitted for job %s", task_id, job_id)

        # Poll until ready or timeout
        while time.monotonic() - start < self._timeout:
            time.sleep(self._poll_interval)
            poll = requests.post(
                f"{_API_BASE}/getTaskResult",
                json={"clientKey": self._api_key, "taskId": task_id},
                timeout=15,
            ).json()
            if poll.get("status") == "ready":
                return SolveResult(
                    token=poll["solution"]["token"],
                    elapsed_seconds=time.monotonic() - start,
                    task_id=task_id,
                )
            if poll.get("errorId"):
                raise CaptchaProviderError(poll.get("errorDescription", "poll failed"))

        raise CaptchaProviderError(f"timeout after {self._timeout}s for task {task_id}")
