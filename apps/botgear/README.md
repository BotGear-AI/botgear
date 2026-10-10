<!-- managed -->
# Botgear

FastAPI backend for `botgear`.

## Quick Start

### 1. Install uv

`uv` is a fast Python package manager. Install it once per machine:

| Platform | Command |
|----------|---------|
| macOS / Linux | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| macOS (Homebrew) | `brew install uv` |
| Windows (PowerShell) | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |

### 2. Install dependencies

**macOS / Linux**
```sh
cd apps/botgear
./install.sh
source .venv/bin/activate
```

**Windows**
```bat
cd apps\botgear
install.bat
.venv\Scripts\activate
```

### 3. Run

```sh
python -m botgear.main
```

API available at <http://localhost:8001>  
Swagger UI at <http://localhost:8001/docs>

## Testing

```sh
pytest                                  # run all tests
pytest --cov=botgear --cov-report html   # with coverage report
```

## Docker

```sh
# Build (run from repo root)
docker build -t botgear -f apps/botgear/Dockerfile .

# Run
docker run --env-file .env --rm -p 8001:8001 botgear

# Interactive shell
docker run --env-file .env --rm -it botgear /bin/sh
```

```sh
# Or with docker-compose
cd apps/botgear
docker-compose up --build
```

## Configuration

Copy `.env.example` to `.env` and fill in the required values before running.
See inline comments in `src/botgear/models/config.py` for all available settings.

## Updating a packaged install

Releases are published at https://github.com/BotGear-AI/botgear/releases
as `botgear-<X.Y.Z>-<platform>` bundles (`linux-x86_64`, `linux-aarch64`,
`darwin-arm64`, `windows-x86_64`). A running packaged build checks the
repository named by `update_repo` (default `BotGear-AI/botgear`):

- **Check** — the **Updates** page (Admin) or `GET /v1/update/check` compares
  the running version with the repo's latest release and finds this
  platform's bundle + `.sha256`. Dev builds (`latest`, `dev-<sha>`, `0.0.0`)
  are never offered updates; prereleases aren't offered until promoted.
- **Update now** (`POST /v1/update/framework`, Linux/macOS packaged builds):
  download → verify sha256 → extract beside the current install as
  `botgear-<new>-<platform>/` → drain services → back up `var/` to
  `rlx-backups/<old>-<timestamp>/` → copy `var/` (users, config sets,
  `security/key.bin`, runtime id) and `.env` into the new install → restart
  into the new binary. Progress streams on `/system/update/progress`.
- **Rollback is manual**: the old install and the `var/` backup are kept;
  start the old binary to go back. Automatic rollback is not built.
- **Windows**: the check reports a new release and links to the download
  page; self-update isn't supported (download and unzip the new release).

Service types update separately through the Catalog (see the
`repo/README.md` registry docs).

## Packaging

To produce a self-contained, distributable build (PyInstaller-bundled
backend + Vite-built UI + a bundled `uv` for runtime venv creation),
see [`packaging/README.md`](packaging/README.md). TL;DR:

```sh
./packaging/build.sh                 # full build → dist/botgear-latest-<platform>.tar.gz
SKIP_SMOKE=1 ./packaging/build.sh    # skip the post-build smoke test (faster)
```

