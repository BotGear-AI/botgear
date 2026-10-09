-- managed
-- SQL migration for audit_event table in packages
-- Creates the table if it doesn't exist, then ensures each column exists.
-- Safe to run multiple times (idempotent).

CREATE TABLE IF NOT EXISTS "audit_event" (
    "id" TEXT PRIMARY KEY,
    "ts" TIMESTAMPTZ,
    "service" TEXT,
    "tenant_id" TEXT,
    "actor_user_id" TEXT,
    "actor_email" TEXT,
    "action" TEXT,
    "entity_type" TEXT,
    "entity_id" TEXT,
    "parent_entity_id" TEXT,
    "summary" TEXT,
    "details" JSONB,
    "outcome" TEXT,
    "status_code" BIGINT,
    "source" TEXT,
    "request_id" TEXT
);

-- Add any missing columns to an existing table
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "id" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "ts" TIMESTAMPTZ;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "service" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "tenant_id" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "actor_user_id" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "actor_email" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "action" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "entity_type" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "entity_id" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "parent_entity_id" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "summary" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "details" JSONB;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "outcome" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "status_code" BIGINT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "source" TEXT;
ALTER TABLE "audit_event" ADD COLUMN IF NOT EXISTS "request_id" TEXT;
