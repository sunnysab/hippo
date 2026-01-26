CREATE EXTENSION IF NOT EXISTS pg_jieba;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_groups (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sync_mode TEXT,
    sync_recent_days INTEGER,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    biz TEXT PRIMARY KEY,
    nickname TEXT NOT NULL,
    alias TEXT,
    round_head_img TEXT,
    group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL,
    is_disabled BOOLEAN NOT NULL DEFAULT FALSE,
    sync_mode TEXT,
    sync_recent_days INTEGER,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE accounts
ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE accounts
ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL;

ALTER TABLE accounts
ADD COLUMN IF NOT EXISTS sync_mode TEXT;

ALTER TABLE accounts
ADD COLUMN IF NOT EXISTS sync_recent_days INTEGER;

ALTER TABLE accounts
DROP COLUMN IF EXISTS is_default;

ALTER TABLE accounts
DROP COLUMN IF EXISTS uin,
DROP COLUMN IF EXISTS key,
DROP COLUMN IF EXISTS pass_ticket;

ALTER TABLE account_groups
ADD COLUMN IF NOT EXISTS sync_mode TEXT;

ALTER TABLE account_groups
ADD COLUMN IF NOT EXISTS sync_recent_days INTEGER;

CREATE INDEX IF NOT EXISTS idx_accounts_group
ON accounts (group_id);

CREATE TABLE IF NOT EXISTS articles (
    id SERIAL PRIMARY KEY,
    biz TEXT NOT NULL REFERENCES accounts(biz) ON DELETE CASCADE,
    article_id TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    digest TEXT,
    cover INTEGER,
    link TEXT NOT NULL,
    source_url TEXT,
    publish_at BIGINT,
    raw_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (biz, article_id)
);

CREATE TABLE IF NOT EXISTS article_content (
    id SERIAL PRIMARY KEY,
    article_pk INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    url_token TEXT,
    clean_html TEXT,
    content_markdown TEXT,
    content_json JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (article_pk)
);

CREATE INDEX IF NOT EXISTS idx_article_content_article_pk
ON article_content (article_pk);

ALTER TABLE articles
ADD COLUMN IF NOT EXISTS search_vector tsvector;

CREATE OR REPLACE FUNCTION build_article_search_vector(
    title TEXT,
    author TEXT,
    digest TEXT,
    content TEXT
) RETURNS tsvector AS $$
SELECT
    setweight(to_tsvector('jiebaqry', COALESCE(title, '')), 'A') ||
    setweight(to_tsvector('jiebaqry', COALESCE(author, '')), 'C') ||
    setweight(to_tsvector('jiebaqry', COALESCE(digest, '')), 'B') ||
    setweight(
        to_tsvector('jiebaqry', COALESCE(SUBSTRING(content FROM 1 FOR 50000), '')),
        'B'
    );
$$ LANGUAGE sql STABLE;

CREATE OR REPLACE FUNCTION articles_search_vector_trigger()
RETURNS trigger AS $$
DECLARE
    content_text TEXT;
BEGIN
    IF TG_OP = 'INSERT' AND NEW.id IS NULL THEN
        content_text := '';
    ELSE
        SELECT COALESCE(c.content_markdown, c.clean_html, '')
        INTO content_text
        FROM article_content c
        WHERE c.article_pk = NEW.id;
    END IF;
    NEW.search_vector = build_article_search_vector(
        NEW.title,
        NEW.author,
        NEW.digest,
        content_text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_articles_search_vector ON articles;

CREATE TRIGGER trg_articles_search_vector
BEFORE INSERT OR UPDATE OF title, author, digest
ON articles
FOR EACH ROW EXECUTE FUNCTION articles_search_vector_trigger();

CREATE OR REPLACE FUNCTION article_content_search_vector_trigger()
RETURNS trigger AS $$
BEGIN
    UPDATE articles
    SET search_vector = build_article_search_vector(
        title,
        author,
        digest,
        COALESCE(NEW.content_markdown, NEW.clean_html, '')
    )
    WHERE id = NEW.article_pk;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_article_content_search_vector ON article_content;

CREATE TRIGGER trg_article_content_search_vector
AFTER INSERT OR UPDATE OF content_markdown, clean_html
ON article_content
FOR EACH ROW EXECUTE FUNCTION article_content_search_vector_trigger();

UPDATE articles a
SET search_vector = build_article_search_vector(
    a.title,
    a.author,
    a.digest,
    COALESCE(c.content_markdown, c.clean_html, '')
)
FROM article_content c
WHERE c.article_pk = a.id AND a.search_vector IS NULL;

UPDATE articles
SET search_vector = build_article_search_vector(title, author, digest, '')
WHERE search_vector IS NULL;

CREATE INDEX IF NOT EXISTS idx_articles_search_vector
ON articles USING GIN (search_vector);

CREATE INDEX IF NOT EXISTS idx_articles_biz_publish
ON articles (biz, publish_at DESC);

CREATE TABLE IF NOT EXISTS article_images (
    id SERIAL PRIMARY KEY,
    article_pk INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    orig_url TEXT,
    content_type TEXT,
    s3_key TEXT,
    failed_at TIMESTAMPTZ,
    failed_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (article_pk, orig_url)
);

ALTER TABLE article_images
ADD COLUMN IF NOT EXISTS s3_key TEXT,
ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS failed_reason TEXT,
DROP COLUMN IF EXISTS data;

CREATE INDEX IF NOT EXISTS idx_article_images_pending
ON article_images (id)
WHERE (s3_key IS NULL OR s3_key = '') AND orig_url IS NOT NULL;

CREATE TABLE IF NOT EXISTS login_sessions (
    id SERIAL PRIMARY KEY,
    token TEXT NOT NULL,
    cookies_json TEXT NOT NULL,
    nickname TEXT,
    avatar TEXT,
    is_default BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
