"""PII patterns match a whole token or nothing.

Regression tests for 1.5.0 misses: ``phone_us`` matched the last ten digits
of the order number ``2024-118-5521`` (redacted to ``2[PHONE]``), a spaced
IBAN was read as an Aadhaar / My Number and only partly redacted, and
several patterns stopped short of the end of a longer number, leaving its
tail in the redacted text.
"""

from __future__ import annotations

import pytest

from promptise.guardrails import PromptiseSecurityScanner, _iban_valid


async def _scan(text: str):
    scanner = PromptiseSecurityScanner(detect_injection=False, detect_toxicity=False)
    return await scanner.scan_text(text, direction="output")


def _redacted(report, text: str) -> str:
    return report.redacted_text if report.redacted_text is not None else text


# ── phone_us inside longer numbers ────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Order 2024-118-5521 shipped",
        "Ticket 12-345-678-9012 closed",
        "Ref 9.555.123.4567 noted",
        "Batch 555-123-4567-89 done",
    ],
)
async def test_phone_not_matched_inside_a_longer_number(text: str) -> None:
    report = await _scan(text)
    assert not [f for f in report.findings if f.category == "phone"], report.findings
    assert _redacted(report, text) == text


@pytest.mark.parametrize(
    "text, phone",
    [
        ("Call (555) 123-4567 today", "(555) 123-4567"),
        ("Call +1 555-123-4567 today", "+1 555-123-4567"),
        ("Call 1-555-123-4567 today", "1-555-123-4567"),
        ("Call 555.123.4567.", "555.123.4567"),
        ("phone:555-123-4567", "555-123-4567"),
    ],
)
async def test_us_phones_still_detected(text: str, phone: str) -> None:
    report = await _scan(text)
    assert phone in [f.matched_text for f in report.findings if f.category == "phone"]
    assert phone not in _redacted(report, text)


# ── IBAN printed in groups of four ────────────────────────────────────


@pytest.mark.parametrize(
    "iban",
    [
        "DE89 3704 0044 0532 0130 00",
        "GB82 WEST 1234 5698 7654 32",
        "FR14 2004 1010 0505 0001 3M02 606",
        "NL91 ABNA 0417 1643 00",
    ],
)
async def test_spaced_iban_is_redacted_whole(iban: str) -> None:
    text = f"Please pay to IBAN {iban} by Friday."
    report = await _scan(text)
    assert report.redacted_text == "Please pay to IBAN [IBAN] by Friday."
    # IBAN wins: its digits are not also reported as Aadhaar / My Number / card
    assert {f.category for f in report.findings} == {"iban"}
    assert [f.matched_text for f in report.findings] == [iban]


async def test_spaced_iban_followed_by_a_capitalised_word() -> None:
    text = "IBAN ES91 2100 0418 4502 0005 1332 THEN wire it"
    report = await _scan(text)
    assert report.redacted_text == "IBAN [IBAN] THEN wire it"


async def test_spaced_iban_with_bad_checksum_is_not_partly_redacted() -> None:
    text = "IBAN DE89 3704 0044 0532 0130 01 (typo)"
    report = await _scan(text)
    assert "iban" not in {f.category for f in report.findings}
    # no other pattern takes a twelve-digit slice out of the middle of it
    assert _redacted(report, text) == text


async def test_compact_iban_still_detected() -> None:
    report = await _scan("IBAN: DE89370400440532013000")
    assert report.redacted_text == "IBAN: [IBAN]"


def test_iban_checksum() -> None:
    assert _iban_valid("DE89370400440532013000")
    assert _iban_valid("GB82WEST12345698765432")
    assert not _iban_valid("DE89370400440532013001")
    assert not _iban_valid("DE89")


async def test_aadhaar_still_detected() -> None:
    report = await _scan("Aadhaar: 2345 6789 0123.")
    assert report.redacted_text == "Aadhaar: [AADHAAR]."


async def test_twelve_digits_of_a_longer_grouped_number_are_not_my_number() -> None:
    text = "Reference 1234 5678 9012 3456 attached"  # fails Luhn: not a card
    report = await _scan(text)
    assert _redacted(report, text) == text


# ── No partial redaction at the end of a longer number ────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Card exp 12/2025 ok", "Card [CARD_EXPIRY] ok"),
        ("Card exp 12/25 ok", "Card [CARD_EXPIRY] ok"),
        ("account 123456789012345678901234 ok", "[BANK_ACCOUNT] ok"),
        ("NPI 12345678901234 ok", "NPI 12345678901234 ok"),
        ("EIN: 12-34567890 ok", "EIN: 12-34567890 ok"),
        ("passport A1234567890 ok", "passport A1234567890 ok"),
        ("version 1.2.3.4.5 ok", "version 1.2.3.4.5 ok"),
        ("order 2024-078-05-1120 ok", "order 2024-078-05-1120 ok"),
        ("server 10.0.0.1. ok", "server [IP_ADDRESS]. ok"),
        ("SSN 078-05-1120 ok", "SSN [SSN] ok"),
    ],
)
async def test_whole_token_or_nothing(text: str, expected: str) -> None:
    report = await _scan(text)
    assert _redacted(report, text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Order 2024-118-5521, IBAN DE89 3704 0044 0532 0130 00, exp 12/2025",
        "acct 1234567890123456789012345 and NPI 123456789012 and DEA AB12345678",
        "MRN: 1234567890-ABCDEFGHIJ-1234567890-XYZ and BSN 1234567890",
        "medicare 2123 45670 12 and nhs 123 456 78901 and DL 123 456 7890",
        "dob 01/02/19901 born 31.12.20001 ref 12-345678-1234567",
    ],
)
async def test_no_finding_starts_or_ends_inside_a_digit_run(text: str) -> None:
    report = await _scan(text)
    for f in report.findings:
        assert f.start == 0 or not text[f.start - 1].isdigit(), f
        assert f.end == len(text) or not text[f.end].isdigit(), f
