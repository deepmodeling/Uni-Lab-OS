"""Edge control plane endpoint derivation rules."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def normalize_scheduler_address(scheduler_address: str) -> str:
    """Normalize a user-configured Scheduler HTTP(S) origin.

    Scheduler control routes are appended by the Edge client, so the stored
    value must be an origin rather than a route, credential-bearing URL, or
    query-specific endpoint.
    """

    value = str(scheduler_address or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Scheduler 地址必须是 HTTP(S) 服务地址")
    if (
        parsed.username
        or parsed.password
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Scheduler 地址只能包含协议、主机名和端口")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def resolve_scheduler_address(
    backend_address: str,
    scheduler_override: str | None = None,
) -> str:
    """Return an explicit Scheduler origin or derive one from Backend."""

    if scheduler_override is not None and scheduler_override.strip():
        return normalize_scheduler_address(scheduler_override)
    return derive_scheduler_address(backend_address)


def derive_scheduler_address(backend_address: str) -> str:
    """Derive the Scheduler Authority origin from a Backend API address.

    Local split deployments expose Backend API and Scheduler on adjacent ports,
    while ingress deployments without an explicit port route both contracts
    through the same origin.
    """

    value = str(backend_address or "")
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = (
        f"{hostname}:{parsed.port + 1}" if parsed.port is not None else parsed.netloc
    )
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))
