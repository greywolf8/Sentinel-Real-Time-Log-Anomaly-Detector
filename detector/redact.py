"""PHI redaction (docs/sentinel-plan.md section 12).

Scrub SSN-like patterns, dates of birth, emails, phones and member-ID formats from all
evidence and messages before any alert leaves the detector. This is a security control
designed with HIPAA and HITRUST-style controls in mind.
"""

from __future__ import annotations

import re
from typing import Pattern

# Section 12: PHI patterns to scrub. These are synthetic IDs in the demo, but the
# redaction must work as if they were real.

# SSN-like patterns: ###-##-#### or ######### (with or without dashes)
_SSN_PATTERN = re.compile(
    r"\b\d{3}-?\d{2}-?\d{4}\b",
    re.IGNORECASE,
)

# Dates of birth: various common formats (YYYY-MM-DD, MM/DD/YYYY, DD-Mon-YYYY, etc.)
_DOB_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # YYYY-MM-DD
    re.compile(r"\b\d{2}/\d{2}/\d{4}\b"),  # MM/DD/YYYY
    re.compile(r"\b\d{2}-\d{2}-\d{4}\b"),  # MM-DD-YYYY
    re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b"),  # DD-Mon-YYYY
    re.compile(r"\b\d{8}\b"),  # YYYYMMDD (less common but possible)
]

# Email addresses
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    re.IGNORECASE,
)

# Phone numbers: various formats
_PHONE_PATTERNS = [
    re.compile(r"\b\d{3}-\d{3}-\d{4}\b"),  # ###-###-####
    re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}"),  # (###) ###-####
    re.compile(r"\b\d{10}\b"),  # ##########
    re.compile(r"\b\+1-\d{3}-\d{3}-\d{4}\b"),  # +1-###-###-####
]

# Member ID formats from the demo: MBR-{n}, CLM-{n}, PRV-{n}, etc.
_MEMBER_ID_PATTERN = re.compile(
    r"\b(MBR|CLM|PRV|APP|EVT|ISA|BNK|REMIT|txn|claim|member|provider|app|file|report|user)-\d+\b",
    re.IGNORECASE,
)

# Additional patterns that might appear in synthetic data
_ADDITIONAL_PATTERNS = [
    re.compile(r"\bNPI-\d+\b", re.IGNORECASE),  # NPI registry IDs
    re.compile(r"\bfund=F-\d+\b", re.IGNORECASE),  # Fund account IDs
    re.compile(r"\bshard=\d+\b", re.IGNORECASE),  # Shard numbers (not PHI but consistent)
]

# Replacement text
_REPLACEMENT = "[REDACTED]"


class Redactor:
    """PHI redactor for alert payloads and evidence."""

    def __init__(self) -> None:
        self._ssn_pattern = _SSN_PATTERN
        self._dob_patterns = _DOB_PATTERNS
        self._email_pattern = _EMAIL_PATTERN
        self._phone_patterns = _PHONE_PATTERNS
        self._member_id_pattern = _MEMBER_ID_PATTERN
        self._additional_patterns = _ADDITIONAL_PATTERNS
        self._stats = {
            "ssn_count": 0,
            "dob_count": 0,
            "email_count": 0,
            "phone_count": 0,
            "member_id_count": 0,
            "additional_count": 0,
        }

    def _redact_with_pattern(
        self, text: str, pattern: Pattern[str], stat_key: str
    ) -> str:
        """Redact matches of a pattern and update stats."""
        matches = pattern.findall(text)
        if matches:
            self._stats[stat_key] += len(matches)
            return pattern.sub(_REPLACEMENT, text)
        return text

    def redact(self, text: str) -> str:
        """Redact all PHI patterns from a string."""
        if not text:
            return text

        result = text

        # SSN
        result = self._redact_with_pattern(result, self._ssn_pattern, "ssn_count")

        # DOB
        for pattern in self._dob_patterns:
            result = self._redact_with_pattern(result, pattern, "dob_count")

        # Email
        result = self._redact_with_pattern(result, self._email_pattern, "email_count")

        # Phone
        for pattern in self._phone_patterns:
            result = self._redact_with_pattern(result, pattern, "phone_count")

        # Member IDs
        result = self._redact_with_pattern(
            result, self._member_id_pattern, "member_id_count"
        )

        # Additional patterns
        for pattern in self._additional_patterns:
            result = self._redact_with_pattern(result, pattern, "additional_count")

        return result

    def redact_dict(self, data: dict[str, object]) -> dict[str, object]:
        """Redact all string values in a dictionary recursively."""
        result = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = self.redact(value)
            elif isinstance(value, dict):
                result[key] = self.redact_dict(value)
            elif isinstance(value, list):
                result[key] = self.redact_list(value)
            else:
                result[key] = value
        return result

    def redact_list(self, data: list[object]) -> list[object]:
        """Redact all string values in a list recursively."""
        result = []
        for item in data:
            if isinstance(item, str):
                result.append(self.redact(item))
            elif isinstance(item, dict):
                result.append(self.redact_dict(item))
            elif isinstance(item, list):
                result.append(self.redact_list(item))
            else:
                result.append(item)
        return result

    def redact_evidence_line(self, line: str) -> str:
        """Redact a single evidence line (log line)."""
        return self.redact(line)

    def stats(self) -> dict[str, int]:
        """Return redaction statistics since last reset."""
        return dict(self._stats)

    def reset_stats(self) -> None:
        """Reset redaction statistics."""
        for key in self._stats:
            self._stats[key] = 0

    def check_for_phi(self, text: str) -> bool:
        """Check if text contains any PHI patterns (for canary testing)."""
        if self._ssn_pattern.search(text):
            return True
        for pattern in self._dob_patterns:
            if pattern.search(text):
                return True
        if self._email_pattern.search(text):
            return True
        for pattern in self._phone_patterns:
            if pattern.search(text):
                return True
        if self._member_id_pattern.search(text):
            return True
        for pattern in self._additional_patterns:
            if pattern.search(text):
                return True
        return False


# Global redactor instance for the detector
_global_redactor: Redactor | None = None


def get_redactor() -> Redactor:
    """Get the global redactor instance."""
    global _global_redactor
    if _global_redactor is None:
        _global_redactor = Redactor()
    return _global_redactor


def reset_redactor() -> None:
    """Reset the global redactor instance (for testing)."""
    global _global_redactor
    _global_redactor = None


def redact_text(text: str) -> str:
    """Convenience function to redact text using the global redactor."""
    return get_redactor().redact(text)


def redact_payload(payload: dict[str, object]) -> dict[str, object]:
    """Convenience function to redact a payload using the global redactor."""
    return get_redactor().redact_dict(payload)
