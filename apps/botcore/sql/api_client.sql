-- managed
-- SQL migration for api_client table in botcore
-- Creates the table if it doesn't exist, then ensures each column exists.
-- Safe to run multiple times (idempotent).

CREATE TABLE IF NOT EXISTS "api_client" (
    "id" TEXT PRIMARY KEY,
    "name" TEXT,
    "secret" TEXT,
    "roles" JSONB,
    "created" BIGINT
);

-- Add any missing columns to an existing table
ALTER TABLE "api_client" ADD COLUMN IF NOT EXISTS "id" TEXT;
ALTER TABLE "api_client" ADD COLUMN IF NOT EXISTS "name" TEXT;
ALTER TABLE "api_client" ADD COLUMN IF NOT EXISTS "secret" TEXT;
ALTER TABLE "api_client" ADD COLUMN IF NOT EXISTS "roles" JSONB;
ALTER TABLE "api_client" ADD COLUMN IF NOT EXISTS "created" BIGINT;
