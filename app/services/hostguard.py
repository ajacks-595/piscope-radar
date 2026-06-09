"""DNS-rebinding guard: validate the request's Host header against LAN-shaped names.

Why: this box deliberately has no auth, so the browser same-origin policy is the
only thing separating the API from hostile web pages. DNS rebinding defeats it —
a public page at attacker.example resolves its OWN hostname to the Pi's private
IP after the first load, and from then on reads AND writes the API as a
same-origin target (it also defeats the WebSocket Origin==Host check, since the
rebound page's Origin host equals the Host header). The rebound request always
carries the attacker's public FQDN in Host, though, so rejecting non-LAN-shaped
Host values closes the vector outright.

Allowed by default (no configuration needed):
  * single-label names — `piaware`, `localhost`, mDNS-less hosts (a public FQDN
    always has a dot, so these are unreachable by rebinding)
  * names under .local / .lan / .home / .home.arpa / .internal
  * IP literals in loopback, link-local, RFC1918 private, and CGNAT/Tailscale
    (100.64.0.0/10) space
  * anything listed in the `allowed_hosts` setting (comma-separated, exact
    case-insensitive match, port ignored) — e.g. a Tailscale MagicDNS name

Set `host_guard_enabled` to false to disable entirely.
"""
from __future__ import annotations

import ipaddress

from . import settings as settings_store

_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _host_only(host_header: str) -> str:
    """Lower-cased Host header minus any port; tolerates bracketed IPv6."""
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        return h.partition("]")[0].lstrip("[")
    if h.count(":") == 1:        # hostname:port or v4:port; raw IPv6 has more colons
        return h.rsplit(":", 1)[0]
    return h


def host_allowed(host_header: str) -> bool:
    """True if this Host header is LAN-shaped (or explicitly allow-listed)."""
    h = _host_only(host_header)
    if not h:
        return False
    extra = {
        t.strip().lower().rstrip(".")
        for t in str(settings_store.get("allowed_hosts") or "").split(",")
        if t.strip()
    }
    h = h.rstrip(".")            # absolute-form FQDNs ("evil.example.") normalise
    if h in extra:
        return True
    if "." not in h:
        return True
    if h.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(
        ip.is_loopback or ip.is_private or ip.is_link_local or ip in _CGNAT
    )


def enabled() -> bool:
    return bool(settings_store.get("host_guard_enabled"))
