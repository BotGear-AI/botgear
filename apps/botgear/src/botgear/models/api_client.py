from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Any, Optional, Literal, Dict
import time
import json
import yaml

class ApiClient(BaseModel):
    """API client credentials for external/service-to-service access. A client authenticates by sending its secret in the X-API-Key header; the matching row's roles then drive the same role gates as a logged-in user. Full CRUD for management (Admin-gated); validate_api_client_secret is an internal helper the auth package's X-API-Key handler calls and is not exposed as a route."""
    id: Optional[str] = Field(None, json_schema_extra={"example":"client-123e4567-e89b-12d3-a456-426614174000"})
    name: Optional[str] = Field(None, description="Human-readable label, e.g. the integration or owner that uses this client", json_schema_extra={"example":"Reporting Pipeline"})
    secret: Optional[str] = Field(None, description="Shared secret presented in the X-API-Key header; the row authenticates when it matches. Minted server-side on create when omitted", json_schema_extra={"example":"supersecret"})
    roles: Optional[list[str]] = Field([], description="Roles granted to requests authenticated by this client; an Admin-roled key is a full admin credential", json_schema_extra={"example":["Admin"]})
    created: Optional[int] = Field(None, description="Creation time, epoch milliseconds", json_schema_extra={"example":1683123456789})

# managed
    @classmethod
    def from_yaml(cls, yaml_str: str):
        """Load ApiClient from a YAML string."""
        data = yaml.safe_load(yaml_str)
        return cls(**data)

    @classmethod
    def from_yaml_file(cls, file_path: str):
        """Load ApiClient from a YAML file."""
        with open(file_path, "r") as file:
            return cls.from_yaml(file.read())

    @classmethod
    def from_json(cls, json_str: str):
        """Load ApiClient from a JSON string."""
        data = json.loads(json_str)
        return cls(**data)

    @classmethod
    def from_json_file(cls, file_path: str):
        """Load ApiClient from a JSON file."""
        with open(file_path, "r") as file:
            return cls.from_json(file.read())
