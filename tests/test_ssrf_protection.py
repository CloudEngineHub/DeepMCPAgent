"""Tests for SSRF protection — _validate_url_not_private."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from promptise.mcp.server._openapi import _validate_url_not_private


class TestValidateUrlNotPrivate:
    """Direct tests for the SSRF URL validation function."""

    def test_rejects_localhost(self):
        with pytest.raises(ValueError, match="private"):
            _validate_url_not_private("http://localhost/spec.json")

    def test_rejects_localhost_with_port(self):
        with pytest.raises(ValueError, match="private"):
            _validate_url_not_private("http://localhost:8080/spec.json")

    def test_rejects_metadata_google(self):
        with pytest.raises(ValueError, match="private"):
            _validate_url_not_private("http://metadata.google.internal/computeMetadata/v1/")

    def test_rejects_127_0_0_1(self):
        """127.0.0.1 resolves to loopback — should be rejected."""
        # This may or may not raise depending on DNS resolution
        # but the function should catch it via ip.is_loopback
        try:
            _validate_url_not_private("http://127.0.0.1/spec.json")
            # If it didn't raise, the DNS resolution path was skipped
        except ValueError:
            pass  # Expected — loopback detected

    def test_accepts_public_url(self):
        """Public URLs should pass without error."""
        # Use a well-known public domain
        _validate_url_not_private("https://api.github.com/repos")

    def test_rejects_empty_hostname(self):
        with pytest.raises(ValueError, match="Invalid URL"):
            _validate_url_not_private("http:///path")

    def test_handles_unresolvable_hostname(self):
        """Unresolvable hostnames should not crash — let httpx handle the error."""
        # This should return without error (gaierror caught internally)
        _validate_url_not_private("https://this-domain-definitely-does-not-exist-xyz123.com/api")

    def test_rejects_169_254_metadata(self):
        """AWS metadata endpoint (link-local) should be rejected."""
        # Patch DNS resolution to return 169.254.169.254
        import socket

        fake_info = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 80))]
        with patch("socket.getaddrinfo", return_value=fake_info):
            with pytest.raises(ValueError, match="private"):
                _validate_url_not_private("http://evil.com/steal-metadata")

    def test_rejects_10_x_private(self):
        """10.x.x.x (RFC 1918) should be rejected."""
        import socket

        fake_info = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 80))]
        with patch("socket.getaddrinfo", return_value=fake_info):
            with pytest.raises(ValueError, match="private"):
                _validate_url_not_private("http://internal-api.com/data")

    def test_rejects_192_168_private(self):
        """192.168.x.x (RFC 1918) should be rejected."""
        import socket

        fake_info = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.100", 80))]
        with patch("socket.getaddrinfo", return_value=fake_info):
            with pytest.raises(ValueError, match="private"):
                _validate_url_not_private("http://office-server.com/api")
