-- Phase 5.7: document status transitions need sanitized failure
-- information. The roadmap step sets `status = 'failed' with an error
-- message` when ingestion fails, but the Phase 2 `documents` table has
-- no error field (verified: 20260908163425_initial_schema.sql), so the
-- columns are added here rather than invented manually.
--
-- The stored values are application-controlled, sanitized data:
--
-- - `failure_stage` is a controlled category slug from the ingestion
--   service (`IngestionError.stage`, e.g. `storage_download`) or the
--   literal `unexpected` for failures outside the normalized pipeline.
-- - `failure_message` is a static, human-readable description that
--   never embeds storage paths, credentials, PDF/chunk text, embedding
--   values, or uncontrolled exception messages.
--
-- The check constraint enforces the invariant the API depends on:
-- failure fields can only exist on a `failed` document, and when they
-- do, they are complete. A `ready` document therefore always carries
-- `failure_stage IS NULL AND failure_message IS NULL`, and `failed`
-- rows that record failure info always record both fields.

alter table public.documents
    add column failure_stage text,
    add column failure_message text;

alter table public.documents
    add constraint documents_failure_fields_only_when_failed
    check (
        (failure_stage is null and failure_message is null)
        or
        (
            status = 'failed'
            and failure_stage is not null
            and failure_message is not null
        )
    );
