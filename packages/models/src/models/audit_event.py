from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Any, Optional, Literal, Dict
import time
import json
import yaml

class AuditEvent(BaseModel):
    id: Optional[str] = Field(None, json_schema_extra={"example":"ae1a2b3c-0000-0000-0000-000000000001"}, description="Audit event id (UUID).")
    ts: Optional[datetime] = Field(None, json_schema_extra={"example":"2026-10-06T14:30:00Z"}, description="When the action completed (server clock, UTC).")
    service: Optional[str] = Field(None, json_schema_extra={"example":"continuous_mfa"}, description="App that recorded the event.")
    tenant_id: Optional[str] = Field(None, json_schema_extra={"example":"t1a2b3c4-0000-0000-0000-000000000001"}, description="Tenant whose data was touched; null for system-wide events.")
    actor_user_id: Optional[str] = Field(None, json_schema_extra={"example":"john@company1.com"}, description="User who performed the action; null for a background job with no user.")
    actor_email: Optional[str] = Field(None, json_schema_extra={"example":"john@company1.com"}, description="Actor email cached for display, so reads need no user join.")
    action: Optional[str] = Field(None, json_schema_extra={"example":"report.approve"}, description="Dotted action key '<area>.<verb>', e.g. 'auth.login', 'report.approve'.")
    entity_type: Optional[str] = Field(None, json_schema_extra={"example":"report"}, description="Type of the entity the action operated on.")
    entity_id: Optional[str] = Field(None, json_schema_extra={"example":"mi7vzo5c-e84e8i"}, description="Id of that entity.")
    parent_entity_id: Optional[str] = Field(None, json_schema_extra={"example":"run_123"}, description="Owning parent's id when the entity is a child — 'everything that happened under X' in one query.")
    summary: Optional[str] = Field(None, json_schema_extra={"example":"Approved report Q3 assay"}, description="One-line human-readable description for activity views.")
    details: Optional[Any] = Field(None, json_schema_extra={"example":{"before":"draft","after":"approved"}}, description="Structured payload — before/after, ids, counts. Shape varies per action.")
    outcome: Optional[Literal["success","failure","denied"]] = Field("success", json_schema_extra={"example":"success"}, description="Whether the action succeeded, failed, or was refused — failed and refused attempts are evidence too.")
    status_code: Optional[int] = Field(None, json_schema_extra={"example":200}, description="HTTP-style status of the action, when there is one.")
    source: Optional[Literal["api","job","system"]] = Field("api", json_schema_extra={"example":"api"}, description="What initiated the action — a user request, a background job, or the system itself.")
    request_id: Optional[str] = Field(None, json_schema_extra={"example":"req_a1b2c3"}, description="Request id shared with log_event — links an audit row to its request's logs.")

# managed
    @classmethod
    def from_yaml(cls, yaml_str: str):
        """Load AuditEvent from a YAML string."""
        data = yaml.safe_load(yaml_str)
        return cls(**data)

    @classmethod
    def from_yaml_file(cls, file_path: str):
        """Load AuditEvent from a YAML file."""
        with open(file_path, "r") as file:
            return cls.from_yaml(file.read())

    @classmethod
    def from_json(cls, json_str: str):
        """Load AuditEvent from a JSON string."""
        data = json.loads(json_str)
        return cls(**data)

    @classmethod
    def from_json_file(cls, file_path: str):
        """Load AuditEvent from a JSON file."""
        with open(file_path, "r") as file:
            return cls.from_json(file.read())
