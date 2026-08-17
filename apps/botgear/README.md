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

## Packaging

To produce a self-contained, distributable build (PyInstaller-bundled
backend + Vite-built UI + a bundled `uv` for runtime venv creation),
see [`packaging/README.md`](packaging/README.md). TL;DR:

```sh
./packaging/build.sh                 # full build → dist/botgear-latest-<platform>.tar.gz
SKIP_SMOKE=1 ./packaging/build.sh    # skip the post-build smoke test (faster)
```

