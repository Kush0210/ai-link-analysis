"""URL validation and SSRF protection utilities.

Prevents requests to loopback, private networks, link-local / cloud metadata
endpoints (e.g., 169.254.169.254), reserved IP ranges, and non-HTTP schemes.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse, urlunparse

ALLOWED_SCHEMES = {"http", "https"}

BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata.goog",
    "metadata",
}

# Carrier-Grade NAT (RFC 6598) is not always marked private across all Python versions.
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class URLValidationError(ValueError):
    """Raised when a URL is malformed, unsupported, or violates SSRF rules."""


def is_ip_public(ip_str: str) -> bool:
    """Return True only if the IP address is globally routable and public."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    # Unwrap IPv4-mapped IPv6 addresses (e.g., ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False

    if isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT_NETWORK:
        return False

    return True


def normalize_url(raw_url: str) -> str:
    """Validate basic URL syntax and normalize scheme/host.

    Does not perform DNS resolution; use `validate_public_url` for full SSRF checks.
    """
    if not raw_url or not isinstance(raw_url, str):
        raise URLValidationError("A non-empty URL string is required.")

    candidate = raw_url.strip()
    if len(candidate) > 2048:
        raise URLValidationError("URL exceeds maximum allowed length of 2048 characters.")

    if any(ch.isspace() for ch in candidate):
        raise URLValidationError("URL must not contain whitespace characters.")

    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise URLValidationError(
            "Invalid URL scheme. Only 'http://' and 'https://' URLs are supported."
        )

    if parsed.username or parsed.password:
        raise URLValidationError("URLs containing embedded credentials are not allowed.")

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise URLValidationError("URL must include a valid hostname.")

    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(".local") or hostname.endswith(".internal"):
        raise URLValidationError(
            f"Access to internal or local hostname '{hostname}' is blocked for security reasons."
        )

    # Validate port if provided
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLValidationError("URL contains an invalid port number.") from exc

    if port is not None and not (1 <= port <= 65535):
        raise URLValidationError("URL port must be between 1 and 65535.")

    # Check literal IP hostnames immediately
    try:
        ipaddress.ip_address(hostname)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip:
        if not is_ip_public(hostname):
            raise URLValidationError(
                f"Access to private, loopback, or reserved IP address '{hostname}' is blocked (SSRF protection)."
            )
    else:
        # Require at least one dot for public hostnames (reject single-label internal hosts like http://redis/)
        if "." not in hostname:
            raise URLValidationError(
                f"Hostname '{hostname}' must be a valid public domain name."
            )

    netloc = hostname
    if ":" in hostname:  # IPv6 literal
        netloc = f"[{hostname}]"
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"

    path = parsed.path or "/"
    normalized = urlunparse((scheme, netloc, path, "", parsed.query, ""))
    return normalized


def validate_public_url(raw_url: str, resolve_dns: bool = True) -> str:
    """Validate URL format and verify via DNS that all resolved IPs are public.

    Raises:
        URLValidationError: If the URL is invalid, unresolvable, or points to
            a private/internal network address.
    """
    normalized = normalize_url(raw_url)
    parsed = urlparse(normalized)
    hostname = (parsed.hostname or "").strip().lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if not resolve_dns:
        return normalized

    try:
        addr_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLValidationError(
            f"Could not resolve domain '{hostname}'. Please verify the website URL exists."
        ) from exc
    except OSError as exc:
        raise URLValidationError(f"DNS lookup failed for '{hostname}': {exc}") from exc

    if not addr_info:
        raise URLValidationError(f"No IP addresses resolved for domain '{hostname}'.")

    resolved_ips = {entry[4][0] for entry in addr_info if entry[4]}
    for ip_str in resolved_ips:
        if not is_ip_public(ip_str):
            raise URLValidationError(
                f"Domain '{hostname}' resolves to a restricted/private network address and is blocked (SSRF protection)."
            )

    return normalized


def canonical_cache_key(normalized_url: str) -> str:
    """Build a canonical cache key for a normalized URL."""
    parsed = urlparse(normalized_url)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}" + (f"?{parsed.query}" if parsed.query else "")
