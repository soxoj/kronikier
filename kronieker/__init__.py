"""OSINT tool that mines historical email/phone contacts from web.archive.org."""

from kronieker.extractors import Contact, extract_contacts
from kronieker.pipeline import ScanResult, scan_domain

__all__ = ["Contact", "ScanResult", "extract_contacts", "scan_domain"]
__version__ = "0.1.0"
