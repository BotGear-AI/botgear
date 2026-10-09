"""Local interface/subnet enumeration.

Reimplements (does NOT import — a subprocess service can't reach into
the backend process) the same filtering ``botgear/runtime/mdns.py``'s
``_all_lan_addresses`` uses: real LAN interfaces only, never loopback,
never a Docker/Podman/libvirt/VPN virtual bridge. That filter exists
there because a peer's mDNS resolver can end up with an unreachable
container-only address for a hostname; here the failure mode would be
the same shape — "scan an address that only means something on this
box" — so the same interface-name exclusion list applies unchanged.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import List, Optional

import ifaddr

# See the module docstring — mirrors mdns.py's _VIRTUAL_IFACE_PREFIXES
# exactly. Matched case-insensitively against the START of the
# interface name, so this also catches numbered variants (docker1,
# veth3f2a1, br-89ab...).
_VIRTUAL_IFACE_PREFIXES = (
    "docker", "br-", "veth", "virbr", "vmnet", "vboxnet",
    "podman", "cni", "flannel", "tun", "tap",
)


def _read_iface_mac(name: str) -> Optional[str]:
    """This box's own MAC for interface ``name``, straight from sysfs.

    ``ip neigh show`` (the scan's normal signal source) structurally
    never contains this box's own address — the kernel doesn't ARP
    itself — so without this, the scanner's own host silently never
    appears in its own roster. Linux-only, matching this app's existing
    Linux-only posture for ``ip neigh``; returns None anywhere the path
    doesn't exist so callers degrade gracefully.
    """
    try:
        with open(f"/sys/class/net/{name}/address", encoding="ascii") as f:
            mac = f.read().strip().lower()
        return mac or None
    except OSError:
        return None


@dataclass
class Subnet:
    interface: str
    ip: str            # this box's own address on the interface
    netmask: str
    network: ipaddress.IPv4Network
    mac: Optional[str] = None   # this box's own MAC on the interface

    @property
    def cidr(self) -> str:
        return str(self.network)

    def host_count(self) -> int:
        # Exclude network + broadcast addresses (num_addresses already
        # does this for anything smaller than a /31/. /32).
        return max(0, self.network.num_addresses - 2)

    def hosts(self):
        return self.network.hosts()


def local_subnets() -> List[Subnet]:
    """Every real (non-loopback, non-virtual) IPv4 subnet this box has
    an address on. One entry per interface+address — a box with both
    Ethernet and Wi-Fi up gets two entries, which is exactly the
    "multiple subnets" case the scanner needs to sweep independently."""
    out: List[Subnet] = []
    for adapter in ifaddr.get_adapters():
        name = (adapter.name or "").lower()
        if name.startswith(_VIRTUAL_IFACE_PREFIXES):
            continue
        for ip in adapter.ips:
            if not ip.is_IPv4:
                continue
            addr = ip.ip
            if addr.startswith("127."):
                continue
            prefix = ip.network_prefix
            if not prefix:
                continue
            try:
                network = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
            except ValueError:
                continue
            out.append(Subnet(
                interface=adapter.nice_name or adapter.name,
                ip=addr,
                netmask=str(network.netmask),
                network=network,
                mac=_read_iface_mac(adapter.name),
            ))
    return out


def parse_subnet_override(value: str) -> Optional[ipaddress.IPv4Network]:
    """Parse a user-supplied subnet override (e.g. from config.subnets),
    e.g. '192.168.1.0/24'. Returns None (skip, don't crash the scan
    loop) for anything malformed rather than raising."""
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None
