CREATE EXTENSION IF NOT EXISTS pg_jieba;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_groups (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    article_count BIGINT NOT NULL DEFAULT 0,
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
    article_count BIGINT NOT NULL DEFAULT 0,
    sync_mode TEXT,
    sync_recent_days INTEGER,
    sync_interval_days INTEGER DEFAULT NULL,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_group
ON accounts (group_id);

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS sync_interval_days INTEGER DEFAULT NULL;

CREATE TABLE IF NOT EXISTS articles (
    id SERIAL PRIMARY KEY,
    biz TEXT NOT NULL REFERENCES accounts(biz) ON DELETE CASCADE,
    article_id TEXT NOT NULL,
    title TEXT NOT NULL,
    item_show_type INTEGER,
    author TEXT,
    digest TEXT,
    cover INTEGER,
    link TEXT NOT NULL,
    source_url TEXT,
    publish_at BIGINT,
    raw_json TEXT NOT NULL,
    search_vector tsvector,
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

CREATE INDEX IF NOT EXISTS idx_articles_biz_publish_id
ON articles (biz, publish_at DESC NULLS LAST, id DESC);

CREATE INDEX IF NOT EXISTS idx_articles_publish_id
ON articles (publish_at DESC NULLS LAST, id DESC);

CREATE INDEX IF NOT EXISTS idx_articles_article_id
ON articles (article_id);

CREATE TABLE IF NOT EXISTS article_images (
    id SERIAL PRIMARY KEY,
    article_pk INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    orig_url TEXT,
    hash_algo TEXT,
    content_hash TEXT,
    content_type TEXT,
    s3_key TEXT,
    failed_at TIMESTAMPTZ,
    failed_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (article_pk, orig_url)
);

CREATE INDEX IF NOT EXISTS idx_article_images_pending
ON article_images (id)
WHERE (s3_key IS NULL OR s3_key = '') AND orig_url IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_article_images_article_pk_position
ON article_images (article_pk, position);

CREATE INDEX IF NOT EXISTS idx_article_images_content_hash
ON article_images (content_hash)
WHERE content_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS blocked_image_hashes (
    id SERIAL PRIMARY KEY,
    hash_algo TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_image_id INTEGER REFERENCES article_images(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (hash_algo, content_hash)
);

CREATE TABLE IF NOT EXISTS sync_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL,
    biz_list JSONB,
    phase TEXT,
    accounts_total INTEGER NOT NULL DEFAULT 0,
    accounts_done INTEGER NOT NULL DEFAULT 0,
    current_account JSONB,
    current_article JSONB,
    last_log TEXT,
    report JSONB,
    accounts JSONB NOT NULL DEFAULT '[]'::jsonb,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    locked_by TEXT,
    locked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_status_created_at
ON sync_jobs (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_trigger_type_status
ON sync_jobs (trigger_type, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_accounts_nickname_trgm
ON accounts USING GIN (nickname gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_accounts_alias_trgm
ON accounts USING GIN (alias gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_accounts_biz_trgm
ON accounts USING GIN (biz gin_trgm_ops);

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

CREATE INDEX IF NOT EXISTS idx_login_sessions_default_id_desc
ON login_sessions (is_default, id DESC);

CREATE OR REPLACE FUNCTION hippo_articles_account_count_trigger()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE accounts
        SET article_count = article_count + 1,
            updated_at = NOW()
        WHERE biz = NEW.biz;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        UPDATE accounts
        SET article_count = GREATEST(article_count - 1, 0),
            updated_at = NOW()
        WHERE biz = OLD.biz;
        RETURN OLD;
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_articles_account_count_insert ON articles;
CREATE TRIGGER trg_articles_account_count_insert
AFTER INSERT ON articles
FOR EACH ROW EXECUTE FUNCTION hippo_articles_account_count_trigger();

DROP TRIGGER IF EXISTS trg_articles_account_count_delete ON articles;
CREATE TRIGGER trg_articles_account_count_delete
AFTER DELETE ON articles
FOR EACH ROW EXECUTE FUNCTION hippo_articles_account_count_trigger();

CREATE OR REPLACE FUNCTION hippo_accounts_group_article_count_trigger()
RETURNS trigger AS $$
DECLARE
    delta BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.group_id IS NOT NULL THEN
            UPDATE account_groups
            SET article_count = GREATEST(article_count - COALESCE(OLD.article_count, 0), 0),
                updated_at = NOW()
            WHERE id = OLD.group_id;
        END IF;
        RETURN OLD;
    END IF;

    IF NEW.group_id IS DISTINCT FROM OLD.group_id THEN
        IF OLD.group_id IS NOT NULL THEN
            UPDATE account_groups
            SET article_count = GREATEST(article_count - COALESCE(OLD.article_count, 0), 0),
                updated_at = NOW()
            WHERE id = OLD.group_id;
        END IF;
        IF NEW.group_id IS NOT NULL THEN
            UPDATE account_groups
            SET article_count = article_count + COALESCE(NEW.article_count, 0),
                updated_at = NOW()
            WHERE id = NEW.group_id;
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.article_count IS DISTINCT FROM OLD.article_count THEN
        delta := COALESCE(NEW.article_count, 0) - COALESCE(OLD.article_count, 0);
        IF delta <> 0 AND NEW.group_id IS NOT NULL THEN
            UPDATE account_groups
            SET article_count = GREATEST(article_count + delta, 0),
                updated_at = NOW()
            WHERE id = NEW.group_id;
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_accounts_group_article_count_update ON accounts;
CREATE TRIGGER trg_accounts_group_article_count_update
AFTER UPDATE OF group_id, article_count ON accounts
FOR EACH ROW EXECUTE FUNCTION hippo_accounts_group_article_count_trigger();

DROP TRIGGER IF EXISTS trg_accounts_group_article_count_delete ON accounts;
CREATE TRIGGER trg_accounts_group_article_count_delete
BEFORE DELETE ON accounts
FOR EACH ROW EXECUTE FUNCTION hippo_accounts_group_article_count_trigger();

CREATE OR REPLACE FUNCTION hippo_rebuild_article_counts()
RETURNS void AS $$
BEGIN
    UPDATE accounts a
    SET article_count = COALESCE(x.cnt, 0),
        updated_at = NOW()
    FROM (
        SELECT biz, COUNT(*)::bigint AS cnt
        FROM articles
        GROUP BY biz
    ) x
    WHERE a.biz = x.biz;

    UPDATE accounts a
    SET article_count = 0,
        updated_at = NOW()
    WHERE NOT EXISTS (
        SELECT 1
        FROM articles ar
        WHERE ar.biz = a.biz
    );

    UPDATE account_groups g
    SET article_count = COALESCE(s.sum_cnt, 0),
        updated_at = NOW()
    FROM (
        SELECT group_id, SUM(article_count)::bigint AS sum_cnt
        FROM accounts
        WHERE group_id IS NOT NULL
        GROUP BY group_id
    ) s
    WHERE g.id = s.group_id;

    UPDATE account_groups g
    SET article_count = 0,
        updated_at = NOW()
    WHERE NOT EXISTS (
        SELECT 1
        FROM accounts a
        WHERE a.group_id = g.id
    );
END;
$$ LANGUAGE plpgsql;

SELECT hippo_rebuild_article_counts();

CREATE TABLE IF NOT EXISTS article_download_attempts (
    id SERIAL PRIMARY KEY,
    biz TEXT NOT NULL REFERENCES accounts(biz) ON DELETE CASCADE,
    article_id TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (biz, article_id)
);

CREATE INDEX IF NOT EXISTS idx_article_download_attempts_biz_article
ON article_download_attempts (biz, article_id);
