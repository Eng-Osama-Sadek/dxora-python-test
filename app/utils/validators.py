"""URL validation and SSRF protection (stdlib only).

Rules: only http(s), only ports 80/443, no embedded credentials, and the host must resolve
exclusively to PUBLIC IP addresses (no loopback, private, link-local, CGNAT, reserved ranges).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443}
DNS_TIMEOUT_SECONDS = 5


class InvalidURLError(ValueError):
    """The URL is malformed or its domain cannot be resolved."""


class UnsafeURLError(ValueError):
    """The URL points to a private, internal or otherwise non-public destination."""


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return None


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def normalize_url(raw: str) -> str:
    """Return a clean http(s) URL or raise InvalidURLError / UnsafeURLError (no network access)."""
    value = (raw or "").strip()
    if not value:
        raise InvalidURLError("URL must not be empty.")
    if len(value) > MAX_URL_LENGTH:
        raise InvalidURLError(f"URL must not exceed {MAX_URL_LENGTH} characters.")
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise InvalidURLError("URL must not contain spaces or control characters.")
    if "://" not in value:
        value = "https://" + value

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError("URL is not valid.") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidURLError("Only http:// and https:// URLs are supported.")
    if parts.username or parts.password or "@" in parts.netloc:
        raise InvalidURLError("URLs with embedded credentials are not allowed.")
    host = (parts.hostname or "").strip(".").lower()
    if not host:
        raise InvalidURLError("URL must contain a host name.")
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError("Only the default web ports (80 and 443) are allowed.")

    if _parse_ip(host) is None:
        if "." not in host:
            raise InvalidURLError(f"'{host}' is not a valid domain name.")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise InvalidURLError(f"'{host}' is not a valid domain name.") from exc

    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


async def resolve_host(host: str) -> list[str]:
    """Resolve a host name to its IP addresses (kept separate so tests can replace it)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=DNS_TIMEOUT_SECONDS
        )
    except (socket.gaierror, asyncio.TimeoutError, UnicodeError) as exc:
        raise InvalidURLError(f"Could not resolve domain '{host}'.") from exc
    return sorted({str(info[4][0]) for info in infos})


async def assert_public_host(host: str) -> None:
    """Raise UnsafeURLError unless every address behind `host` is public."""
    ip = _parse_ip(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise UnsafeURLError("URLs pointing to private or internal addresses are not allowed.")
        return

    addresses = await resolve_host(host)
    if not addresses:
        raise InvalidURLError(f"Could not resolve domain '{host}'.")
    for address in addresses:
        resolved = _parse_ip(address)
        if resolved is None or not is_public_ip(resolved):
            raise UnsafeURLError(
                f"'{host}' resolves to a private or internal address; this URL is not allowed."
            )


async def validate_url(raw: str) -> str:
    """Full validation (syntax + DNS + SSRF check). Returns the normalized URL."""
    url = normalize_url(raw)
    await assert_public_host(urlsplit(url).hostname or "")
    return url
