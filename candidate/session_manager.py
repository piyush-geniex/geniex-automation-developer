"""
Agent session state management for the price intelligence platform.

Maintains the complete identity context for each scraping job:
cookies (including Cloudflare clearance) and user-agent.
Provides session persistence and restoration across retries.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict

from .models import Job

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Single source of truth for all agent session state.

    Callers should use restore_session() to obtain cookies for a job.
    Direct manipulation of cookies outside this class violates its
    coherence guarantees.
    """

    def __init__(self) -> None:
        # job_id → cookie dict (persisted after each successful request)
        self._sessions: Dict[str, dict] = {}
        self._user_agents: Dict[str, str] = {}

    def restore_session(self, job: Job) -> dict:
        """
        Restore the full session context for a job being retried.

        Returns the persisted session cookies including cf_clearance.
        cf_clearance cookies are session-scoped, not IP-scoped — they
        encode the verified user-agent fingerprint and solve timestamp.
        This makes them safely portable across proxy rotations within
        the same session window as long as the user-agent is preserved.

        If no persisted session exists, returns an empty cookie jar.
        """
        cookies = self._sessions.get(job.id)
        if cookies:
            logger.debug("restoring session for job %s", job.id)
            return deepcopy(cookies)

        logger.debug("no persisted session for job %s — empty cookie jar", job.id)
        return {}

    def store_session(
        self,
        job: Job,
        cookies: dict,
        proxy_id: str,
        user_agent: str,
    ) -> None:
        """
        Persist the session state after a successful request.
        Called by the worker after each successful response for future restore_session calls.
        """
        self._sessions[job.id] = deepcopy(cookies)
        self._user_agents[job.id] = user_agent
        logger.debug(
            "session stored for job %s (proxy=%s) at %s",
            job.id, proxy_id, datetime.now(timezone.utc).isoformat(),
        )

    def invalidate_session(self, job_id: str) -> None:
        """Remove the persisted session snapshot for a job."""
        removed = self._sessions.pop(job_id, None)
        if removed:
            logger.debug("session invalidated for job %s", job_id)

    def get_user_agent(self, job: Job) -> str:
        return self._user_agents.get(
            job.id,
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36",
        )
