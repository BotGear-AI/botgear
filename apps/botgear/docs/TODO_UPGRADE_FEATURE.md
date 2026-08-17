# TODO — Self-update / Upgrade

How a running botgear instance updates **itself** — the modular
services in its repo *and* the framework + builtins it's running — through
a transparent wizard that shows what can be upgraded and lets the operator
choose. Updating a live robot is genuinely hard; this doc is the
**maintained, extensible** plan of record. Keep it in sync as the code
lands (mirror `TODO_REPO_PLAN.md`'s "verified against the code" discipline).

Status legend: ✅ done · 🔜 next · ⬜ planned · ⚠️ risk/decision open

Status date: 2026-06-27 (design; nothing in this doc is built yet except
the primitives in §2).

Companion docs it **extends, never forks**: `TODO_REPO_PLAN.md`
(registry `type@version` + `install_phase` + multi-registry),
`TODO_INSTALL_WIZARD.md` / `TODO_UNINSTALL.md` (the install/uninstall
engine + `/service_request` progress), `TODO_CONFIG_SETS.md` ("file is
truth" + graceful restart), `TODO_WORKSPACES_AND_RESTART.md` (re-exec +
reconcile), `TODO_SERVICE_UI_BUNDLES.md` (per-version `ui.js`),
`../TODO_PUBLIC_REPO.md` (D6: the deployed runtime only **consumes**
Releases — it downloads/verifies/swaps/restarts and ships **no**
build/publish/deploy tooling).

---

## 1. Framing — three upgrade axes (do **not** conflate)

| # | Axis | What it replaces | Difficulty | Reboots? |
|---|---|---|---|---|
| **A** | **Service-type update** | one modular non-builtin type `<name>/<version>/` (package.yml + src + optional `ui/dist/ui.js` + per-type `.venv`) | easy — pure registry op | per-instance restart only |
| **B** | **Framework + builtins update** | the frozen bundle: the `botgear` binary + `_internal/` + read-only `repo/` + bundled `uv` + `version.json` | hard — atomic swap + re-exec + backup/rollback + data migration | whole runtime re-execs |
| **C** | Config-set switch | nothing (already exists, `graceful_restart`) | n/a | listed only so the wizard does **not** call it an "upgrade" |

**Critical boundary the wizard must teach the user:** the named
"builtins" (clock, echo, brain, chat, cli, cron, motor_control, python,
security, servo, servo_mixer, ui) are `language: builtin` `.py` that live
in `repo/` but **import the frozen `botgear.framework.service.Service`**
and are `exec`'d into the live process (`in_process.py:99-118`). They
have **no hot reload** → the framework and its in-process builtins are
**one atomic unit** (axis B), never individually selectable. Only
subprocess/pip services (axis A) update without a full reboot.

This feature is ~80% **assembly of existing primitives** (§2) + ~20% new
machinery (A/B layout, backup/rollback, migrations, version surfacing).

---

## 2. Current state — primitives that already exist (verified)

| Capability | State | Evidence |
|---|---|---|
| Registry load (ABSENT→LOADED): download, **sha256 verify**, traversal-guarded extract into writable `var/repo/<name>/<ver>/` | ✅ | `runtime/registry.py:180-215` (`fetch_merged_catalog`), `:259-330` (`_download`/`_extract_archive`), `repo.py:290-294` (`writable_repo_dir`) |
| Registry install (LOADED→INSTALLED): per-type `.venv` on a daemon thread, `/registry/install/{name}@{ver}/progress` bus stream, serialized under `_REGISTRY_INSTALL_LOCK` | ✅ | `api/registry_api.py:182`, `runtime/registry.py` |
| Install / uninstall wizard + `/service_request` progress channel + `InstallProgress.tsx` step UI | ✅ | `TODO_INSTALL_WIZARD.md`, `TODO_UNINSTALL.md` |
| Graceful restart + re-exec: `set_draining` → `save_all_service_config` (snapshot desired_state) → `drain_services` → `os.execv`; recorded launch command replayed | ✅ tested | `runtime/system.py` (`graceful_restart`, `record_start_command`), `api/system_api.py:106-117` |
| Boot reconcile: `boot_from_config_set` → `reconcile_running_proxies` revives exact prior running state | ✅ | `runtime/workspaces.py`, `event_handlers.py` |
| Config sets ("file is truth"): `runtime.yml` + `<proxy_id>.yml` + `active_set.txt` + `.migrated` marker; secrets as `Encrypted--` leaves via `security` singleton | ✅ | `runtime/config_sets.py:150-224`, `TODO_CONFIG_SETS.md` |
| Data isolation: `entry.py` points `DATA_DIR`, `REPO_DIR`, and cwd all into `var/`; the frozen bundle holds **no** user state | ✅ | `packaging/entry.py:55-93` |
| Version surfacing: `version.json` → `/v1/version`, OpenAPI, mDNS announce, `/runtime/info` federation payload | ✅ (binary side) | `build.sh:271`, `entry.py:63-68`, `server.py:50,523-525`, `event_handlers.py:328-336` |
| Per-version UI bundle serving: `/repo/<name>/<ver>/ui.js` (no-cache + ETag) | ✅ | `runtime/script_routes.py:106-127` |
| Public Releases as the runtime + registry source | ✅ | `../TODO_PUBLIC_REPO.md` Phase 3, `tools/publish_services.py` |

### Gaps this feature closes
- ⬜ No "what can be upgraded?" detector (installed-vs-latest, framework or service).
- ⬜ `min_runtime` is captured (`build_services.py:160`) but **enforced nowhere**.
- ⬜ No framework self-replace: nothing downloads a new bundle, swaps it, and re-execs into it.
- ⬜ `service_proxy` rows are pinned to `name@OLD_version`; load/install of a new version makes a **parallel** dir and leaves instances on the old one (`lifecycle.py:225-229`, `subprocess.py:32-44`). No "repoint + rolling restart".
- ⬜ No backup / rollback / data-migration machinery.
- ⬜ UI `RuntimeMeta.version` is declared but hardcoded `null` (`RuntimeConnection.ts:52,90`) — the UI can't even show the running version yet.
- ⬜ Versioning is `latest`/sha-based in CI; no monotonic, pollable, `v`-less release scheme (§7).

---

## 3. Target architecture

### 3.1 On-disk layout — A/B `releases/` + `current` symlink (D1, recommended)

Restructure the flat bundle into an atomic-swap layout (industry-standard
"releases + current", mapped onto the already-tested re-exec):

```
<install>/
  botgear                 # thin launcher (stable argv[0]) → follows current/
  current  ->  releases/0.1.<n>     # atomic symlink = THE commit point
  releases/
    0.1.<n>/  { botgear.bin, _internal/, repo/, uv, version.json }
    0.1.<m>/  ...                   # staged new release, off to the side
  var/                       # SHARED user state (databases, config_sets, security, repo, ...)
  backups/<fromver>-<ts>/    # pre-swap var/ snapshots
  .staging/                  # download + extract scratch
```

- **Commit = one `rename()`** over `current` (write `current.tmp` symlink,
  `rename`). Everything before it (download/verify/stage) is throwaway.
- `os.execv` resolves `argv[0]` at exec time, so re-execing through the
  stable `<install>/botgear` launcher picks up the repointed symlink →
  boots new code with the **existing** restart backbone unchanged.
- ⚠️ PyInstaller may canonicalize `sys.executable`/`argv[0]` to the
  realpath of `releases/<old>/` → re-exec would boot OLD code. Mitigation:
  before execv, rewrite `last_start_command.json` argv[0] to the launcher
  path (`system.py:107` honors an absolute existing argv[0]). Verify per
  PyInstaller version.
- `entry.py` must learn an install root (`BOTGEAR_HOME`) and resolve
  `var/` there (today it resolves `var/` next to the binary, which would
  put it *inside* `releases/<ver>/`).

### 3.2 New backend surface (Admin-gated, beside `system_api.py`)

| Endpoint | Purpose |
|---|---|
| `GET /v1/update/status` | running `/v1/version`, latest framework release, per-service installed-vs-latest diff, compat verdicts, disk pre-flight |
| `POST /v1/update/services` `{items:[{name,version}]}` | axis A — reuses registry load+install + `/service_request` progress |
| `POST /v1/update/framework` `{target_version}` | axis B — download→verify→stage→backup→journal→swap→`graceful_restart` |
| bus `/system/update/progress` | framework phases (download%/verify/backup/stage/swap/restart/verify), mirroring the registry install stream |

Plus a process-wide **update lock** (extend the `_REGISTRY_INSTALL_LOCK`
discipline) so an in-flight upgrade can't be retriggered.

### 3.3 New UI surface

An **Updates** panel (one wizard, two clearly-separated sections — §6).
Prereq: populate `RuntimeMeta.version` from `/v1/version` so the UI can
show the running version at all.

---

## 4. Axis A — service-type update (the easy axis)

Pure registry, reuses everything, mostly **no backend reboot**.

**Detect** (`GET /v1/update/status`, services portion): fetch the remote
merged catalog (`registry.py:180-215`); for each installed `service_meta`
row diff installed `name@version` vs the newest remote version per name —
run the existing PEP440 `latest_per_name` (`repo.py:415-440`) over the
**remote** catalog (today it only runs over local manifests). Annotate
each with `local_state` (`registry_api.py:149-171`) and **gate by
`min_runtime`** against `/v1/version` (the enforcement that's missing
today; tolerate `latest`/`0.0.0` sentinels).

**Apply** (per selected service — load+install the **new** version):
1. `POST /v1/registry/load {name, new_version}` → buffered sha256-verified
   download (`registry.py:259-274`; 409 on mismatch — deterministic builds
   via `build_services.py mtime=0` are load-bearing) → extract into
   `var/repo/<name>/<new_version>/`.
2. `POST /v1/registry/install {name, new_version}` → fresh per-type `.venv`,
   streamed progress, serialized under the install lock.
3. `reconcile_catalog` + `types_index.rebuild()` surface the new version
   live — **no backend restart**.

**The real gap — repoint running instances (D9):** load/install creates a
parallel `<name>/<new_version>/` dir but instances stay pinned to
`name@OLD_version`. The update primitive must (a) update each instance's
`service_meta_id` to `name@new_version`, then (b) **per-instance stop+start
(rolling)**. Subprocess/pip services run as a child from
`<version>/.venv/bin` (`subprocess.py:63-71`), so the restarted instance
loads new code with no backend reboot; config survives ("file is truth" +
`save_all_service_config`).

**Hot vs restart — the load-bearing branch:**
- subprocess / pip → per-instance stop+start only. ✅ axis A lives here.
- in-process builtin → no hot reload → updated **only** via axis B. The
  wizard must not offer builtins in this section.

**UI bundle swap is nearly free:** a new version is a new per-version URL
`/repo/<name>/<new_ver>/ui.js`; picked up on browser refresh. (The `ui:`
block lives in on-disk `package.yml` (`repo.py:237`), not catalog
metadata — so "what changes (UI?)" is only known after load+reconcile.)

**Per-service rollback/GC:** keep the previous version dir as the A/B
rollback target (re-loading the same version preserves its `.venv`,
`registry.py:445-458`); prune via `uninstall_type` (`registry.py:647-680`).

---

## 5. Axis B — framework + builtins update (the hard axis)

Atomic A/B swap of the frozen bundle on a live system. Reuses
`graceful_restart`/`os.execv`; adds staging, backup, journal, atomic
cutover. **Each phase is reversible until the symlink rename.**

0. **Pre-flight.** From the release feed (§8.2) pick the latest
   `MAJOR.MINOR.BUILD` > current with a `botgear-<ver>-<platform>.tar.gz`
   asset for **this** arch (PyInstaller can't cross-compile; platform tag
   `build.sh:104`). Check `min_data_version` ≤ reachable, free disk ≥
   bundle×2 + backup, no upgrade already running. Refuse on a dev sentinel
   (`latest`/`0.0.0`).
1. **Download** tarball + `.sha256` to `.staging/`; **verify sha256**
   (reuse the registry integrity gate). Partial file → re-download.
2. **Stage** — extract the frozen layer to `releases/<new>/`; verify its
   `version.json == target`. Running system untouched.
3. **Backup + quiesce.** `set_draining(True)` (`lifecycle.py:392-393`);
   `save_all_service_config()` while live state is real (`system.py:140-148`);
   **stop the periodic reconciler + ensure no in-flight TinyDB write**
   (the documented `service_proxy.json` "Unterminated string" truncation
   source); `drain_services()`. Then snapshot the critical `var/` subset
   (§6.... see §"Backup/restore") to `backups/<fromver>-<ts>/`.
4. **Journal.** Write `var/.upgrade_pending.json = {from,to,backup_path,
   started_at,confirmed:false}` — the crash-recovery + rollback marker.
5. **Commit (atomic).** Repoint `current` → `releases/<new>` via
   `current.tmp` + `rename`. The single mutating step.
6. **Re-exec.** Rewrite `last_start_command.json` argv[0] = the launcher
   path, then existing `graceful_restart()` → `os.execv`. The launcher
   follows `current/` → new binary. `entry.py` reseeds `var/version.json`
   from `releases/<new>/version.json` every boot → all version surfaces
   report `<new>`.
7. **`var/repo` refresh — the subtle hazard (D3).** New builtin `.py` ship
   in the read-only bundle `repo/`, but `var/repo` is **first-boot-seed
   only** (`entry.py:55-58`) and `effective_repo_roots` searches writable
   `var/repo` **first** (`entry.py:84-93`) → a stale `var/repo` builtin
   **shadows** new bundle code = OLD builtin running against NEW framework
   API = silent break. Fix: on a version.json change, refresh the
   **framework-owned** subset of `var/repo` from the new bundle `repo/`
   (overwrite framework-shipped type dirs, **preserve** user-installed
   types + their `.venv`s) via a "managed types" manifest. (This is the
   same hazard already seen with the raspi `ui.js` 404 — see
   `entry.py` `REPO_PATHS` fallback.)
8. **Post-boot verify (new code).** Read the journal, run forward
   migrations (§8.3), self-test (DB loads, config-sets parse with
   `key.bin`, core boots, `/v1/version == to`). On success: `confirmed:true`,
   delete journal, publish "complete".
9. **Rollback (two tiers, D2).**
   - boots but self-test fails → repoint `current`→`releases/<old>`,
     restore `var/` from the pre-migration backup, re-exec.
   - fails to start at all → the upgrade code is gone post-execv → needs a
     supervisor: a thin launcher watchdog (non-zero exit within a window +
     unconfirmed journal → auto-repoint+restore+retry) **or** systemd
     `Restart=always` + `ExecStartPost` health gate **or** documented
     manual recovery. Keep `releases/<old>` until the new build is
     confirmed healthy.

**Inherited constraints:** the backend **port must stay stable** so
surviving subprocesses reconnect (`workspaces.py:88-94`); keep the 0.5s
response-flush before execv (`system_api.py:115`); singletons re-exec into
new code automatically.

### Backup / restore surface

`var/` is the **entire** mutable surface (the bundle holds no user state).

**Critical (irreplaceable) — every backup MUST include:**
- `var/databases/` — TinyDB system-of-record (User incl. admin — there is
  no `admin_password.txt`, auth_bootstrap=first_user_claim; Config,
  ServiceProxy/Meta/Config/Request, Workspace, ApiClient, AuthSession).
- `var/config_sets/` — robot wiring (`runtime.yml` start_order +
  `<proxy_id>.yml` + `active_set.txt` + `.migrated`).
- ⚠️ `var/security/key.bin` (0600) — **single point of failure.** Decrypts
  every `Encrypted--` secret; losing/regenerating it bricks all stored
  credentials (Pydantic load failure, `config_sets.py:217-224`). MUST be
  backed up, MUST NOT be regenerated.
- `var/runtime_id` — federation identity (`identity.py:86-101`).
- The operator's `.env` — `BOTGEAR_JWT_SECRET` (sessions die if it
  changes). ⚠️ location is ambiguous (next to binary vs cwd=var) — preserve
  whichever exists and verify it's read post-upgrade.

**Carry but rebuildable:** `var/repo/` user-installed type **source**
(re-run the installer rather than copy machine-specific `.venv`s);
`var/ssl/` (regenerable). **Not user state:** `var/version.json`
(re-seeded every boot).

**Where:** `backups/` sibling of `releases/`/`var/`. `releases/<old>/`
**is** the code backup → a code rollback is just a symlink repoint. Keep
last N backups + last-good release; prune on disk-constrained boards (D10).
Test the restore path (a never-restored backup is not a backup).

---

## 6. The wizard — "Updates" panel

**Prereq (Phase 1):** populate `RuntimeMeta.version` (`RuntimeConnection.ts`)
from `/v1/version` or `/runtime/info` — without it the UI can't show what's
installed.

Opens on `GET /v1/update/status`; **two visually distinct sections**:

- **Section A — Services (modular).** Table: `Service | Installed | Latest |
  Status [Up to date / Update available / Pinned by min_runtime] | ☑`.
  Each selectable independently; the safe, no-reboot section. Per-row "what
  changes" (new ui.js? new deps?) after a dry-resolve.
- **Section B — Framework & Builtins.** A **single card** (not a list):
  `Current 0.1.<n> → Latest 0.1.<m>`, changelog, and a **compat badge**:
  - 🟢 *Build update — safe, in-place* when MAJOR.MINOR match;
  - 🟡 *Minor/major — data migration, requires confirm* when the prefix
    changed / `breaking`;
  - 🔴 *Incompatible* (no asset for this arch, or `min_data_version` too
    new) — disabled with the reason.
  Persistent note: **"Builtin services (brain, chat, servo, ui, …) update
  together with the framework and cannot be selected individually."** ←
  the teaching moment that answers "what can be upgraded?".

**Select → Plan → Confirm → Progress.** The plan summary shows: total
download size, sha256 verification, that a `var/` backup will be taken,
**ordering (services first, framework LAST** because it re-execs
everything), estimated downtime, and the rollback guarantee. Framework
selection requires explicit confirm; a MAJOR.MINOR jump requires a typed
confirmation. Services stream the existing `/service_request` /
`InstallProgress` UI; the framework streams `/system/update/progress`
through the same component. **Across the re-exec** the client behaves like
a config-set switch: WS drops, auto-reconnects, then polls `/v1/version` +
`started_at` until `version == target` with a fresh `started_at`.

**Settings sub-panel:** update channel + auto-update toggle (new
**generated** Config fields → add to `templates/apps/botgear.yml`,
regenerate; never edit `models/config.py`). Default manual; if auto-update
on, auto-apply BUILD-only bumps within a MAJOR.MINOR line, always gate
MAJOR.MINOR jumps (D4).

---

## 7. Versioning (Phase 0 — prerequisite) ✅ implemented 2026-06-27

**Status (2026-06-27):** ✅ applied — `.github/workflows/package_botgear.yml`
(top-level `env: VERSION_PREFIX: "0.1."`; `VERSION: "${{ env.VERSION_PREFIX }}${{ github.run_number }}"`
on the Build step; "Resolve version" collapsed to the same single source;
the `botgear-v*` tag trigger removed; a dispatch-gated `Publish Release`
that tags + titles with the **bare** version, no `v`); `build_arm64.sh`
`TAG="${VERSION}"` (v dropped); UI `RuntimeConnection` now populates
`meta.version` + `meta.started_at` from the retained `/runtime/info`.
⬜ **Remaining (D6/D7):** the action creates the Release in **this**
(cloudseeder) repo — publishing to the public `BotGear-AI/botgear`
needs a cross-repo `token:` + `repository:`; x86_64 (CI) and aarch64
(QEMU-local) still have separate `run_number`s (need a matrix or a shared
`VERSION`); the legacy public `v0.1.0` release is not yet retired.

**Requirement:** release version/tag = `MAJOR.MINOR.BUILD` with **no
leading `v`**; the `MAJOR.MINOR.` prefix is a **hardcoded Action variable**;
`BUILD` = the Action **build number** (`GITHUB_RUN_NUMBER`).

`/.github/workflows/package_botgear.yml` is **hand-written** (no
`#managed` header) → edit directly. Precedent already in-repo:
`.github/workflows/build.yml:52` `IMAGE_VERSION: 0.9.${{ github.run_number }}`.
The version **string** already has no `v`; only the **tag** scheme carries
it. `build.sh:89` honors the `VERSION` env **first**, so **no build.sh edit
is needed** — just set it in CI.

**(1) Hardcoded prefix** — add a top-level `env:` (after `permissions:`):
```yaml
env:
  VERSION_PREFIX: "0.1."   # hardcoded major.minor — bump by hand for a minor/major release
```
**(2) Inject VERSION** on the Build step:
```yaml
      - name: Build
        working-directory: apps/botgear
        env:
          VERSION: "${{ env.VERSION_PREFIX }}${{ github.run_number }}"
        run: ./packaging/build.sh
```
→ `botgear-0.1.<run>-linux-x86_64.tar.gz`, `version.json={"version":"0.1.<run>"}`.
**(3) Collapse the duplicate "Resolve version" step** (kills the
documented build.sh-vs-workflow drift + the push-to-main name/content
mismatch where the artifact is named `0.1.0-<sha>` but contains
`...-latest` with `version.json=latest`):
```yaml
      - name: Resolve version
        id: ver
        run: echo "version=${{ env.VERSION_PREFIX }}${{ github.run_number }}" >> "$GITHUB_OUTPUT"
```
**(4) Drop the `v` + auto-create a pollable Release.** Move the tag trigger
off `botgear-v*`; publish with a bare tag so the updater can poll a
deterministic, monotonically-increasing release:
```yaml
      - name: Publish Release
        if: <release trigger — see D7>
        uses: softprops/action-gh-release@v2
        with:
          tag_name: ${{ env.VERSION_PREFIX }}${{ github.run_number }}   # e.g. 0.1.42, no 'v'
          files: |
            apps/botgear/dist/*.tar.gz
            apps/botgear/dist/*.tar.gz.sha256
```
And `packaging/build_arm64.sh:78` `TAG="v${VERSION}"` → `TAG="${VERSION}"`.

**Downstream impact:** tag/release naming `0.1.<run>` (no `v`);
version.json / `/v1/version` / OpenAPI / mDNS / `/runtime/info` all become
`0.1.<run>`; the service registry (`services-latest`, per-service versions)
is unchanged but `min_runtime` is now comparable; the updater parses
`MAJOR.MINOR.BUILD` → integer tuple. Local builds keep `latest`/
`--versioned` (GITHUB_RUN_NUMBER unset) — intentional CI-vs-local split.

⚠️ **Coherence risks (must resolve before relying on it):**
- `GITHUB_RUN_NUMBER` is **per-workflow-file** and **resets if the workflow
  is renamed/recreated** → BUILD could go backwards → the updater sees a
  "downgrade". Never rename `package_botgear.yml` (or persist a
  monotonic floor + offset).
- x86_64 (CI) and aarch64 (`build_arm64.sh`, manual QEMU) are **separate
  runs with separate run_numbers** → the "same" release gets different
  version strings per platform and the two assets won't land on one
  `0.1.<run>` Release. Fix via a matrix workflow (shared run_number) or
  pass an explicit shared `VERSION` to the arm64 build (D6).

---

## 8. Compatibility & migration

**Two independent version dimensions — keep separate:**
1. **Build version** `MAJOR.MINOR.BUILD` — *not* strict semver: BUILD is an
   opaque monotonic build id. **MAJOR.MINOR = the curated compatibility
   line** (bump by editing `VERSION_PREFIX`); BUILD = opaque id within it.
   Compare as numeric tuple. Within a line → forward-compatible
   (auto-applyable); across lines → deliberate, possibly migrating, gated.
2. **`data_version`** — a separate integer stored in `var/.data_version`
   describing the on-disk TinyDB + config-set yml shape. **This**, not the
   build version, drives migrations.

**8.2 Registry advertises versions.** Publish a `runtime-catalog.yml`
asset alongside the runtime tarballs on Releases (uniform with the service
`catalog.yml` → reuse `fetch_catalog`/merge). Per entry: `version` (bare),
per-platform `archive`+`sha256`, `min_data_version`, `min_previous_version`,
`breaking: bool`, `notes`. Fallback: GH `releases/latest` + asset names.
(Publish-half exists; the client-side check is the gap.)

**8.3 Migrations.** A new `runtime/migrations.py` runs on post-upgrade boot
(§5 step 8), forward-only + idempotent + ordered, recording applied
migrations (safe to re-run after an interrupted upgrade). Additive Config
changes are free (generated model + Pydantic defaults — add fields to the
**template**). Config-sets get a per-set schema version extending the
`.migrated` marker; secrets round-trip through `security` (carry `key.bin`,
never blind re-encrypt). **Gating:** refuse/hard-warn when target
`min_data_version` > reachable, no platform asset, or a `breaking` jump
without typed confirm. **Service↔framework:** enforce service `min_runtime`
at install/update *and* re-validate installed services after a framework
upgrade. **Rollback compat:** forward migrations may be irreversible →
rollback restores the **pre-migration** `var/` backup (not a
down-migration); `min_previous_version` says how far back is safe.

---

## 9. Phased work

| Phase | Scope | Verify |
|---|---|---|
| **0 — Versioning + surfacing** ✅ (core) | §7 workflow change ✅; drop `v` in `build_arm64.sh` ✅; wire UI `meta.version` from `/runtime/info` ✅; local-vs-CI sentinel split preserved ✅. ⬜ remaining: public-repo publish (cross-repo token), x86/arm64 run_number coherence (D6), retire legacy `v0.1.0` (D7) | a CI build produces `0.1.<run>`; `/v1/version`, mDNS, `/runtime/info`, UI all show it |
| **1 — Detector (read-only)** 🔜 framework half ✅ | ✅ `GET /v1/update/check` (framework current-vs-latest GitHub release + per-platform asset/sha256 + `0.0.0`/`latest` sentinel handling, graceful on network error — `api/update_api.py` + pure `runtime/updater.py` + unit tests); ✅ `update_repo` config field; ✅ **Updates** page ("Check for updates", Admin-gated) + nav + CI publishes to the public repo (§7). ⬜ remaining: per-service installed-vs-latest diff (PEP440 over remote catalog), `min_runtime` enforcement, the fuller `/update/status` + two-section wizard | ✅ `/v1/update/check` returns current→latest + platform asset on a live check |
| **2 — Service update (axis A)** 🔜 | `POST /v1/update/services` reusing load+install + progress; repoint instances + rolling restart (subprocess: no reboot); per-service rollback dir; optional GC | update a pip service to a newer version with no backend reboot; config survives; rollback works |
| **3 — A/B layout migration** ⬜ | flat bundle → `releases/<ver>/` + `current` symlink + stable launcher; `BOTGEAR_HOME` in entry.py; argv[0]=launcher; re-exec-through-symlink proof | idempotent one-time migration; restart still works; argv[0] follows symlink |
| **4 — Framework self-update (happy path)** ✅ MVP verified (x86_64) | ✅ `POST /v1/update/framework` (`runtime/update_apply.py`): download (stdlib urllib — NOT httpx, which isn't collected into the frozen bundle) → sha256-verify → traversal-guarded extract to a sibling `botgear-<new>-<plat>/` → drain (`save_all_service_config`+`drain_services`) → back up `var/` to `rlx-backups/` → carry `var/` (users/config_sets/key.bin/runtime_id) → rewrite `last_start_command.json` argv[0] → reuse tested `system.restart()` execv; ✅ `/system/update/progress` bus channel; ✅ UI **Update now** (POST → progress stream → poll `/v1/version` across restart). ⬜ remaining (Phase 5): A/B symlink layout (this MVP uses a flat sibling dir), `var/repo` framework-subset refresh (D3), `.upgrade_pending` journal + auto-rollback, migrations. | ✅ DEMONSTRATED 2026-06-27: a frozen 0.1.1 self-updated to 0.1.2 in ~14s (download→swap→re-exec), `/v1/version` flipped, admin token + runtime_id carried, old install kept as rollback |
| **5 — Backup/rollback + migrations + gating** ⬜ | `runtime/migrations.py` (data_version); journal-driven rollback; supervisor/launcher watchdog (D2); restore self-test; `runtime-catalog.yml` (min_data_version/breaking/min_previous_version); MAJOR.MINOR gating + typed confirm; retention/pruning | inject a failing build → auto-rollback to last-good; migration runs once; disk pruning holds |
| **6 — Polish** ⬜ | auto-update channel + toggle (generated Config fields); cross-platform release coherence; changelog rendering; federation-aware staggered peer upgrades; update lock/concurrency | auto-apply a BUILD bump; gate a MINOR jump; peers upgrade without version-skew breakage |

**Ship order rationale:** Phase 2 (services) first — lowest risk, highest
immediate value, no new layout. Phases 3-5 are the framework machinery,
each safe to land incrementally behind the detector.

---

## 10. Risks (⚠️)

- **execv mid-write truncates TinyDB** (`service_proxy.json` "Unterminated
  string"). The 0.5s `call_later` only flushes HTTP — MUST stop the
  reconciler + quiesce DB writers before swap/execv.
- **Stale `var/repo` shadows new builtin code** (D3) — silent API break.
- **`key.bin` loss = dead secrets** — any backup that omits it bricks creds.
- **No self-rollback after a failed boot** (code is gone post-execv) —
  needs a supervisor (D2); botgear has no external supervisor by default.
- **PyInstaller argv[0] canonicalization** could boot OLD code — mitigated
  by the launcher-path rewrite; verify per version.
- **Irreversible forward migrations** — rollback relies on a complete,
  tested pre-migration backup.
- **Disk pressure on Pi SD** — A/B doubles the frozen footprint + backups;
  needs retention + a pre-flight free-space gate.
- **The one-time A/B layout migration itself** runs on every existing
  install before the safety machinery exists — must be idempotent +
  reversible.
- **Non-deterministic framework tarball** → every framework update fails
  the sha256 gate (depends on `build_services.py mtime=0` determinism).
- **D6 public-repo constraint** — the updater module must NOT import
  build/publish/deploy tooling into the public mirror.
- **Auto-update on a dev build** (`version.json='latest'/'0.0.0'`) would
  loop-upgrade — sentinel handling must hard-disable it.
- **Port must stay stable** across re-exec or surviving subprocesses
  orphan onto stale code.
- **`GITHUB_RUN_NUMBER` reset/per-platform divergence** (§7).

---

## 11. Open decisions

| ID | Decision | Recommendation |
|---|---|---|
| **D1** | On-disk layout | A/B `releases/`+`current` symlink+shared `var/`+launcher (true atomic + clean rollback) vs in-place swap+`.prev` |
| **D2** | Boot-failure rollback supervisor | thin launcher watchdog (headless) vs systemd `Restart=always`+health gate (prod) vs manual |
| **D3** | `var/repo` refresh on framework bump | re-seed framework-owned subset, preserve user types+venvs vs flip `effective_repo_roots` precedence vs accept stale-shadow risk |
| **D4** | Auto-update policy | manual default; auto-apply BUILD-only within a line, always gate MAJOR.MINOR |
| **D5** | Framework compat metadata transport | published `runtime-catalog.yml` (reuse fetch/merge) vs per-release `release.json` vs GH release body |
| **D6** | Cross-platform version coherence | single matrix workflow (shared run_number) vs explicit shared VERSION to arm64 build |
| **D7** | Tag/Release migration + trigger | start fresh at `0.1.<run>` vs migrate legacy `v0.1.0`; auto-create Release on every main push vs release-branch/dispatch (recommended) |
| **D8** | `data_version` + migration framework | `var/.data_version` + `runtime/migrations.py` (forward-only, idempotent, recorded); extend config-set `.migrated` |
| **D9** | Service-instance update policy | auto-repoint + rolling restart (recommended) vs leave pinned until user restarts; + old-version GC/retention |
| **D10** | Backup scope + retention | critical-subset (db/config_sets/key.bin/runtime_id/.env) vs full `var/` tar; N backups / N releases before prune |
| **D11** | Federation/multi-runtime upgrades | staggered vs simultaneous across `@<peer-id>` peers; gate vs surface version skew |
