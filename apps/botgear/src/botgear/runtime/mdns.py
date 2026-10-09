# unmanaged
"""mDNS / Bonjour auto-discovery for multi-runtime federation.

Each botgear runtime can announce itself on the LAN as
``_botgear._tcp.local.`` with the runtime's id + WS port carried
in the service properties. Peers browse for the same service type
and auto-connect via ``peer_manager.connect(url)``.

Boot flow:

  1. ``start(runtime_id, port, ...)`` is called from
     ``event_handlers.on_startup``.
  2. We register the local service so other runtimes can find us.
  3. We start a ServiceBrowser that fires ``_on_change`` whenever a
     peer is added / removed / updated.
  4. For ADDED peers, if ``auto_connect`` is on, we extract the
     ``id`` + WS URL from the TXT record and hand them to
     ``peer_manager.connect``. Self-discovery is filtered out using
     our own runtime id.

Trusted-LAN assumption: anyone on the same multicast scope can see
our announcement and connect using a subprocess JWT signed with our
``JWT_SECRET_KEY`` (Option A auth). Same threat model as ROS 1 master
+ DDS — fine for a workshop, not for hostile networks.

Tested by mocking the zeroconf library at the boundary so we don't
need a real multicast environment.
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Callable, Dict, Optional

import ifaddr


logger = logging.getLogger(__name__)


_SERVICE_TYPE = "_botgear._tcp.local."

# Module-level handles so start/stop can wind everything down cleanly.
_zc = None                # zeroconf.Zeroconf
_browser = None           # zeroconf.ServiceBrowser
_registered_name: Optional[str] = None
_local_runtime_id: Optional[str] = None
_auto_connect: bool = True
# Whether THIS runtime serves its API over TLS (settings.ssl_enabled).
# Peers dial the scheme WE speak — every runtime in a federation is
# assumed to share the same SSL posture (same deployment convention),
# so this is our own flag, not something learned per-peer.
_use_ssl: bool = False
# The kwargs the last successful ``start()`` call was made with (minus
# the test-only factory injections). Lets ``rescan()`` rebuild with the
# same identity without the caller having to remember runtime_id/port.
_last_start_kwargs: Optional[Dict[str, Any]] = None



# Interface name prefixes for virtual/container bridges that show up
# on any dev box running Docker, Podman, libvirt, etc. These carry
# private addresses that are only reachable from the SAME host — never
# from a genuine LAN peer — so they're excluded for the same reason
# loopback is (see docstring below). Confirmed in the field: a runtime
# on a Docker host announced ``docker0``'s 172.17.0.1 alongside its
# real LAN IP, and a remote peer's mDNS resolver happened to cache the
# Docker address for ``<runtime-id>.local`` — the peer then dialed
# 172.17.0.1 from a completely different machine, where nothing is
# listening, and sat in a silent TCP-connect timeout loop forever.
# Matched case-insensitively against the START of the interface name,
# so this also catches Linux's numbered variants (docker1, veth3f2a1,
# br-89ab..., virbr0) without needing exact names.
_VIRTUAL_IFACE_PREFIXES = (
    "docker", "br-", "veth", "virbr", "vmnet", "vboxnet",
    "podman", "cni", "flannel", "tun", "tap",
)


def _all_lan_addresses() -> list[bytes]:
    """Pack every IPv4 address on every non-loopback, non-virtual
    interface into the byte form zeroconf wants. Returning ALL of them
    means a peer on any reachable subnet sees us.

    Loopback is excluded from the main scan and used only as the
    last-resort fallback (a genuinely offline/isolated box with no
    other interface) — NOT mixed in alongside real addresses. Zeroconf
    has no way to tell a remote peer "ignore this one, it's only for
    same-box discovery"; if 127.0.0.1 ends up in the list a remote
    querier can end up resolving our hostname straight back to
    ITSELF instead of to us, which is silent and easy to misdiagnose
    (the connecting side sees a resolvable hostname and a live socket,
    just talking to the wrong process). ``ifaddr.get_adapters()``
    enumerates the loopback adapter too, so this must be filtered
    explicitly — the previous version's docstring claimed "every
    non-loopback interface" but never actually excluded it.

    Docker/Podman/libvirt bridge interfaces get the same treatment for
    the same reason, just with a different symptom: instead of us
    resolving to ourselves, a REMOTE peer resolves our SRV hostname to
    an address that only means something on our box, and hangs in a
    TCP-connect timeout against a private subnet it can't route to —
    see ``_VIRTUAL_IFACE_PREFIXES`` above.
    """
    addrs: list[bytes] = []
    for adapter in ifaddr.get_adapters():
        name = (adapter.name or "").lower()
        if name.startswith(_VIRTUAL_IFACE_PREFIXES):
            continue
        for ip in adapter.ips:
            if not ip.is_IPv4:
                continue
            if ip.ip.startswith("127."):
                continue
            try:
                addrs.append(socket.inet_aton(ip.ip))
            except OSError:
                continue
    return addrs or [socket.inet_aton("127.0.0.1")]


def _on_service_change(
    zeroconf=None, service_type=None, name=None, state_change=None,
    # Test code calls this positionally with (zc, service_type, name,
    # state_change). Real zeroconf uses kwargs. Support both by making
    # everything keyword-with-default; the first positional in tests
    # becomes ``zeroconf=...`` here.
    **_extra,
) -> None:
    """ServiceBrowser callback. Called when a peer's mDNS record is
    added / updated / removed. We only act on ADDED + UPDATED states
    by extracting the peer's WS URL + id and asking the peer manager
    to dial it.
    """
    zc = zeroconf
    from zeroconf import ServiceStateChange   # local import keeps top of file lighter

    if state_change not in (ServiceStateChange.Added, ServiceStateChange.Updated):
        return
    info = zc.get_service_info(service_type, name)
    if info is None:
        return

    # TXT properties are bytes → bytes. Decode permissively; an entry
    # we can't decode is a stranger's service that happens to share
    # our service type — skip.
    props: Dict[str, str] = {}
    for k, v in (info.properties or {}).items():
        try:
            props[k.decode("utf-8")] = v.decode("utf-8") if v else ""
        except (UnicodeDecodeError, AttributeError):
            continue

    peer_id = props.get("id")
    if not peer_id:
        logger.debug("mdns: %s has no id property — skipping", name)
        return

    # Self-discovery filter. We announce ourselves on this same channel,
    # and the browser sees our own record. Skip it.
    if peer_id == _local_runtime_id:
        return

    # Build the WS URL. Prefer the announcement's SRV hostname
    # (``<runtime-id>.local.``) over a raw IPv4 address — the hostname
    # is stable across docker bridge churn, distinguishes co-hosted
    # runtimes by id, and is what browser-side dialogs want to display
    # + persist. The host's mDNS resolver (avahi-daemon on Linux,
    # Bonjour on macOS) handles the lookup transparently for both the
    # server-side peer_manager.connect() and browser fetches.
    #
    # Falls back to the first IPv4 address if the announcement doesn't
    # carry a server hostname (shouldn't happen — we always announce
    # one — but be defensive).
    if info.port is None:
        return
    host = (info.server or "").rstrip(".")
    if not host:
        if not info.addresses:
            return
        host = socket.inet_ntoa(info.addresses[0])
    # Dial the scheme WE speak (settings.ssl_enabled) — every federation
    # peer is assumed to share the same SSL posture. A plaintext ws://
    # dial against a TLS-only port (or vice versa) doesn't fail cleanly:
    # the handshake bytes just don't parse as either protocol, surfacing
    # as an opaque ``InvalidMessage`` retry loop with no clue it was a
    # scheme mismatch.
    scheme = "wss" if _use_ssl else "ws"
    url = f"{scheme}://{host}:{info.port}/v1/ws"

    if not _auto_connect:
        logger.info("mdns: discovered peer %s at %s (auto_connect=False)", peer_id, url)
        return

    # ServiceBrowser fires this callback on its OWN thread. PeerConnection.start()
    # calls asyncio.create_task() which needs a running event loop on the
    # current thread — boom, RuntimeError. Hop onto the manager's bound loop
    # (set during event_handlers.on_startup) via call_soon_threadsafe so
    # the connect happens where the rest of peer_manager lives.
    try:
        from botgear.runtime import peer_manager
        loop = peer_manager._manager_loop
        if loop is None:
            # No loop bound yet — early-boot race. Just call connect
            # inline and hope a loop is up. If not, log it.
            try:
                peer_manager.connect(url)
                logger.info("mdns: auto-connecting to discovered peer %s at %s", peer_id, url)
            except RuntimeError:
                logger.warning(
                    "mdns: discovered peer %s at %s but no asyncio loop "
                    "available yet — skipping (will rediscover)", peer_id, url,
                )
            return
        loop.call_soon_threadsafe(_connect_on_loop, url, peer_id)
    except Exception:  # noqa: BLE001
        logger.exception("mdns: failed to schedule connect to peer %s at %s", peer_id, url)


def _connect_on_loop(url: str, peer_id: str) -> None:
    """Runs on the asyncio loop thread — safe to call asyncio APIs.
    Separated so the threadsafe hop has a plain function to schedule."""
    try:
        from botgear.runtime import peer_manager
        peer_manager.connect(url)
        logger.info("mdns: auto-connecting to discovered peer %s at %s", peer_id, url)
    except Exception:  # noqa: BLE001
        logger.exception("mdns: connect to peer %s at %s failed", peer_id, url)


def start(
    runtime_id: str,
    port: int,
    *,
    version: str = "0.0.0",
    auto_connect: bool = True,
    use_ssl: bool = False,
    zc_factory: Optional[Callable[[], object]] = None,
    browser_factory: Optional[Callable[[object, str, Callable], object]] = None,
) -> None:
    """Announce + browse. Idempotent — calling twice is a no-op.

    ``use_ssl`` should mirror ``settings.ssl_enabled`` — it decides
    whether discovered peers are dialed over ``wss://`` or ``ws://``
    (see the scheme comment in ``_on_service_change``).

    ``zc_factory`` and ``browser_factory`` are injection points for
    tests so we can drive the discovery callback without real
    multicast. Production uses the real zeroconf classes.
    """
    global _zc, _browser, _registered_name, _local_runtime_id, _auto_connect, _use_ssl, _last_start_kwargs
    # Recorded unconditionally (even on the idempotent no-op path below)
    # so ``rescan()`` always has the latest identity to restart with.
    _last_start_kwargs = dict(
        runtime_id=runtime_id, port=port, version=version,
        auto_connect=auto_connect, use_ssl=use_ssl,
    )
    if _zc is not None:
        return
    _local_runtime_id = runtime_id
    _auto_connect = auto_connect
    _use_ssl = use_ssl

    if zc_factory is None:
        from zeroconf import Zeroconf
        zc_factory = Zeroconf
    if browser_factory is None:
        from zeroconf import ServiceBrowser
        browser_factory = lambda zc, st, handler: ServiceBrowser(
            zc, st, handlers=[handler],
        )

    try:
        _zc = zc_factory()
    except Exception:  # noqa: BLE001
        logger.exception("mdns: zeroconf init failed — discovery disabled")
        return

    # Register the local service so other runtimes can find us.
    # zeroconf's sync ``register_service`` raises EventLoopBlocked when
    # invoked from inside an already-running asyncio loop (FastAPI's
    # lifespan in our case). Punt to a background thread so the boot
    # path stays unblocked. Registration is one-shot — fire-and-forget
    # is fine; failures log + the browser still works for inbound
    # discovery.
    try:
        from zeroconf import ServiceInfo
        instance_name = f"{runtime_id}.{_SERVICE_TYPE}"
        info = ServiceInfo(
            type_=_SERVICE_TYPE,
            name=instance_name,
            addresses=_all_lan_addresses(),
            port=int(port),
            properties={"id": runtime_id, "version": version},
            server=f"{runtime_id}.local.",
        )
        _registered_name = instance_name
        zc_handle = _zc
        def _register_in_thread() -> None:
            try:
                zc_handle.register_service(info)
                logger.info("mdns: announced %s on port %s", instance_name, port)
            except Exception:  # noqa: BLE001
                logger.exception("mdns: register_service failed (worker thread)")
        threading.Thread(
            target=_register_in_thread,
            name=f"mdns-register:{runtime_id}",
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001
        logger.exception("mdns: register_service setup failed")

    # Browse for peers. The browser fires on its own thread; our
    # callback hands work to the peer_manager which uses its bound
    # asyncio loop to dispatch.
    try:
        _browser = browser_factory(_zc, _SERVICE_TYPE, _on_service_change)
        logger.info("mdns: browsing for %s peers", _SERVICE_TYPE)
    except Exception:  # noqa: BLE001
        logger.exception("mdns: ServiceBrowser failed")


def stop() -> None:
    """Unregister + close the zeroconf instance. Safe to call when
    not started — idempotent."""
    global _zc, _browser, _registered_name, _local_runtime_id
    if _zc is None:
        return
    try:
        if _registered_name is not None:
            try:
                _zc.unregister_all_services()
            except Exception:  # noqa: BLE001
                logger.exception("mdns: unregister_all_services raised")
        _zc.close()
    finally:
        _zc = None
        _browser = None
        _registered_name = None
        _local_runtime_id = None


def is_started() -> bool:
    return _zc is not None


def rescan() -> bool:
    """Tear down + rebuild the zeroconf engine in place, without
    restarting the runtime.

    Why this exists: ``start()`` runs once at boot, and
    ``_all_lan_addresses()`` — plus zeroconf's own interface
    enumeration inside ``Zeroconf()`` — snapshot whatever interfaces
    exist *at that instant*. On a laptop, the backend routinely wins
    the race against DHCP: it's up and calling ``start()`` before Wi-Fi
    has an IP, so it announces itself on the ``127.0.0.1`` fallback and
    the zeroconf instance never joins the multicast group on the real
    interface — it stays deaf to LAN peers even after Wi-Fi comes up
    seconds later, because nothing re-evaluates interfaces after
    construction. A brand-new ``Zeroconf()`` re-enumerates from
    scratch, so tearing down + restarting just this module (not the
    whole process) is a full fix and is cheap enough to expose as a
    user-facing "Rescan network" action instead of "restart botgear".

    No-op (returns False) if ``start()`` was never called — nothing to
    rebuild. Returns True once the rebuild has been kicked off; peers
    reappear asynchronously as the browser re-discovers them (near
    instant for anything actively broadcasting on the LAN).
    """
    if _last_start_kwargs is None:
        return False
    kwargs = _last_start_kwargs
    stop()
    start(**kwargs)
    return True


def reset_for_tests() -> None:
    """Force-clear module state without touching network resources.
    Tests that injected fake factories use this to reset between cases."""
    global _zc, _browser, _registered_name, _local_runtime_id, _auto_connect, _last_start_kwargs
    _zc = None
    _browser = None
    _registered_name = None
    _local_runtime_id = None
    _auto_connect = True
    _last_start_kwargs = None
