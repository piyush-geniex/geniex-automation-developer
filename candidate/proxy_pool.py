"""
Proxy pool management for the price intelligence platform.

Handles proxy acquisition, stickiness enforcement, rotation, and
health tracking. Proxies are assigned to campaigns, not to individual
jobs — all jobs within a campaign share the same proxy.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .config import config
from .models import Proxy, ProxyStatus

logger = logging.getLogger(__name__)


class ProxyPool:
    """
    Manages a pool of proxies with stickiness, health tracking, and rotation.

    Proxy assignment is campaign-scoped: `acquire(campaign_id)` returns the
    proxy currently assigned to that campaign (if still within its stickiness
    window), or allocates a new one.

    Workers are stateless with respect to proxy identity — they call
    `acquire()` before each request cycle and do not cache the result.
    """

    def __init__(self, proxies: List[Proxy]) -> None:
        self._proxies: Dict[str, Proxy] = {p.id: p for p in proxies}
        # campaign_id → proxy_id assignment table
        self._assignments: Dict[str, str] = {}

    def acquire(self, campaign_id: str) -> Optional[Proxy]:
        """
        Return the proxy assigned to this campaign.

        If the campaign has an active sticky assignment, returns that proxy.
        Otherwise, allocates the next available healthy proxy and assigns it.
        """
        assigned_id = self._assignments.get(campaign_id)
        if assigned_id:
            proxy = self._proxies.get(assigned_id)
            if proxy and proxy.status == ProxyStatus.HEALTHY and proxy.is_sticky_active():
                return proxy
            # Sticky window expired or proxy is no longer healthy — reallocate
            logger.info(
                "proxy %s stickiness expired or unhealthy for campaign %s — reallocating",
                assigned_id, campaign_id,
            )

        return self._allocate(campaign_id)

    def _allocate(self, campaign_id: str) -> Optional[Proxy]:
        """Assign the next healthy, unassigned proxy to this campaign."""
        active_assignments = set(self._assignments.values())

        for proxy in self._proxies.values():
            if proxy.status != ProxyStatus.HEALTHY:
                continue
            # Skip proxies that have crossed the consecutive-failure threshold
            # to prevent assigning a repeatedly-failing proxy to a fresh campaign.
            if proxy.consecutive_failures >= config.proxy.max_consecutive_failures:
                logger.debug(
                    "skipping proxy %s — consecutive_failures=%d >= threshold=%d",
                    proxy.id, proxy.consecutive_failures,
                    config.proxy.max_consecutive_failures,
                )
                continue
            if proxy.id in active_assignments and proxy.assigned_campaign_id != campaign_id:
                # Proxy is actively sticky to a different campaign
                if proxy.is_sticky_active():
                    continue
            # Assign this proxy
            ttl_seconds = config.proxy.sticky_session_ttl
            expiry = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            proxy.sticky_until = expiry.isoformat()
            proxy.assigned_campaign_id = campaign_id
            self._assignments[campaign_id] = proxy.id
            logger.debug(
                "allocated proxy %s to campaign %s (sticky until %s)",
                proxy.id, campaign_id, proxy.sticky_until,
            )
            return proxy

        logger.warning("no healthy proxies available for campaign %s", campaign_id)
        return None

    def rotate(self, proxy_id: str, campaign_id: str) -> Optional[Proxy]:
        """
        Mark the given proxy as COOLING_DOWN and allocate a replacement.

        Called when a proxy receives a definitive failure (403, ban signal).
        The previous proxy enters a cooldown period before re-entering the pool.
        """
        proxy = self._proxies.get(proxy_id)
        if proxy:
            self._mark_cooling_down(proxy)

        # Clear the current assignment so _allocate picks a fresh proxy
        self._assignments.pop(campaign_id, None)
        return self._allocate(campaign_id)

    def _mark_cooling_down(self, proxy: Proxy) -> None:
        proxy.status = ProxyStatus.COOLING_DOWN
        proxy.ban_count += 1
        proxy.assigned_campaign_id = None
        proxy.sticky_until = None
        logger.info(
            "proxy %s → COOLING_DOWN (ban_count=%d)", proxy.id, proxy.ban_count
        )

    def record_success(self, proxy_id: str) -> None:
        proxy = self._proxies.get(proxy_id)
        if proxy:
            proxy.success_count += 1
            proxy.last_used_at = datetime.now(timezone.utc).isoformat()

    def get_pool_status(self) -> dict:
        counts: Dict[str, int] = {}
        for proxy in self._proxies.values():
            counts[proxy.status.value] = counts.get(proxy.status.value, 0) + 1
        return counts
