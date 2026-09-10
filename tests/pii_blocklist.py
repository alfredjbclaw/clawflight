"""The single source of truth for what may not appear in this repository.

Both the pytest gate (``test_no_pii.py``) and the standalone scanner
(``pii_scan.py``) import from here. They used to carry separate pattern lists —
one in Python, one in shell — and the shell copy was quietly weaker, because
``git grep -E`` implements POSIX ERE and silently matches nothing for ``\\b``.
Several patterns had therefore never matched anything. One list, one regex
engine, no divergence.
"""
from __future__ import annotations

import re
from typing import List, NamedTuple, Tuple


#: The maintainer's own name and contact address.
#:
#: This gate exists to keep *third-party* personal data out — family members who
#: never agreed to appear in a public repository. The maintainer's own identity
#: is different in kind: it is published deliberately, so people have somewhere
#: to send a bug report.
#:
#: The allowance is exact. Any *other* address at the same provider is still a
#: finding.
MAINTAINER_CONTACTS: Tuple[str, ...] = (
    "Alfred J Berchtold",
    "alfred.j.berchtold@gmail.com",
)

#: Paths that necessarily contain the patterns they define.
SELF_REFERENTIAL = ("test_no_pii.py", "pii_blocklist.py", "pii_scan.py", "history_scan.sh")

#: Directories that are never source and never publish.
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".mypy_cache"}

#: Extensions never scanned as text.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".pyc"}


class Rule(NamedTuple):
    name: str
    pattern: "re.Pattern"
    why: str


def blocklist(strict: bool = False) -> List[Rule]:
    """Patterns that match the *shape* of real personal data.

    A fixture may say "SkyMiles"; it may never carry a membership number.

    With ``strict``, the maintainer's own name is blocked too — the setting for
    a fork that wants no personal identity at all.
    """
    rules = [
        Rule(
            "private-domains",
            re.compile(r"\b(?:ledecompany|livenation)\b|berchtold\.org", re.IGNORECASE),
            "domains belonging to real people or employers",
        ),
        Rule(
            "institutional-email",
            re.compile(
                r"@(?:cornell\.edu|gmail\.com|icloud\.com|outlook\.com|yahoo\.com)\b",
                re.IGNORECASE,
            ),
            "a real mailbox; fixtures must use example.com or a .example/.test domain",
        ),
        Rule(
            "loyalty-number",
            re.compile(
                r"\b(?:skymiles|aadvantage|mileageplus|frequent\s*flyer)\b[^\n]{0,24}?\d{6,}",
                re.IGNORECASE,
            ),
            "a loyalty programme membership number",
        ),
        Rule(
            "eticket-number",
            re.compile(
                r"\b(?:e-?ticket|ticket\s*(?:number|#))[^\n]{0,24}?\d{10,}", re.IGNORECASE
            ),
            "an e-ticket number",
        ),
        Rule(
            "payment-card",
            re.compile(
                r"\b(?:amex|american\s+express|visa|mastercard|card)\b[^\n]{0,24}?"
                r"(?:\*{2,}|x{4,}|ending\s+in\s*)\s*\d{4}\b",
                re.IGNORECASE,
            ),
            "a partial payment card number",
        ),
        Rule(
            "us-phone-number",
            re.compile(
                r"(?<![\d.\-])(?:\+1[ .-]?)?\(?[2-9]\d{2}\)?[ .-][2-9]\d{2}[ .-]\d{4}(?![\d.\-])"
            ),
            "a real-looking phone number; use +1555… only",
        ),
        Rule(
            "home-coordinates",
            re.compile(r"\bhome_(?:lat|lon|latitude|longitude|address)\b", re.IGNORECASE),
            "a home location field; drive-home routing is out of scope",
        ),
    ]
    if strict:
        rules.append(
            Rule(
                "maintainer-identity",
                re.compile(r"\bAlfred J Berchtold\b|alfred\.j\.berchtold@gmail\.com"),
                "the maintainer's own identity (strict mode: fully anonymous fork)",
            )
        )
    return rules


def without_maintainer(text: str, strict: bool = False) -> str:
    """Blank out the maintainer's own identity before pattern matching."""
    if strict:
        return text
    for contact in MAINTAINER_CONTACTS:
        text = text.replace(contact, "<maintainer>")
    return text


def is_self_referential(path: str) -> bool:
    return any(path.endswith(name) for name in SELF_REFERENTIAL)


def findings_in(text: str, label: str, strict: bool = False) -> List[str]:
    """Return one formatted finding per matching line of *text*."""
    hits = []
    rules = blocklist(strict)
    for line_number, line in enumerate(text.splitlines(), start=1):
        redacted = without_maintainer(line, strict)
        for rule in rules:
            if rule.pattern.search(redacted):
                hits.append(
                    "{}:{}: [{}] {}".format(
                        label, line_number, rule.name, line.strip()[:160]
                    )
                )
                break
    return hits
