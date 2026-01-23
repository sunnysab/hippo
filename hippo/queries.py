"""Centralized SQL statements for PostgreSQL storage."""

SCHEMA_INIT_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_groups (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        sync_mode TEXT,
        sync_recent_days INTEGER,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        biz TEXT PRIMARY KEY,
        nickname TEXT NOT NULL,
        alias TEXT,
        round_head_img TEXT,
        uin TEXT NOT NULL,
        key TEXT NOT NULL,
        pass_ticket TEXT NOT NULL,
        group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL,
        is_disabled BOOLEAN NOT NULL DEFAULT FALSE,
        sync_mode TEXT,
        sync_recent_days INTEGER,
        last_synced_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT FALSE
    """,
    """
    ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS sync_mode TEXT
    """,
    """
    ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS sync_recent_days INTEGER
    """,
    """
    ALTER TABLE accounts
    DROP COLUMN IF EXISTS is_default
    """,
    """
    ALTER TABLE account_groups
    ADD COLUMN IF NOT EXISTS sync_mode TEXT
    """,
    """
    ALTER TABLE account_groups
    ADD COLUMN IF NOT EXISTS sync_recent_days INTEGER
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_accounts_group
    ON accounts (group_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS articles (
        id SERIAL PRIMARY KEY,
        biz TEXT NOT NULL REFERENCES accounts(biz) ON DELETE CASCADE,
        article_id TEXT NOT NULL,
        title TEXT NOT NULL,
        author TEXT,
        digest TEXT,
        cover TEXT,
        link TEXT NOT NULL,
        source_url TEXT,
        publish_at BIGINT,
        raw_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE (biz, article_id)
    )
    """,
    """
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
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_article_content_article_pk
    ON article_content (article_pk)
    """,
]


ARTICLES_COLUMN_CHECK_QUERY = """
SELECT column_name
FROM information_schema.columns
WHERE table_name = 'articles'
AND column_name IN (
    'url_token',
    'clean_html',
    'content_markdown',
    'content_json'
)
"""


ARTICLE_CONTENT_MIGRATION_QUERY = """
INSERT INTO article_content
    (article_pk, url_token, clean_html, content_markdown, content_json,
     created_at, updated_at)
SELECT
    id,
    url_token,
    clean_html,
    content_markdown,
    content_json::jsonb,
    created_at,
    updated_at
FROM articles
WHERE url_token IS NOT NULL
   OR clean_html IS NOT NULL
   OR content_markdown IS NOT NULL
   OR content_json IS NOT NULL
ON CONFLICT (article_pk) DO UPDATE SET
    url_token=EXCLUDED.url_token,
    clean_html=EXCLUDED.clean_html,
    content_markdown=EXCLUDED.content_markdown,
    content_json=EXCLUDED.content_json,
    updated_at=EXCLUDED.updated_at
"""


DROP_ARTICLE_LEGACY_COLUMNS = """
ALTER TABLE articles
DROP COLUMN IF EXISTS url_token,
DROP COLUMN IF EXISTS clean_html,
DROP COLUMN IF EXISTS content_markdown,
DROP COLUMN IF EXISTS content_json,
DROP COLUMN IF EXISTS cover_image_id
"""


CREATE_ARTICLES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_articles_biz_publish
ON articles (biz, publish_at DESC)
"""


CREATE_ARTICLE_IMAGES_TABLE = """
CREATE TABLE IF NOT EXISTS article_images (
    id SERIAL PRIMARY KEY,
    article_pk INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    orig_url TEXT,
    content_type TEXT,
    data BYTEA,
    failed_at TIMESTAMPTZ,
    failed_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (article_pk, orig_url)
)
"""


ALTER_ARTICLE_IMAGES_TABLE = """
ALTER TABLE article_images
ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS failed_reason TEXT
"""


CREATE_LOGIN_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS login_sessions (
    id SERIAL PRIMARY KEY,
    token TEXT NOT NULL,
    cookies_json TEXT NOT NULL,
    nickname TEXT,
    avatar TEXT,
    is_default BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
)
"""


UPSERT_SCHEMA_VERSION = """
INSERT INTO meta(key, value)
VALUES ('schema_version', %s)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
"""
