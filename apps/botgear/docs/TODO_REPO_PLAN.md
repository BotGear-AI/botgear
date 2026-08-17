# Remote service repo — implementation plan

Companion to `docs/TODO_REPO.md` (the design). That doc lays out the
vision; this one records **where the code actually is today**, the
**decisions taken**, and the **ordered work** to close the gap.

Status date: 2026-06-13.

---

## 1. Current state (verified against the code)

| Area | State | Evidence |
|---|---|---|
| Backend lifecycle | **Done** — `fetch_catalog`, `find_in_catalog`, `load` (ABSENT→LOADED, sha256-verified, traversal-guarded extract, `.venv` preserved), `install` (LOADED→INSTALLED via `install_pip`), `uninstall` | `runtime/registry.py` |
| REST endpoints | **Done** — `GET /v1/registry/catalog` (+`local_state`), admin-gated `POST /registry/{load,install,uninstall}`; mounted via `botgear.yml` `api.extend` | `api/registry_api.py`, `server.py:537` |
| Config knob | **Done (single)** — `registry_url` default `file:///tmp/repo/catalog.yml` | `models/config.py:18` |
| Catalog reconcile | **Done (single dir)** — scans one `repo_dir`, derives `installed` via `is_installed` (builtin always installed; pip needs `.venv`) | `runtime/catalog.py:98`, `runtime/repo.py:is_installed` |
| Build/publish tooling | **Done** — `build_services.py` (deterministic tar + sha256 + metadata.json) + `publish_services.py` (`--kind file`/`http`/`gh_release`); both walk `repo/` keyed off each `package.yml`'s `publish: true` flag (no `registry.yml`); transport set by `--kind`/`--repo`/`--base-url`. Output matches `fetch_catalog` | `tools/build_services.py`, `tools/publish_services.py`, `tools/registry_common.py` |
| Dev mode | **Works as desired** — monorepo checkout loads all of `repo/`; builtins immediately runnable, pip types install lazily on first Start. No change wanted. | `is_installed` |

### Gaps

1. **UI is 100% unwired.** `Catalog.tsx` lists only locally-present
   `service_meta`; nothing calls `/v1/registry/*`. No browse view, no
   Load button, no ABSENT/LOADED/INSTALLED states, no Refresh, no
   load-vs-install failure split. Remote lifecycle is curl-only.
2. **Single repo / single registry.** One `repo_dir`, one
   `registry_url`, threaded through `catalog.reconcile_catalog`,
   `registry.*`, and the framework adapters. Can't reference a separate
   public-repo checkout or multiple remotes.
3. **No `install_phase`.** State is derived from `installed` +
   `installation_exception`; no persisted "installing" state, and
   load-failures (network/sha256) collapse into the same field as
   install-failures (pip/venv).
4. **Synchronous install.** `POST /registry/install` blocks on the
   venv build; `install_pip`'s `on_progress` hook is unused.
5. **Phase 5 (real hosting)** and **Phase 6 (sideload)** unimplemented;
   `publish_services.py` `gh_release`/`http` targets are stubs.

---

## 2. Decisions taken

- **D1 — Repo sources are ordered lists of local + remote.** Generalize
  both single knobs:
  - `repo_paths: list[str]` — ordered local roots scanned for LOADED
    types (dev checkout, bundled repo, a separate public
    `botgear-services` checkout, a private dir).
  - `registries: list[str]` — ordered remote catalog URLs searched for
    ABSENT types.
- **D2 — Dedicated writable root.** Loads/installs always land in one
  writable root; other local roots are read-only sources. The existing
  `repo_dir` *becomes* that writable root (dev: `./repo`; bundle:
  `var/repo`, already seeded by `packaging/entry.py`). `repo_paths` adds
  *extra read-only* roots on top.
- **D3 — Add `install_phase`.** `absent | loaded | installing |
  installed | failed`, plus split error fields. Keep `installed` for one
  release as a derived view, then deprecate.
- **D4 — UI wiring is the first milestone.** Backend already works;
  making it usable end-to-end is the highest-value next step.

### Back-compat shims (so nothing breaks mid-migration)

- `repo_dir` stays and means "the writable root." `repo_paths` defaults
  to `[]` (extra roots only). Effective search order =
  `repo_paths` (as listed) then `repo_dir` last; **first match wins**
  per `type@version`.
- `registry_url` stays; if `registries` is empty it's treated as
  `[registry_url]`. New deployments set `registries`.

Both `Config` and `service_meta` are generated from
`templates/apps/botgear.yml` — **all model changes go in the
template, then regenerate** (never edit `models/*.py` directly).

---

## 3. Target architecture

```
                 ┌─────────────────────── ordered local roots (read-only) ──────────────────────┐
repo_paths:      ./repo (dev)         ~/botgear-services (public split)    ~/.rlx/private
                 └──────────────────────────────────────────────────────────────────────────────┘
                                                  +
repo_dir:        var/repo  ← WRITABLE root: loads extract here, installs build .venv here
                                                  │
                 reconcile_catalog scans ALL roots → service_meta rows, each tagged repo_root
                                                  │
registries:      file:///tmp/repo/catalog.yml  ,  https://repo.botgear.ai/catalog.yml
                 └── searched in order for ABSENT types: first registry that has type@version wins
```

Per-row `repo_root` (new `service_meta` field) records which root a
LOADED type came from, so `install`/`uninstall` act on the correct dir.
Installing a type that lives in a read-only root copies its source into
the writable root first, then builds the venv there.

---

## 4. Phased work

Ordered per **D4** (UI first), then the multi-repo foundation (the
headline requirement), then real hosting.

> **Status (2026-06-13): all phases A–F implemented.** Built in
> dependency order (C → D → B → A → E → F) to avoid building the UI
> twice. New unit tests in `tests/test_registry.py` +
> `tests/test_multi_repo.py` (17 total) pass; full backend suite has
> zero new regressions; UI builds clean; a live boot confirms both
> single-root (unchanged dev behaviour) and multi-root reconcile. Live
> `http` / `gh_release` uploads are dry-run-verified only — they need a
> real remote host + credentials to exercise end to end.

### Phase A — UI wiring (against the existing backend)

Goal: a user can browse the remote catalog and drive the full
ABSENT→LOADED→INSTALLED→(uninstall) lifecycle from the Catalog page.

- New TS models: `CatalogResponse`, `CatalogEntry`, `TypeState`
  (`absent|loaded|installed|failed`) under
  `botgear_ui/src/models/`.
- `pages/Catalog.tsx`: add a **Installed / Browse registry** toggle and
  a **Refresh** button. Browse view calls `GET /v1/registry/catalog`,
  renders each `type@version` with a chip from `local_state`:
  - ABSENT → **Load** button (`POST /registry/load`)
  - LOADED → **Install** button (`POST /registry/install`)
  - INSTALLED → existing Install-instance / Uninstall actions
- Distinct failure surfaces: `load` errors (404/409/502) vs `install`
  errors (409) shown as separate banners.
- Reuse existing search box pattern from the Composer CATALOG palette.

Ships value immediately; no backend change required. Limitation: the
Install button blocks until the venv build finishes — fixed in Phase B.

### Phase B — Async install + progress

- `POST /registry/install` returns an `install_request` id immediately;
  the venv build runs in a worker. Wire `install_pip`'s `on_progress`
  to publish to the bus (`/registry/install/{id}/progress`), mirroring
  the existing `/service_request/{id}/progress` convention.
- UI shows a progress bar / live log on the LOADED→INSTALLED action
  (reuse `components/InstallProgress`).

### Phase C — `install_phase` model (D3)

- `templates/apps/botgear.yml`: add
  `service_meta.install_phase: Literal["absent","loaded","installing","installed","failed"]`
  and split `load_error` / `install_error`. Keep `installed` as a
  derived mirror for one release. Regenerate models (Python + TS).
- `registry.load/install/uninstall` and `_mark_installed` write
  `install_phase`. `_local_state_map` reads it directly instead of
  inferring. UI uses `install_phase` for the three-state chips and the
  failure split (replaces Phase A's heuristic).

### Phase D — Multi-source repos (D1 + D2) — the headline requirement

- `templates/apps/botgear.yml` Config: add `repo_paths: list[str]`
  and `registries: list[str]`; document `repo_dir` as the writable root
  and the back-compat shims (§2). Regenerate.
- `paths.py`: `repo_roots() -> list[Path]` (ordered, deduped) and
  `writable_repo_dir() -> Path`.
- `catalog.reconcile_catalog`: accept multiple roots, scan all, record
  `repo_root` per row, apply first-match-wins precedence. (Today it
  takes a single `repo_dir` — generalize signature; keep a
  single-arg shim.)
- `runtime/registry.py`:
  - `fetch_catalog` → `fetch_catalogs(urls)`; `find_in_catalog` searches
    registries in order (first hit wins), records which registry served
    a type.
  - `load` extracts to `writable_repo_dir()` regardless of source.
  - `install`/`uninstall` resolve the type's `repo_root`; if read-only,
    copy source into the writable root before building `.venv`.
- Framework adapters (`framework/adapters/in_process.py`,
  `framework/service.py` repo_dir resolution): resolve a type by
  searching `repo_roots()` rather than the single `repo_dir`.
- `registry_api.py`: `_resolve_repo_dir` → writable root;
  `_resolve_registry_url` → `registries` list; catalog endpoint merges
  across registries.
- UI: catalog rows show their source (registry URL / "local: <root>").

This is the "check locally, else search remote repos in order, download"
flow made real, plus the public-split-on-disk support.

### Phase E — Real remote hosting (Phase 5 of the design)

- `publish_services.py`: implement the `gh_release` target (upload
  archives as release assets, commit `catalog.yml`) and `http` (PUT).
- Stand up the public `supertick/botgear-services` repo as the split
  home; point a default `registries` entry at its `catalog.yml`.
- Confirm the bundled `uv` (shipped today by `packaging/build.sh`) drives
  the LOADED→INSTALLED venv build in a frozen install.

### Phase F — Sideload (Phase 6)

- On startup, scan `<install>/repo-staging/` for archives; extract into
  the writable root via Phase 3's extract code; log the sideload.
  Air-gapped install path.

---

## 5. Verification per phase

- **A/B/C:** local mirror loop end-to-end —
  `tools/build_services.py && tools/publish_services.py --target local`,
  then `BOTGEAR_REGISTRY_URL=file:///tmp/repo/catalog.yml ./botgear`,
  and drive Load → Install → Start from the UI for `echo_http` (pip) and
  `master_template` (builtin). Add unit tests alongside the existing
  registry tests.
- **D:** multi-root reconcile test (two local roots + a writable root,
  precedence + `repo_root` tagging); multi-registry fallback test
  (type only in the 2nd registry); install of a read-only-root pip type
  copies-then-builds. Verify dev mode (no `repo_paths`/`registries` set)
  is byte-for-byte unchanged.
- **E:** publish to a throwaway GH repo; install from the public URL on
  a clean box / fresh bundle.

---

## 6. Deferred / out of scope (unchanged from design)

Signing (Phase 8), service-on-service dependency graphs, multi-tenant
private registries with auth, install telemetry, a standalone web
registry browser.
