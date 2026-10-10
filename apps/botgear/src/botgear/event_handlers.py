# unmanaged
from fastapi import Request
from database.interface import DatabaseAdapter
from config import get_settings
from models.auth_oauth_provider_config import AuthOauthProviderConfig
from models.auth_oauth_callback_data import AuthOauthCallbackData
from models.auth_session import AuthSession
from models.database_tinydb_config import DatabaseTinydbConfig
from models.database_sqlite_config import DatabaseSqliteConfig
from database.factory import create_database_client, get_database_client
from botgear.models.config import Config as BotgearConfig
from botgear.models.user import User
from botgear.models.registration import Registration
from botgear.models.service_meta import ServiceMeta
from botgear.models.service_proxy import ServiceProxy
from botgear.models.service_config import ServiceConfig
from botgear.models.service_request import ServiceRequest
from botgear.models.workspace import Workspace
from botgear.models.api_client import ApiClient
from botgear.runtime.catalog import reconcile_catalog
from botgear.runtime.workspaces import (
    ensure_runtime_workspace,
    reconcile_running_proxies,
    restore_active_workspaces,
)
from botgear.runtime import discovery as _discovery
from botgear.runtime import bus_stats as _bus_stats
from botgear.runtime import reconciler as _reconciler
from botgear.runtime import identity as _identity
from botgear.runtime import peer_manager as _peer_manager
from botgear.runtime import mdns as _mdns
from botgear.runtime.bus import get_bus
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def _bridge_jwt_secret() -> None:
    """Bridge ``BOTGEAR_JWT_SECRET`` from .env to ``JWT_SECRET_KEY``
    in the environment if the latter isn't already set.

    The auth packages (``packages/auth``) all read ``JWT_SECRET_KEY``
    directly from ``os.environ``. The botgear convention in
    ``.env`` files is to name the same value ``BOTGEAR_JWT_SECRET``
    (alongside every other ``BOTGEAR_*`` knob). Without this
    bridge the .env value is dead code and the auth code silently
    falls back to ``"fallback_dev_key"`` — which works in isolation
    but breaks federation the moment one runtime DOES set
    ``JWT_SECRET_KEY`` and another doesn't (peers signing with
    different secrets → 403 at handshake).

    Direct ``JWT_SECRET_KEY`` env var takes precedence so power users
    can override per-run without editing .env.
    """
    import os
    if os.environ.get("JWT_SECRET_KEY"):
        return
    bridge = os.environ.get("BOTGEAR_JWT_SECRET")
    if bridge:
        os.environ["JWT_SECRET_KEY"] = bridge
        logger.info("auth: bridged BOTGEAR_JWT_SECRET → JWT_SECRET_KEY")


def _ensure_jwt_secret(data_dir: Optional[str]) -> None:
    """Give this install its own JWT signing secret when none is configured.

    Without one, ``packages/auth`` falls back to a short public default that
    every botgear install shares — so any botgear (or anyone who reads the
    source) can mint Admin tokens this runtime accepts, and PyJWT warns about
    the key length on every token. Generate 32 random bytes once, keep them
    owner-readable under ``<data_dir>/security/jwt_secret``, and reuse them on
    every start. An explicit ``JWT_SECRET_KEY`` / ``BOTGEAR_JWT_SECRET`` still
    wins — set the same value on runtimes that should trust each other's tokens.

    Packaged (frozen) builds only: source checkouts keep the shared default
    (and its warning) so an existing source-run fleet that federates over it
    keeps working until it sets a shared ``BOTGEAR_JWT_SECRET``. mDNS
    auto-connect between installs then needs that shared secret or per-peer
    pairing (``peer_manager``'s api_key path).
    """
    import os
    import secrets
    import sys
    from auth.jwt_util import is_weak_jwt_secret

    if not getattr(sys, "frozen", False) or not is_weak_jwt_secret():
        return
    path = Path(data_dir or "data") / "security" / "jwt_secret"
    secret = ""
    try:
        secret = path.read_text().strip()
    except OSError:
        pass
    if len(secret) < 64:
        secret = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secret + "\n")
        logger.info("auth: generated this install's JWT secret at %s", path)
    os.environ["JWT_SECRET_KEY"] = secret


def get_ssl_paths(settings) -> tuple:
    """Where the self-signed HTTPS cert lives (server.py's get_ssl_paths hook).

    ``ssl/`` under the working directory: ``apps/botgear/ssl/`` from source
    (where botgear_ui's dev server looks for it) and ``var/ssl/`` in a
    packaged build, beside the rest of the install's data — so it survives
    reboots (no new cert, and no new browser warning, after every restart),
    is carried across self-updates with ``var/``, and isn't shared with other
    apps on the host the way the shared default under the temp dir is.
    """
    import os
    ssl_dir = Path("ssl").resolve()
    # Owner-only directory: the key file itself is written with the default
    # umask (often group/world-readable), so the directory is what keeps it
    # private.
    ssl_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ssl_dir, 0o700)
    except OSError:
        pass
    return (ssl_dir / "cert.pem", ssl_dir / "key.pem")


def on_startup():
    # Install the runtime-log ring buffer first so it captures the rest of
    # startup (the Logs page reads it via GET /v1/logs).
    from botgear.runtime import log_bus
    log_bus.install()

    _install_sigterm_handler()

    # Serialize the exact launch command so POST /v1/system/restart can
    # re-exec it (incl. any CLI args) — see runtime/system.py.
    from botgear.runtime import system
    system.record_start_command()

    # Stamp this process as a botgear backend so a peer runtime's
    # reconciler recognizes it across a PyInstaller freeze (where the
    # 'botgear.main' argv marker is gone). Inert on source builds.
    from botgear.runtime import proc_compat
    proc_compat.mark_backend_process()

    # Bridge BEFORE any auth code (subprocess_auth, jwt_util) reads
    # the env var. settings hasn't loaded yet but load_dotenv ran in
    # main.py before us, so BOTGEAR_JWT_SECRET is already in env.
    _bridge_jwt_secret()


    settings: BotgearConfig = get_settings()
    _ensure_jwt_secret(getattr(settings, "data_dir", None))

    # Resolve + cache the runtime_id BEFORE anything that might call
    # ``get_runtime_id()`` with no args (in-process service base
    # classes, services_index publisher, etc.). If we wait until later
    # in startup, the first no-arg caller wins and the cache gets
    # populated from the persisted file — settings.runtime_id is
    # silently ignored.
    runtime_id = _identity.get_runtime_id(
        data_dir=getattr(settings, "data_dir", None),
        settings_runtime_id=getattr(settings, "runtime_id", None),
    )
    logger.info("runtime.identity: resolved id=%s (data_dir=%s)",
                runtime_id, getattr(settings, "data_dir", None))

    # Phase 1 ships with the lowdb / TinyDB backend by default. The PRD
    # treats "install light" as a core principle, so the boot path stays
    # Postgres-free until a user explicitly opts in.
    #
    # Database files live under ``<data_dir>/databases/`` — co-located
    # with the runtime_id so a per-instance data dir (sibling runtimes on
    # the same box use distinct BOTGEAR_DATA_DIR values) keeps every
    # working file isolated. (botgear onboards via first_user_claim, so
    # there is no admin_password.txt.)
    _databases_dir = str(Path(settings.data_dir or "data") / "databases")
    if settings.database_type == "sqlite":
        # TinyDB/lowdb rewrites a whole table's JSON file on every write —
        # a process killed mid-write leaves a truncated file and the whole
        # table unreadable (it happened on 2026-06-20).
        # SQLite's own journal/WAL makes a torn write impossible: a crash
        # mid-transaction leaves either the prior committed state or the
        # new one, never a corrupt file.
        create_database_client(DatabaseSqliteConfig(
            name="default",
            sqlite_path=str(Path(_databases_dir) / "botgear.db"),
        ))
    else:
        create_database_client(DatabaseTinydbConfig(
            name="default",
            data_dir=_databases_dir,
        ))
    db: DatabaseAdapter = get_database_client()
    if not db:
        logger.error("Database client not created.")
        raise Exception("Database client not created.")

    db.ensure_table(model=AuthSession)
    db.ensure_table(model=Registration)
    db.ensure_table(model=User)
    db.ensure_table(model=ServiceMeta)
    db.ensure_table(model=ServiceProxy)
    db.ensure_table(model=ServiceConfig)
    db.ensure_table(model=ServiceRequest)
    db.ensure_table(model=Workspace)
    db.ensure_table(model=ApiClient)
    # The config/default row is written below (and read/written throughout
    # registry.py, config_service.py, registry_api.py) without ever being
    # ensure_table'd elsewhere. TinyDB tolerates that (tables materialize
    # lazily on first access); sqlite does not — an un-created "config"
    # table would 500 the very first boot at the get_item() check below.
    db.ensure_table(model=BotgearConfig)

    # Register the X-API-Key validator so requests carrying an `X-API-Key`
    # header authenticate against the api_client table. packages/auth's
    # get_current_user_or_service consults this BEFORE the JWT path on every
    # require_role-gated route; without this registration the header just
    # 401s. set_shared_secret_validator writes a process-global, so the same
    # auth_deps instance the CRUD routers use picks it up immediately.
    from botgear.api.crud_router_factory import auth_deps as _auth_deps
    from botgear.services.api_client_service import validate_api_client_secret
    _auth_deps.set_shared_secret_validator(validate_api_client_secret)
    logger.info("auth: registered api_client X-API-Key validator")

    # Reconcile the service catalog against the on-disk repo. Adds new
    # packages, refreshes changed ones, removes rows whose package is
    # gone. Idempotent — safe to run on every boot.
    from botgear.runtime import registry as _registry
    from botgear.runtime.repo import writable_repo_dir
    # The writable root is where loads/installs land; downstream boot
    # (config-set provisioning) still threads it through for any writes.
    repo_dir = writable_repo_dir(settings)
    # Effective roots honour user-edited config/default.repo_paths over
    # env, so a UI-managed repo list takes effect on the next boot.
    roots = _registry.effective_repo_roots(settings, db)

    # Sideload (Phase 6): extract any archives an operator dropped in
    # <writable-root-parent>/repo-staging/ into the writable repo before
    # we reconcile, so air-gapped installs appear as LOADED types. No-op
    # when the dir is absent.
    try:
        staging = repo_dir.parent / "repo-staging"
        sl = _registry.sideload_dir(staging, repo_dir=repo_dir, db=db, reconcile_roots=roots)
        if sl.get("loaded") or sl.get("failed"):
            logger.info("sideload: %s", sl)
    except Exception:  # noqa: BLE001
        logger.warning("sideload scan failed (non-fatal)", exc_info=True)

    reconcile_catalog(db, roots)

    # Seed the singleton config/default row if missing. The UI's
    # ConfigPage GETs /v1/config/default on load; without a seeded row
    # it would 404 every time a fresh deployment boots.
    if not db.get_item("config", "default"):
        default_cfg = settings.model_dump()
        default_cfg["id"] = "default"
        default_cfg.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        db.insert_item("config", "default", default_cfg)
        logger.info("Seeded config/default from env settings")

    # Start the subprocess discovery listener BEFORE reconcile so that
    # subprocess services that survived our restart get a chance to
    # announce themselves with a 'hello' before we go spawning new ones.
    _discovery.start()

    # Runtime-wide HALT status tracker (/runtime/halt_state). Services react
    # to /runtime/halt directly; this only keeps the retained status the UI
    # banner + brain start-latch read. See runtime/halt.py.
    from botgear.runtime import halt as _halt
    _halt.start()

    # MCP gateway (/v1/mcp): external AI agents drive live services under one
    # tool policy + HALT. Its catalog watchers + MCP session manager run in
    # this (the backend's) event loop. See mcp_gateway/ + docs/AGENT_ROADMAP.md.
    try:
        from botgear import mcp_gateway as _mcp_gateway
        _mcp_gateway.start()
    except Exception:  # noqa: BLE001 — the robot must boot even if the gateway can't
        logger.exception("mcp_gateway: failed to start")

    # Throttled per-topic publish-rate digest on /bus/stats — feeds the
    # Composer's live-flow overlay without exposing per-message traffic.
    _bus_stats.start()

    # Materialize the singleton "runtime" workspace (kind='runtime', id='runtime').
    # Its members are computed at read-time from the registry, but the row
    # itself owns the persistent canvas layout + edges.
    ensure_runtime_workspace(db)

    # Sync the active config set on disk → service_proxy rows. On first
    # boot under the new system the migrator writes yml files from the
    # existing rows; on every boot after that, the yml files become
    # authoritative and any drift propagates row-ward.
    # See AGENTS.md's "Config sets" section for the contract.
    from botgear.runtime.boot import boot_from_config_set
    from botgear.runtime.config_sets import pin_booted_set
    data_dir_path = Path(settings.data_dir or "data")
    if not data_dir_path.is_absolute():
        data_dir_path = Path.cwd() / data_dir_path
    # Pin the active set for the life of this process BEFORE any provisioning
    # or runtime config-set I/O — a later UI "switch" rewrites the marker for
    # the next boot only and must not retarget the live process's writes.
    pin_booted_set()
    boot_report = boot_from_config_set(db, repo_dir, data_dir_path)
    logger.info("boot.config_set: %s", {
        k: v for k, v in boot_report.items() if k not in ("sync_actions", "registry")
    })

    # Bring back every service that was running when we shut down, IN REGISTRY
    # ORDER. Their service_proxy rows still say 'running' but the processes are
    # gone; this resets them to 'stopped' and fires start_service. We pass the
    # exact in-memory registry sync just iterated (no disk re-read).
    reconcile_running_proxies(db, boot_report.get("registry"))
    restore_active_workspaces(db)

    # Periodic reconciler — converges drift unconditionally. Without it
    # we depend on every lifecycle event reaching every consumer, which
    # has historically been brittle (UI tab caches, dropped frames mid-
    # restart, orphan subprocesses surviving a crash, etc.).
    _reconciler.start()

    # ─── Runtime identity (multi-runtime federation, step 1) ──────────
    # Resolve once and persist; this id is the future ``@<id>`` topic
    # suffix that lets peer runtimes address us. Two override knobs:
    #   * RLX_RUNTIME_ID env var (direct, highest-priority env knob)
    #   * BOTGEAR_RUNTIME_ID in .env → settings.runtime_id (standard
    #     pydantic-settings path; what most users will reach for since
    #     it's symmetrical with PORT, DATABASE_DIR, etc.)
    # See runtime/identity.py for the full priority order + format
    # rules.
    # runtime_id was resolved at the top of on_startup so in-process
    # services see the correct value during their boot; here we just
    # read the cached value.
    runtime_id = _identity.get_runtime_id()
    # Step 2a: hand the id to the bus so it can recognise self-suffixed
    # topics (``/foo@<this-runtime>`` → ``/foo`` local) and route
    # other-suffixed ones to the peer manager.
    get_bus().set_local_id(runtime_id)
    _publish_runtime_info(db, runtime_id, settings)

    # Step 2c: bind the FastAPI loop to the peer manager so cross-thread
    # bus.publish_sync callers (sync FastAPI workers) can still send
    # frames upstream to peers.
    try:
        loop = asyncio.get_running_loop()
        _peer_manager.bind_loop(loop)
    except RuntimeError:
        # on_startup is currently called from a sync context. The
        # bind happens on first call from an async context instead.
        pass

    # Step 2d: autoconnect to peers persisted in the active set's runtime.yml
    # (stone 4b: was the runtime row's service_config.peers). Each entry is a
    # WS URL; the manager idempotently dedupes so a manual
    # POST /v1/peers/connect of the same URL is a no-op. Connection happens
    # lazily — these calls return immediately; identification + bridging
    # proceeds in the background.
    from botgear.runtime.config_sets import read_runtime_config
    _data_dir = Path(settings.data_dir or "data")
    if not _data_dir.is_absolute():
        _data_dir = Path.cwd() / _data_dir
    runtime_cfg = read_runtime_config(_data_dir)
    # Each entry is either a bare URL string (Option A — shared
    # JWT_SECRET_KEY) or {"url", "secret"} (Option B — a per-peer
    # api_client credential minted on the REMOTE side for us; see
    # peer_service.mint_peer_credential / peer_manager.connect).
    peer_entries = runtime_cfg.get("peers") or []

    # stone 4b migration: shed any legacy service_config left on the runtime
    # row by older builds (peers/mdns now live in runtime.yml; runtime_id in
    # the data/runtime_id file + the retained /runtime/info topic). update_item
    # MERGES (can't delete a key), so replace the row.
    _rt = db.get_item("service_proxy", "runtime")
    if _rt and "service_config" in _rt:
        _rt.pop("service_config", None)
        db.delete_item("service_proxy", "runtime")
        db.insert_item("service_proxy", "runtime", _rt)
    for entry in peer_entries:
        if isinstance(entry, dict):
            url, secret = entry.get("url"), entry.get("secret")
        else:
            url, secret = entry, None
        if not url:
            continue
        try:
            _peer_manager.connect(url, secret=secret)
            logger.info(
                "peer_manager.boot: dialed persisted peer %s (auth=%s)",
                url, "api_key" if secret else "shared_secret",
            )
        except Exception:  # noqa: BLE001
            logger.exception("peer_manager.boot: failed to dial %s", url)

    # mDNS auto-discovery. Defaults: enabled + auto-connect to any
    # discovered peer. User can opt out by setting either flag false
    # in runtime.service_config.{mdns_enabled, mdns_auto_connect}.
    if runtime_cfg.get("mdns_enabled", True):
        try:
            port = int(settings.port) if hasattr(settings, "port") else 8998
            _mdns.start(
                runtime_id=runtime_id,
                port=port,
                version=_read_version(),
                auto_connect=bool(runtime_cfg.get("mdns_auto_connect", True)),
                use_ssl=bool(getattr(settings, "ssl_enabled", False)),
            )
        except Exception:  # noqa: BLE001
            logger.exception("mdns.start failed — discovery disabled this boot")

    # Start the runtime's own /runtime/runtime/state publisher — system
    # + process metrics every few seconds. The publisher is internally
    # idempotent so multiple on_startup invocations (HMR, test reuse)
    # don't stack tasks.
    try:
        from botgear.runtime import system_state
        system_state.start_publisher(proxy_id="runtime", interval_s=3.0)
    except Exception:  # noqa: BLE001
        logger.exception("runtime.system_state: failed to start publisher")

    # Start the runtime's own /runtime/runtime/power publisher — battery,
    # AC, and (Linux) suspend/resume detection. Same idempotent-start
    # convention as system_state above.
    try:
        from botgear.runtime import power_state
        power_state.start_publisher(proxy_id="runtime", interval_s=5.0)
    except Exception:  # noqa: BLE001
        logger.exception("runtime.power_state: failed to start publisher")

    # Start the runtime's services-index publisher — aggregates every
    # service's /<type>/<id>/meta retained topic into a single
    # /runtime/runtime/services digest so consumers have a single
    # entry point to enumerate everything running in this process.
    try:
        from botgear.runtime import services_index
        services_index.start_publisher()
    except Exception:  # noqa: BLE001
        logger.exception("runtime.services_index: failed to start publisher")

    # Read-only request/reply surface for the runtime itself —
    # /runtime/runtime/meta + /control (get_time, get_info, ...), so
    # brain workflows and other bus clients can ask the runtime things.
    try:
        from botgear.runtime import runtime_control
        runtime_control.start()
    except Exception:  # noqa: BLE001
        logger.exception("runtime.control: failed to start")

    # Start the runtime's types-index publisher — introspects every
    # registered service type's config_class, @service_method args,
    # and optional state/topic schemas, publishing JSON Schemas keyed
    # by type-name to /runtime/runtime/types/<type>. The schema home
    # for instances that reference a type by key in their meta payload.
    try:
        from botgear.runtime import types_index
        types_index.start_publisher()
    except Exception:  # noqa: BLE001
        logger.exception("runtime.types_index: failed to start publisher")

    logger.info("Application has started successfully.")

    from botgear import startup_banner
    startup_banner.schedule(settings, _read_version())


def _publish_runtime_info(db: DatabaseAdapter, runtime_id: str, settings: BotgearConfig) -> None:
    """Announce this runtime on the bus.

    Publishes a retained ``/runtime/info`` so a late peer subscriber
    catches the announcement on connect (the same way every service
    surfaces its retained ``/state``). The runtime_id is also persisted to
    the ``data/runtime_id`` file by the identity module; there is no DB row
    mirror (stone 4b).
    """
    import os
    from datetime import datetime, timezone

    info = {
        "id": runtime_id,
        "version": _read_version(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    try:
        get_bus().publish_sync("/runtime/info", info, retained=True)
        logger.info("runtime.identity: announced /runtime/info id=%s", runtime_id)
    except Exception:  # noqa: BLE001
        logger.exception("runtime.identity: failed to publish /runtime/info")
    # stone 4b: the runtime_id is served via the retained /runtime/info topic
    # (above) + the persisted data/runtime_id file — no DB row mirror.


def _read_version() -> str:
    """Pull the version string the same way main.py does, but here so
    event_handlers doesn't take a hard dep on main.py. Best-effort —
    missing/malformed version.json yields '0.0.0'."""
    import json
    try:
        with open("version.json") as f:
            return str(json.load(f).get("version") or "0.0.0")
    except (OSError, json.JSONDecodeError):
        return "0.0.0"


def _install_sigterm_handler() -> None:
    """Make SIGTERM run on_shutdown (drain services) before the process dies.

    uvicorn's Server.capture_signals swaps in its own handler while serving,
    then on exit RESTORES the handler that was there before and re-raises
    the signal. With the default SIGTERM disposition that kills the process
    on the spot — main.run()'s ``finally: on_shutdown()`` never runs and
    every subprocess service is orphaned. Installing this before uvicorn
    starts means the restored handler is this one: drain, then re-raise
    with the default disposition so the exit status is still SIGTERM's.
    Ctrl-C (SIGINT → KeyboardInterrupt) already unwinds through that
    ``finally`` and doesn't need this."""
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        return

    def _on_sigterm(signum, _frame):
        try:
            on_shutdown()
        finally:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    signal.signal(signal.SIGTERM, _on_sigterm)


def on_shutdown():
    logger.info("Application is shutting down.")
    # Stop every service before the process goes (SIGTERM / Ctrl-C land
    # here). Subprocess services run in their own session, so without this
    # they outlive the backend and the next boot adopts a mix of old and
    # new processes. Skipped when a graceful restart/shutdown already
    # drained (those paths never return here anyway — execv / os._exit).
    try:
        from botgear.runtime import system
        if not system.is_draining():
            system.drain_for_exit("backend exit", snapshot=False)   # keep hand-edited ymls
    except Exception:  # noqa: BLE001
        logger.exception("shutdown: draining services failed")
    # Stop the system_state / power_state publishers so they don't fight
    # uvicorn's task cancellation. on_shutdown normally runs with the main
    # loop still running (main.run()'s finally, or the SIGTERM handler) —
    # a nested run_until_complete raises there — so schedule the stop on
    # that loop; only drive a fresh loop when none is running.
    from botgear.runtime import power_state, system_state
    for name, stop in (("system_state", system_state.stop_publisher),
                       ("power_state", power_state.stop_publisher)):
        try:
            _run_coroutine_best_effort(stop())
        except Exception:  # noqa: BLE001
            logger.exception("runtime.%s: shutdown failed", name)


def _run_coroutine_best_effort(coro) -> None:
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        running.create_task(coro)
        return
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def on_new_registration(registration, request: Optional[Request] = None):
    logger.info("A new user has registered.")


def on_verify_registration(user: dict, registration: dict, db) -> dict:
    return user


def on_first_user(user: dict, db) -> None:
    """Per-app business logic for the first-user-claim flow.

    Called from /v1/auth/claim-first-user (api/first_user_routes.py) AFTER
    the request payload has been validated and the user dict assembled,
    but BEFORE the row is inserted into the user table. Mutate ``user``
    in place — typically to set roles.

    botgear grants the first claimer Admin. Other apps that opt into
    the same flow (via the ``auth_bootstrap: first_user_claim`` config
    field) override this with their own role model — e.g. an app
    would assign ``["tenant_admin"]`` and link a tenant row here.
    """
    user["roles"] = ["Admin"]
    logger.info("first user claimed: %s with roles=%s", user.get("email"), user["roles"])


def on_get_oauth_provider_config(request: Request) -> Optional[AuthOauthProviderConfig]:
    logger.info("Fetching OAuth provider configuration.")
    return None


def on_oauth_callback(
    callback_data: AuthOauthCallbackData,
    user_data: dict,
    request: Request,
):
    logger.info("Processing OAuth callback.")
    sanitized_account = {
        "id": callback_data.id,
        "email": callback_data.email,
        "user_id": callback_data.user_id,
    }
    return {"status": "success", "account": sanitized_account}


def on_websocket_connect(websocket):
    logger.info("New WebSocket connection established")


def on_websocket_message(data: str, websocket) -> Optional[str]:
    logger.info(f"WebSocket message received: {data[:100]}...")
    return f"Echo: {data}"


def on_websocket_disconnect(websocket):
    logger.info("WebSocket disconnected")


def on_get_auth_session_ttl_seconds(request: Request | None = None) -> int:
    """Refresh-token session lifetime in seconds.

    Default reads from settings.auth_session_ttl_seconds (config_base.py default: 86400 = 24h).
    Override this function per deployment to use a different value.
    """
    settings = get_settings()
    return settings.auth_session_ttl_seconds or 86400


def on_get_jwt_access_token_ttl_minutes(request: Request | None = None) -> int:
    """Access-token JWT lifetime in minutes.

    Default reads from settings.jwt_access_token_ttl_minutes (config_base default: 60).
    Set JWT_EXPIRATION_MINUTES=2 env var to test the refresh flow on short cycles.
    """
    settings = get_settings()
    return settings.jwt_access_token_ttl_minutes or 60


def on_get_auth_session_idle_timeout_seconds(request: Request | None = None) -> int:
    """Idle-timeout in seconds — sliding limit enforced at the next /v1/refresh-token call.

    Default reads from settings.auth_session_idle_timeout_seconds (config_base default: 1800 = 30m).
    Any value < 1 disables the check; only the absolute auth_session_ttl_seconds ceiling applies.
    """
    settings = get_settings()
    value = settings.auth_session_idle_timeout_seconds
    return value if value is not None else 1800


# ─── server.py extension hooks ───────────────────────────────────────


def register_admin_routes(app) -> None:
    """Called by the generated server.py at AppServer init to attach
    app-specific admin / debug / fixture endpoints. The template
    invokes this AFTER the generated CRUD routers + the .yml's
    api_extend block, so anything registered here can override generic
    routes when paths overlap (none do today).

    Botgear registers the admin DB inspector here:

      * GET /v1/admin/table-list          — list every backend table
      * GET /v1/admin/table/{table}       — fetch every row in a table

    The Workspaces → Tables page (route /admin/tables) consumes both.
    """
    from botgear.api.admin_db_api import router as admin_db_router
    app.include_router(admin_db_router, prefix="/v1", tags=["Admin DB"])

    # Recent-runtime-log buffer for the Logs page (GET /v1/logs).
    from botgear.api.logs_api import router as logs_router
    app.include_router(logs_router, prefix="/v1", tags=["Logs"])

    # System control — GET /v1/system/info, POST /v1/system/restart.
    from botgear.api.system_api import router as system_router
    app.include_router(system_router, prefix="/v1", tags=["System"])

    # Explicit per-instance config save (yml-aware) — POST
    # /v1/service-proxy/{id}/config. Counterpart to the generic CRUD PUT,
    # which only writes the (largely unused, for subprocess types) DB
    # row's service_config and never reaches the yml a subprocess
    # actually starts from.
    from botgear.api.service_proxy_config_api import router as proxy_config_router
    app.include_router(proxy_config_router, prefix="/v1", tags=["Service Proxy Config"])

    # MCP gateway endpoint — see on_startup for its background work.
    try:
        from botgear import mcp_gateway as _mcp_gateway
        _mcp_gateway.mount(app)
    except Exception:  # noqa: BLE001
        logger.exception("mcp_gateway: failed to mount /v1/mcp")


def resolve_ui_dir() -> Optional[str]:
    """Called by the generated server.py to locate the compiled SPA.
    Returning a path overrides the default ``apps/botgear/build``
    lookup; returning None falls through to that default.

    Botgear uses this so PyInstaller --onedir bundles can find the
    UI at ``<install>/_internal/ui`` (where the spec file stages it)
    while dev runs still pick up the unfrozen ``build/`` next to the
    source tree. See ``botgear.paths.ui_dir`` for the full
    frozen-vs-dev resolution.
    """
    from botgear.paths import ui_dir
    resolved = ui_dir()
    return str(resolved) if resolved is not None else None
