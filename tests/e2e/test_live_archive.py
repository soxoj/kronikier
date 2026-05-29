"""End-to-end tests that hit the real web.archive.org.

These tests are marked ``e2e`` and are skipped by default. Run them with::

    pytest -m e2e

Whitelisted live domains
========================

Two real domains are explicitly whitelisted for live e2e fixtures —
``theranos.com`` and ``enron.com``. Both are textbook OSINT cases:
corporate collapse, live site gone, archive still holds the canonical
contact email (`info@…`) plus US-format office phones. The asymmetry
"live site returns nothing, archive returns contacts" is exactly what this
tool is for, which makes them clean smoke tests.

These are the *only* real domains allowed in committed code (see the
project rule on placeholder-only fixtures elsewhere).

If the wayback API is unreachable during a CI run, the test is skipped.
"""

from __future__ import annotations

import itertools
import socket

import pytest
import requests

from kronieker.cdx import query_domain
from kronieker.pipeline import scan_domain


def _network_available() -> bool:
    try:
        socket.create_connection(("web.archive.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _network_available(), reason="web.archive.org unreachable"),
]


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_cdx_smoke_returns_snapshots_for_known_domain():
    """The cheapest possible live check: CDX returns something for theranos.com."""
    rows = list(itertools.islice(query_domain("theranos.com", limit=10), 10))
    assert rows, "CDX returned zero rows for theranos.com — wayback unreachable?"
    assert any("theranos.com" in r.original for r in rows)


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_theranos_archive_yields_contact_info():
    """Theranos went dark in 2018; archives from 2014 still carry contacts.

    This is the canonical OSINT use case: the current site is defunct but
    the wayback machine holds the contact information from before the
    company collapsed.
    """
    result = scan_domain(
        "theranos.com",
        max_snapshots=15,
        from_year=2007,
        to_year=2016,
        probe_well_known=True,
        rate_limit_per_sec=2.0,
        default_phone_regions=("US",),
    )

    assert result.snapshots_considered > 100, (
        f"Expected many archived snapshots, got {result.snapshots_considered}"
    )
    assert result.snapshots_fetched > 0, "No snapshots successfully fetched"

    emails = {s.contact.value for s in result.sightings if s.contact.kind == "email"}
    phones = {s.contact.value for s in result.sightings if s.contact.kind == "phone"}

    # The canonical contact email from theranos.com's contact-us page
    assert "info@theranos.com" in emails, (
        f"Expected info@theranos.com in archived contacts; got emails={emails}"
    )
    # At least one US-format phone number must be recovered
    assert any(p.startswith("+1") for p in phones), (
        f"Expected at least one US-format phone; got phones={phones}"
    )


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_enron_archive_yields_contact_info():
    """Enron collapsed in late 2001; archived pre-collapse snapshots still
    carry the corporate switchboard and ``info@enron.com``.
    """
    result = scan_domain(
        "enron.com",
        max_snapshots=10,
        from_year=1998,
        to_year=2002,
        probe_well_known=False,  # CDX already gives us thousands of rows
        rate_limit_per_sec=2.0,
        default_phone_regions=("US",),
    )

    assert result.snapshots_considered > 100
    assert result.snapshots_fetched > 0

    emails = {s.contact.value for s in result.sightings if s.contact.kind == "email"}
    phones = {s.contact.value for s in result.sightings if s.contact.kind == "phone"}

    assert "info@enron.com" in emails, f"Expected info@enron.com; got {emails}"
    # Houston / 800-number switchboard
    assert any(p.startswith("+1") for p in phones), phones


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_current_theranos_has_no_contacts_but_archive_does():
    """Verify the asymmetry the tool is built around.

    A direct fetch of https://theranos.com today returns nothing usable
    (parked / gone). The wayback scan returns archived contacts. This is
    the asymmetry that justifies the tool's existence.
    """
    from kronieker.extractors import extract_contacts

    live_emails: set[str] = set()
    try:
        resp = requests.get("https://theranos.com/", timeout=10, allow_redirects=True)
        if resp.status_code == 200 and "text" in resp.headers.get("Content-Type", ""):
            live_emails = {
                c.value for c in extract_contacts(resp.text) if c.kind == "email"
            }
    except requests.RequestException:
        pass  # live site unreachable / parked — fine, that's the whole point

    # We don't make a hard assertion on the live site (it could change),
    # but we DO assert the archive yields contacts the live site does not.
    archived = scan_domain(
        "theranos.com",
        max_snapshots=10,
        from_year=2013,
        to_year=2015,
        probe_well_known=True,
        rate_limit_per_sec=2.0,
        default_phone_regions=("US",),
    )
    archived_emails = {
        s.contact.value for s in archived.sightings if s.contact.kind == "email"
    }
    assert archived_emails, "No archived emails found at all — test setup broken"
    # The interesting OSINT signal: contacts in archive that are NOT on live site.
    recovered = archived_emails - live_emails
    assert recovered, (
        "Archive returned the same emails as the live site — no OSINT value in this test. "
        f"live={live_emails}, archived={archived_emails}"
    )
