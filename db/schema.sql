create extension if not exists vector;

create table if not exists papers (
    id           text primary key,  -- arXiv id without version suffix, e.g. 2410.01234
    title        text not null,
    abstract     text not null,
    authors      text[] not null default '{}',
    categories   text[] not null,
    published_at timestamptz not null,
    embedding    vector(384),       -- null until the embed step has run
    tldr         text,
    ingested_at  timestamptz not null default now()
);

create index if not exists papers_published_idx on papers (published_at desc);
create index if not exists papers_categories_idx on papers using gin (categories);
create index if not exists papers_embedding_idx on papers using hnsw (embedding vector_cosine_ops);

create table if not exists users (
    id         uuid primary key default gen_random_uuid(),
    categories text[] not null default '{}',  -- picked at signup, used for cold start
    created_at timestamptz not null default now()
);

-- Text + check instead of a Postgres enum: adding a value later is a one-line change
-- instead of an ALTER TYPE migration.
create table if not exists events (
    id         bigint generated always as identity primary key,
    user_id    uuid not null references users (id) on delete cascade,
    paper_id   text not null references papers (id) on delete cascade,
    kind       text not null check (kind in ('impression', 'click', 'save', 'dismiss')),
    ranker     text,  -- ranking version that produced the impression, for per-version metrics
    created_at timestamptz not null default now()
);

create index if not exists events_user_idx on events (user_id, created_at desc);
