from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Any, Optional, Literal, Dict
import time
import json
import yaml
from models.config_base import ConfigBase

# .env file for configuration
class Config(ConfigBase):
    id: Optional[str] = Field("default", description="Config record id (use 'default' for the singleton app config)", json_schema_extra={"example":"default"})
    port: Optional[int] = Field(8998, description="HTTP(S) listen port. Overrides ConfigBase's generic 8001 default with botgear's assigned slot in the monorepo's port registry (tools/PORTS.md), so a fresh checkout doesn't collide with sibling apps run side-by-side.", json_schema_extra={"example":8998})
    ssl_enabled: Optional[bool] = Field(True, description="Serve HTTPS/WSS using a self-signed cert (ssl/cert.pem, ssl/key.pem — auto-generated on first boot if missing). Overrides ConfigBase's default-off so a fresh clone matches botgear_ui's dev server, which auto-detects and reuses that same cert and derives ws vs wss from its own https/http choice — leaving this False while the UI has picked up an https cert breaks the WebSocket handshake (endless 'disconnected — retrying').", json_schema_extra={"example":True})
    database_type: Literal["postgres", "lowdb", "sqlite", "filesystem", "none"] = Field("sqlite", description="Database backend. sqlite is the default — lowdb/TinyDB rewrites a whole table's JSON file on every write, so a process killed mid-write (SIGKILL, crash) leaves a truncated, unreadable table; sqlite's journal/WAL makes that impossible.", json_schema_extra={"example":"sqlite"})
    data_dir: Optional[str] = Field("data", description="Per-process working directory. Houses databases/ (TinyDB JSON files), runtime_id (federation handle), admin_password.txt, and any other instance-local state. Set per-runtime via BOTGEAR_DATA_DIR; sibling runtimes on the same box use distinct values to keep their state isolated.", json_schema_extra={"example":"data"})
    repo_dir: Optional[str] = Field("repo", description="The WRITABLE service repo root. Loads (downloaded archives) extract here and installs (per-type .venv) build here. Dev uses 'repo' (the monorepo checkout); a frozen bundle uses var/repo (seeded by packaging/entry.py). CWD-relative when not absolute.", json_schema_extra={"example":"repo"})
    repo_paths: Optional[list] = Field(None, description="Additional READ-ONLY local repo roots scanned for service types, in order, on top of the writable repo_dir. Lets a runtime reference a separate public botgear-services checkout and/or a private services directory without copying them in. Each entry is a filesystem path to a directory holding <type>/<version>/ dirs. Resolution precedence is writable repo_dir first, then these in listed order, first match per type@version wins. Empty/None in normal dev — the single repo_dir is enough.", json_schema_extra={"type":"array","items":{"type":"string"},"example":["/home/me/botgear-services","/home/me/.rlx/private"]})
    registries: Optional[list] = Field(None, description="Ordered list of remote catalog.yml URLs searched for ABSENT types (those not present in any local repo root). The first registry that has a requested type@version serves it. Supports file:// (local mirror) and http(s)://. When empty/None, falls back to the single registry_url. Lets a runtime pull from the public registry plus a private mirror.", json_schema_extra={"type":"array","items":{"type":"string"},"example":["https://github.com/BotGear-AI/botgear/releases/download/services/catalog.yml","file:///tmp/repo/catalog.yml"]})
    jwt_access_token_ttl_minutes: Optional[int] = Field(1440, description="Access-token JWT lifetime in minutes. The UI refreshes the token via /v1/refresh-token when it expires; this knob controls how often that happens. Default 60.", json_schema_extra={"example":1440})
    runtime_id: Optional[str] = Field(None, description="Override the runtime's federation id (the handle peers address us by; defaults to the sanitized machine hostname). Set via BOTGEAR_RUNTIME_ID in .env. When None, identity.py auto-generates + persists one to data/runtime_id.", json_schema_extra={"example":"funny-droid"})
    registry_url: Optional[str] = Field("https://github.com/BotGear-AI/botgear/releases/download/services/catalog.yml", description="URL of the remote service registry's catalog.yml, consulted for service types not present locally. Defaults to the public catalog published with each release to the BotGear-AI/botgear 'services' release. Supports file:// (a local mirror, e.g. file:///tmp/repo/catalog.yml from tools/publish_services.py --kind file) and http(s)://.", json_schema_extra={"example":"https://github.com/BotGear-AI/botgear/releases/download/services/catalog.yml"})
    update_repo: Optional[str] = Field("BotGear-AI/botgear", description="GitHub owner/name whose Releases provide framework self-updates. 'Check for updates' polls this repo's latest release and compares its tag to the running build version; the matching platform tarball asset is the download. See docs/TODO_UPGRADE_FEATURE.md.", json_schema_extra={"example":"BotGear-AI/botgear"})
    auth_bootstrap: Literal["admin_seed", "first_user_claim"] = Field("first_user_claim", description="How the user table is initialised on a fresh runtime. 'admin_seed' auto-creates admin@cloudseeder.ai with a generated password. 'first_user_claim' skips that seed and lets the operator establish the first account via POST /v1/auth/claim-first-user — paired with event_handlers.on_first_user for per-app role assignment. Generator-injected by app.auth.bootstrap.", json_schema_extra={"example":"first_user_claim"})

    model_config = ConfigDict(
        env_prefix="BOTGEAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

# managed
    @classmethod
    def from_yaml(cls, yaml_str: str):
        """Load Config from a YAML string."""
        data = yaml.safe_load(yaml_str)
        return cls(**data)

    @classmethod
    def from_yaml_file(cls, file_path: str):
        """Load Config from a YAML file."""
        with open(file_path, "r") as file:
            return cls.from_yaml(file.read())

    @classmethod
    def from_json(cls, json_str: str):
        """Load Config from a JSON string."""
        data = json.loads(json_str)
        return cls(**data)

    @classmethod
    def from_json_file(cls, file_path: str):
        """Load Config from a JSON file."""
        with open(file_path, "r") as file:
            return cls.from_json(file.read())
