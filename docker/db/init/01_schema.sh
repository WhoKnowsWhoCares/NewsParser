#!/bin/sh
# Create schema with configurable embedding dimension (default 4096, qwen3-embedding-8b).
# Set EMBED_DIM in .env to match the embeddings model.
# Schema is shared with CI via the templated file (schema.sql.template).
DIR="$(cd "$(dirname "$0")" && pwd)"
EMBED_DIM="${EMBED_DIM:-4096}"

sed "s/{EMBED_DIM}/$EMBED_DIM/g" "$DIR/schema.sql.template" | \
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
