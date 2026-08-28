-- Extensions required by CLAUDE.md §4: pgvector (future embedding use) and pg_trgm
-- (trigram indexes on sanctions_alias.name and party_record.name, CLAUDE.md §6).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
