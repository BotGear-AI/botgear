// managed
// Config interface for use across the project
export interface Config {
  id?: string;
  port?: number;
  ssl_enabled?: boolean;
  open_browser?: boolean;
  database_type: "postgres" | "lowdb" | "sqlite" | "filesystem" | "none";
  data_dir?: string;
  repo_dir?: string;
  repo_paths?: any;
  registries?: any;
  jwt_access_token_ttl_minutes?: number;
  runtime_id?: string;
  registry_url?: string;
  update_repo?: string;
  auth_bootstrap: "admin_seed" | "first_user_claim";
}

export function createEmptyConfig(): Config {
  return {
    id: "default",
    port: 8998,
    ssl_enabled: true,
    open_browser: true,
    database_type: "sqlite",
    data_dir: "data",
    repo_dir: "repo",
    repo_paths: undefined,
    registries: undefined,
    jwt_access_token_ttl_minutes: 1440,
    runtime_id: undefined,
    registry_url: "https://github.com/BotGear-AI/botgear/releases/download/services/catalog.yml",
    update_repo: "BotGear-AI/botgear",
    auth_bootstrap: "first_user_claim",
  };
}

// Helper function to validate Config object
export function isConfig(obj: any): obj is Config {
  return obj && typeof obj === 'object';
}

// Helper function to clone Config object
export function cloneConfig(item: Config): Config {
  return {
    ...item,
  };
}

// JSON Schema for Fastify route validation and OpenAPI documentation
export const ConfigSchema = {
  "type": "object",
  "properties": {
    "id": {
      "type": "string",
      "description": "Config record id (use 'default' for the singleton app config)",
      "example": "default"
    },
    "port": {
      "type": "integer",
      "description": "HTTP(S) listen port. Overrides ConfigBase's generic 8001 default with botgear's assigned slot in the monorepo's port registry (tools/PORTS.md), so a fresh checkout doesn't collide with sibling apps run side-by-side.",
      "example": 8998
    },
    "ssl_enabled": {
      "type": "boolean",
      "description": "Serve HTTPS/WSS using a self-signed cert (ssl/cert.pem, ssl/key.pem \u2014 auto-generated on first boot if missing). Overrides ConfigBase's default-off so a fresh clone matches botgear_ui's dev server, which auto-detects and reuses that same cert and derives ws vs wss from its own https/http choice \u2014 leaving this False while the UI has picked up an https cert breaks the WebSocket handshake (endless 'disconnected \u2014 retrying').",
      "example": true
    },
    "open_browser": {
      "type": "boolean",
      "description": "Open the default web browser at this runtime's address when it starts. Only on an interactive start (launched from a terminal or console, with a graphical display on Linux); never on a restart or self-update, so headless robots and services are unaffected. Set BOTGEAR_OPEN_BROWSER=false to turn it off.",
      "example": true
    },
    "database_type": {
      "type": "string",
      "enum": [
        "postgres",
        "lowdb",
        "sqlite",
        "filesystem",
        "none"
      ],
      "description": "Database backend. sqlite is the default \u2014 lowdb/TinyDB rewrites a whole table's JSON file on every write, so a process killed mid-write (SIGKILL, crash) leaves a truncated, unreadable table; sqlite's journal/WAL makes that impossible.",
      "example": "sqlite"
    },
    "data_dir": {
      "type": "string",
      "description": "Per-process working directory. Houses databases/ (TinyDB JSON files), runtime_id (federation handle), admin_password.txt, and any other instance-local state. Set per-runtime via BOTGEAR_DATA_DIR; sibling runtimes on the same box use distinct values to keep their state isolated.",
      "example": "data"
    },
    "repo_dir": {
      "type": "string",
      "description": "The WRITABLE service repo root. Loads (downloaded archives) extract here and installs (per-type .venv) build here. Dev uses 'repo' (the monorepo checkout); a frozen bundle uses var/repo (seeded by packaging/entry.py). CWD-relative when not absolute.",
      "example": "repo"
    },
    "repo_paths": {
      "description": "Additional READ-ONLY local repo roots scanned for service types, in order, on top of the writable repo_dir. Lets a runtime reference a separate public botgear-services checkout and/or a private services directory without copying them in. Each entry is a filesystem path to a directory holding <type>/<version>/ dirs. Resolution precedence is writable repo_dir first, then these in listed order, first match per type@version wins. Empty/None in normal dev \u2014 the single repo_dir is enough.",
      "example": [
        "/home/me/botgear-services",
        "/home/me/.rlx/private"
      ],
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "registries": {
      "description": "Ordered list of remote catalog.yml URLs searched for ABSENT types (those not present in any local repo root). The first registry that has a requested type@version serves it. Supports file:// (local mirror) and http(s)://. When empty/None, falls back to the single registry_url. Lets a runtime pull from the public registry plus a private mirror.",
      "example": [
        "https://github.com/BotGear-AI/botgear/releases/download/services/catalog.yml",
        "file:///tmp/repo/catalog.yml"
      ],
      "type": "array",
      "items": {
        "type": "string"
      }
    },
    "jwt_access_token_ttl_minutes": {
      "type": "integer",
      "description": "Access-token JWT lifetime in minutes. The UI refreshes the token via /v1/refresh-token when it expires; this knob controls how often that happens. Default 60.",
      "example": 1440
    },
    "runtime_id": {
      "type": "string",
      "description": "Override the runtime's federation id (the handle peers address us by; defaults to the sanitized machine hostname). Set via BOTGEAR_RUNTIME_ID in .env. When None, identity.py auto-generates + persists one to data/runtime_id.",
      "example": "funny-droid"
    },
    "registry_url": {
      "type": "string",
      "description": "URL of the remote service registry's catalog.yml, consulted for service types not present locally. Defaults to the public catalog published with each release to the BotGear-AI/botgear 'services' release. Supports file:// (a local mirror, e.g. file:///tmp/repo/catalog.yml from tools/publish_services.py --kind file) and http(s)://.",
      "example": "https://github.com/BotGear-AI/botgear/releases/download/services/catalog.yml"
    },
    "update_repo": {
      "type": "string",
      "description": "GitHub owner/name whose Releases provide framework self-updates. 'Check for updates' polls this repo's latest release and compares its tag to the running build version; the matching platform tarball asset is the download. See docs/TODO_UPGRADE_FEATURE.md.",
      "example": "BotGear-AI/botgear"
    },
    "auth_bootstrap": {
      "type": "string",
      "enum": [
        "admin_seed",
        "first_user_claim"
      ],
      "description": "How the user table is initialised on a fresh runtime. 'admin_seed' auto-creates admin@cloudseeder.ai with a generated password. 'first_user_claim' skips that seed and lets the operator establish the first account via POST /v1/auth/claim-first-user \u2014 paired with event_handlers.on_first_user for per-app role assignment. Generator-injected by app.auth.bootstrap.",
      "example": "first_user_claim"
    }
  },
  "required": [
    "database_type",
    "auth_bootstrap"
  ]
}
