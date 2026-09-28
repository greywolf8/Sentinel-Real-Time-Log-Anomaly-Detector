"""Tests for PHI redaction (section 12)."""

import pytest

from detector.redact import Redactor, redact_payload, redact_text


@pytest.fixture
def redactor() -> Redactor:
    """Create a redactor for testing."""
    return Redactor()


def test_redactor_initialization(redactor: Redactor) -> None:
    """Test redactor initialization."""
    assert redactor.stats()["ssn_count"] == 0
    assert redactor.stats()["email_count"] == 0


def test_redact_ssn_with_dashes(redactor: Redactor) -> None:
    """Test redacting SSN with dashes."""
    text = "Member SSN: 123-45-6789"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "123-45-6789" not in result
    assert redactor.stats()["ssn_count"] == 1


def test_redact_ssn_without_dashes(redactor: Redactor) -> None:
    """Test redacting SSN without dashes."""
    text = "Member SSN: 123456789"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "123456789" not in result


def test_redact_dob_yyyy_mm_dd(redactor: Redactor) -> None:
    """Test redacting DOB in YYYY-MM-DD format."""
    text = "Date of birth: 1990-05-15"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "1990-05-15" not in result


def test_redact_dob_mm_dd_yyyy(redactor: Redactor) -> None:
    """Test redacting DOB in MM/DD/YYYY format."""
    text = "DOB: 05/15/1990"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "05/15/1990" not in result


def test_redact_email(redactor: Redactor) -> None:
    """Test redacting email addresses."""
    text = "Contact: john.doe@example.com"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "john.doe@example.com" not in result


def test_redact_phone_with_dashes(redactor: Redactor) -> None:
    """Test redacting phone numbers with dashes."""
    text = "Phone: 555-123-4567"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "555-123-4567" not in result


def test_redact_phone_with_parens(redactor: Redactor) -> None:
    """Test redacting phone numbers with parentheses."""
    text = "Phone: (555) 123-4567"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "(555) 123-4567" not in result


def test_redact_member_id(redactor: Redactor) -> None:
    """Test redacting member IDs."""
    text = "Member ID: MBR-12345"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "MBR-12345" not in result


def test_redact_claim_id(redactor: Redactor) -> None:
    """Test redacting claim IDs."""
    text = "Claim: CLM-88213"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "CLM-88213" not in result


def test_redact_npi_id(redactor: Redactor) -> None:
    """Test redacting NPI IDs."""
    text = "NPI: NPI-1234567890"
    result = redactor.redact(text)
    assert "[REDACTED]" in result
    assert "NPI-1234567890" not in result


def test_redact_multiple_patterns(redactor: Redactor) -> None:
    """Test redacting multiple PHI patterns in one string."""
    text = "Member MBR-12345, SSN 123-45-6789, email john@example.com"
    result = redactor.redact(text)
    assert result.count("[REDACTED]") >= 3
    assert "MBR-12345" not in result
    assert "123-45-6789" not in result
    assert "john@example.com" not in result


def test_redact_empty_string(redactor: Redactor) -> None:
    """Test redacting an empty string."""
    result = redactor.redact("")
    assert result == ""


def test_redact_no_phi(redactor: Redactor) -> None:
    """Test redacting a string with no PHI."""
    text = "System error: database timeout"
    result = redactor.redact(text)
    assert result == text
    assert redactor.stats()["ssn_count"] == 0


def test_redact_dict(redactor: Redactor) -> None:
    """Test redacting a dictionary."""
    data = {
        "message": "Member MBR-12345 has SSN 123-45-6789",
        "code": "E4410",
        "nested": {"email": "john@example.com"},
    }
    result = redactor.redact_dict(data)
    assert "[REDACTED]" in result["message"]
    assert "[REDACTED]" in result["nested"]["email"]
    assert result["code"] == "E4410"


def test_redact_list(redactor: Redactor) -> None:
    """Test redacting a list."""
    data = ["MBR-12345", "CLM-88213", "normal text"]
    result = redactor.redact_list(data)
    assert "[REDACTED]" in result[0]
    assert "[REDACTED]" in result[1]
    assert result[2] == "normal text"


def test_redact_stats(redactor: Redactor) -> None:
    """Test redaction statistics."""
    redactor.redact("SSN: 123-45-6789")
    redactor.redact("Email: test@example.com")
    stats = redactor.stats()
    assert stats["ssn_count"] == 1
    assert stats["email_count"] == 1


def test_redact_reset_stats(redactor: Redactor) -> None:
    """Test resetting redaction statistics."""
    redactor.redact("SSN: 123-45-6789")
    redactor.reset_stats()
    stats = redactor.stats()
    assert stats["ssn_count"] == 0


def test_check_for_phi_ssn(redactor: Redactor) -> None:
    """Test checking for SSN patterns."""
    assert redactor.check_for_phi("SSN: 123-45-6789") is True
    assert redactor.check_for_phi("No PHI here") is False


def test_check_for_phi_email(redactor: Redactor) -> None:
    """Test checking for email patterns."""
    assert redactor.check_for_phi("Email: test@example.com") is True


def test_check_for_phi_phone(redactor: Redactor) -> None:
    """Test checking for phone patterns."""
    assert redactor.check_for_phi("Phone: 555-123-4567") is True


def test_check_for_phi_member_id(redactor: Redactor) -> None:
    """Test checking for member ID patterns."""
    assert redactor.check_for_phi("Member: MBR-12345") is True


def test_redact_evidence_line(redactor: Redactor) -> None:
    """Test redacting an evidence line."""
    line = "1790603412345|I|ELG|MBR|00000|000014|a91f03c2|member lookup ok member=MBR-104233"
    result = redactor.redact_evidence_line(line)
    assert "[REDACTED]" in result
    assert "MBR-104233" not in result


def test_global_redactor() -> None:
    """Test global redactor functions."""
    from detector.redact import get_redactor, reset_redactor

    reset_redactor()
    redactor = get_redactor()
    result = redact_text("SSN: 123-45-6789")
    assert "[REDACTED]" in result


def test_redact_payload_global() -> None:
    """Test global payload redaction."""
    payload = {"message": "Member MBR-12345", "code": "E4410"}
    result = redact_payload(payload)
    assert "[REDACTED]" in result["message"]
    assert result["code"] == "E4410"


def test_canary_test_no_leakage(redactor: Redactor) -> None:
    """Canary test: ensure no PHI leaks after redaction."""
    text = "Member MBR-12345 SSN 123-45-6789 email john@example.com phone 555-123-4567"
    result = redactor.redact(text)
    # Check that no original PHI patterns remain
    assert "MBR-12345" not in result
    assert "123-45-6789" not in result
    assert "john@example.com" not in result
    assert "555-123-4567" not in result
    # Check that redaction occurred
    assert result.count("[REDACTED]") >= 4


def test_canary_test_detects_phi(redactor: Redactor) -> None:
    """Canary test: check_for_phi should detect PHI."""
    phi_text = "Member MBR-12345 with SSN 123-45-6789"
    assert redactor.check_for_phi(phi_text) is True

    clean_text = "System error in component CLM.STR"
    assert redactor.check_for_phi(clean_text) is False
