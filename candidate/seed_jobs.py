"""
Seed data representing the current state of the price intelligence platform.

This module defines the proxy pool and campaign history that the system had
at the time of this assessment snapshot (2025-02-06 09:15 UTC).

Use this as ground truth when answering the assessment questions.
"""

from __future__ import annotations

from .models import (
    Campaign,
    CampaignStatus,
    Job,
    JobResult,
    JobStatus,
    ParseResult,
    Proxy,
    ProxyStatus,
    ProxyType,
)

# ---------------------------------------------------------------------------
# Proxies
# ---------------------------------------------------------------------------

PROXIES = [
    Proxy(
        id="P-001",
        host="us-res-1.proxyvendor.net",
        port=10000,
        username="geniex_res",
        password="pass-p001",
        proxy_type=ProxyType.RESIDENTIAL,
        country="US",
        status=ProxyStatus.HEALTHY,
        # Sticky session previously assigned, but TTL expired 10 minutes ago.
        # The proxy is HEALTHY in the pool and will be reallocated to the next
        # campaign that calls acquire().
        sticky_until="2025-02-06T09:05:00+00:00",
        assigned_campaign_id="C-001",
        last_used_at="2025-02-06T09:04:50+00:00",
        ban_count=0,
        success_count=312,
    ),
    Proxy(
        id="P-002",
        host="us-res-2.proxyvendor.net",
        port=10001,
        username="geniex_res",
        password="pass-p002",
        proxy_type=ProxyType.RESIDENTIAL,
        country="US",
        status=ProxyStatus.HEALTHY,
        sticky_until="2025-02-06T09:25:00+00:00",
        assigned_campaign_id="C-001",
        last_used_at="2025-02-06T09:14:00+00:00",
        ban_count=0,
        success_count=287,
    ),
    Proxy(
        id="P-003",
        host="us-dc-1.proxyvendor.net",
        port=20000,
        username="geniex_dc",
        password="pass-p003",
        proxy_type=ProxyType.DATACENTER,
        country="US",
        # Status reflects last database write. During a recent run, this proxy
        # received a 403 SESSION_FINGERPRINT_MISMATCH response. The ErrorClassifier
        # classified it as PROXY_BANNED and rotate() was called, setting it to
        # COOLING_DOWN in memory. However, a process restart occurred before the
        # status was flushed to the database. The database still shows HEALTHY.
        status=ProxyStatus.HEALTHY,
        sticky_until=None,
        assigned_campaign_id=None,
        last_used_at="2025-02-06T08:47:00+00:00",
        ban_count=2,
        success_count=94,
    ),
    Proxy(
        id="P-004",
        host="uk-mob-1.proxyvendor.net",
        port=30000,
        username="geniex_mob",
        password="pass-p004",
        proxy_type=ProxyType.MOBILE,
        country="UK",
        # P-004 entered COOLING_DOWN after an account-sharing detection event
        # during a prior campaign. At the time, P-001's sticky session had expired
        # and both P-001 and P-004 were serving requests for the same campaign
        # concurrently, leading the target site to flag same-IP session conflicts.
        status=ProxyStatus.COOLING_DOWN,
        sticky_until=None,
        assigned_campaign_id=None,
        last_used_at="2025-02-06T08:51:00+00:00",
        ban_count=1,
        success_count=55,
    ),
]

# ---------------------------------------------------------------------------
# Campaign C-003: completed — 10/10 jobs, but 7 have null price data
# ---------------------------------------------------------------------------
#
# C-003 ran successfully in the sense that all 10 jobs reached COMPLETED.
# However, 7 of the 10 responses were Cloudflare JS challenge pages
# (HTTP 200, ~14KB of valid HTML, no product content). PriceParser.parse()
# returned success=True with price=None and available=None for each.
# The campaign reports a 100% success rate and a 30% price coverage rate.
# ---------------------------------------------------------------------------

def _make_c003_job(job_num: int, price: float | None, available: bool | None) -> Job:
    parse_result = ParseResult(
        success=True,
        price=price,
        available=available,
        currency="USD" if price else None,
        raw_html_size=14200 if price is None else 9800,
        parse_duration_ms=12.4 if price is None else 8.1,
    )
    job_result = JobResult(
        job_id=f"J-{str(job_num).zfill(3)}",
        status=JobStatus.COMPLETED,
        parse_result=parse_result,
        error_type=None,
        proxy_id="P-001",
        completed_at="2025-02-06T08:55:00+00:00",
    )
    return Job(
        id=f"J-{str(job_num).zfill(3)}",
        campaign_id="C-003",
        url=f"https://www.target-retailer.com/products/sku-{str(job_num).zfill(4)}",
        status=JobStatus.COMPLETED,
        retry_count=0,
        captcha_solves_used=1,
        assigned_proxy_id="P-001",
        result=job_result,
        created_at="2025-02-06T08:50:00+00:00",
        updated_at="2025-02-06T08:55:00+00:00",
    )


# Jobs 101-103: genuine product pages — have price and availability
# Jobs 104-110: Cloudflare challenge pages — parse succeeded, data is null
C003_JOBS = [
    _make_c003_job(101, price=49.99, available=True),
    _make_c003_job(102, price=129.00, available=True),
    _make_c003_job(103, price=24.95, available=False),
    _make_c003_job(104, price=None, available=None),   # challenge page
    _make_c003_job(105, price=None, available=None),   # challenge page
    _make_c003_job(106, price=None, available=None),   # challenge page
    _make_c003_job(107, price=None, available=None),   # challenge page
    _make_c003_job(108, price=None, available=None),   # challenge page
    _make_c003_job(109, price=None, available=None),   # challenge page
    _make_c003_job(110, price=None, available=None),   # challenge page
]

CAMPAIGN_C003 = Campaign(
    id="C-003",
    name="New product launch monitoring",
    status=CampaignStatus.COMPLETED,
    target_urls=[j.url for j in C003_JOBS],
    jobs=C003_JOBS,
    created_at="2025-02-06T08:50:00+00:00",
    completed_at="2025-02-06T08:55:00+00:00",
)

# ---------------------------------------------------------------------------
# Convenience exports
# ---------------------------------------------------------------------------

ALL_PROXIES = PROXIES
ALL_CAMPAIGNS = [CAMPAIGN_C003]
