# xoult

A personalized daily arXiv feed. New papers are embedded, given a short TLDR, and ranked
for each reader based on what they save and dismiss.

## Local setup

Requires [uv](https://docs.astral.sh/uv/) and either Docker or a free Supabase project.

```sh
uv sync
cp .env.example .env          # point DATABASE_URL at Supabase if not using Docker
docker compose up -d db       # skip if using Supabase
uv run python -m xoult.db     # applies db/schema.sql and checks the connection
```
