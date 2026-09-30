CREATE EXTENSION IF NOT EXISTS pg_jieba;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_groups (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
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
    gh_id TEXT,
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
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS gh_id TEXT;

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
        -- 只认派生内容：原始 HTML 已搬到 article_document
        SELECT COALESCE(c.content_markdown, '')
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
        COALESCE(NEW.content_markdown, '')
    )
    WHERE id = NEW.article_pk;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_article_content_search_vector ON article_content;

CREATE TRIGGER trg_article_content_search_vector
AFTER INSERT OR UPDATE OF content_markdown
ON article_content
FOR EACH ROW EXECUTE FUNCTION article_content_search_vector_trigger();

UPDATE articles a
SET search_vector = build_article_search_vector(
    a.title,
    a.author,
    a.digest,
    COALESCE(c.content_markdown, '')
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
    attempts INTEGER NOT NULL DEFAULT 0,
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
    error_type TEXT,
    retryable BOOLEAN NOT NULL DEFAULT TRUE,
    last_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (biz, article_id)
);

CREATE INDEX IF NOT EXISTS idx_article_download_attempts_biz_article
ON article_download_attempts (biz, article_id);

ALTER TABLE article_download_attempts
    ADD COLUMN IF NOT EXISTS error_type TEXT,
    ADD COLUMN IF NOT EXISTS retryable BOOLEAN NOT NULL DEFAULT TRUE;

-- 原始文档存储：正文原文（content_noencode / 整页网页 HTML）与原始响应 JSON。
-- 线上正文表 article_content 只保留派生内容（content_markdown + content_json），
-- 原始数据放这里，解析逻辑出问题时可以从原文重放。
CREATE TABLE IF NOT EXISTS article_document (
    article_pk INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    url_token TEXT,
    raw_html TEXT,
    raw_json JSONB,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_article_document_source
ON article_document (source);

-- 列表阶段的待抓正文队列：列表只给长链（会失效），所以先入队，
-- 等详情（short_link）回来后才建 articles 行，避免长链落进 articles.link。
CREATE TABLE IF NOT EXISTS article_queue (
    id BIGSERIAL PRIMARY KEY,
    biz TEXT NOT NULL,
    sn TEXT NOT NULL,
    appmsg_id TEXT,
    long_link TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    retryable BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (biz, sn)
);

CREATE INDEX IF NOT EXISTS idx_article_queue_state
ON article_queue (state, id);

-- ============================================================
-- 多用户：账号与会话
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    is_disabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- 邮箱大小写不敏感；允许多个 NULL（管理员可由 CLI 创建但尚未绑定邮箱）
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
ON users (lower(email)) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS user_session (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_agent TEXT,
    ip TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_session_user
ON user_session (user_id);

-- 一份实体同时服务邮箱验证与密码重置
CREATE TABLE IF NOT EXISTS user_token (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_token_hash
ON user_token (token_hash);

CREATE INDEX IF NOT EXISTS idx_user_token_user
ON user_token (user_id, kind);

-- ============================================================
-- 多用户：订阅（关注谁、放哪个分组、是否停用都是 per-user 的）
-- ============================================================

ALTER TABLE account_groups ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_account_groups_user
ON account_groups (user_id);

CREATE TABLE IF NOT EXISTS subscription (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    biz TEXT NOT NULL REFERENCES accounts(biz) ON DELETE CASCADE,
    group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL,
    is_disabled BOOLEAN NOT NULL DEFAULT FALSE,
    sync_interval_days INTEGER,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, biz)
);

CREATE INDEX IF NOT EXISTS idx_subscription_biz
ON subscription (biz);

-- ============================================================
-- 多用户：阅读状态
-- 刻意不加 FK 到 articles（153 万行），加 FK 会全表扫描验证并持锁阻塞写入
-- ============================================================

CREATE TABLE IF NOT EXISTS article_read (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_pk INTEGER NOT NULL,
    read_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, article_pk)
);

-- ============================================================
-- 划线（quote + prefix/suffix 锚定，不用 offset）
-- ============================================================

CREATE TABLE IF NOT EXISTS annotation (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_pk INTEGER NOT NULL,
    quote TEXT NOT NULL,
    prefix TEXT NOT NULL DEFAULT '',
    suffix TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT 'default',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotation_user_article
ON annotation (user_id, article_pk);

-- ============================================================
-- LLM provider（管理员在面板里维护；必须先于 chat_session 定义）
-- ============================================================

CREATE TABLE IF NOT EXISTS llm_provider (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    api_key TEXT NOT NULL,
    model TEXT NOT NULL,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- ============================================================
-- AI 会话：article_pk 为空即自由聊天，非空即对某篇文章的解读
-- ============================================================

CREATE TABLE IF NOT EXISTS chat_session (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_pk INTEGER,
    provider_id INTEGER REFERENCES llm_provider(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_session_user
ON chat_session (user_id, updated_at DESC);

-- 一篇文章只对应一个解读会话（自由聊天的 article_pk 为 NULL，不受约束）
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_session_article
ON chat_session (user_id, article_pk) WHERE article_pk IS NOT NULL;

CREATE TABLE IF NOT EXISTS chat_message (
    id BIGSERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_message_session
ON chat_message (session_id, id);

-- ============================================================
-- 日报
-- ============================================================

CREATE TABLE IF NOT EXISTS report_setting (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    send_hour INTEGER NOT NULL DEFAULT 8,
    recipients TEXT[] NOT NULL DEFAULT '{}',
    group_ids INTEGER[] NOT NULL DEFAULT '{}',
    include_read BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS report_delivery (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_date DATE NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    -- 幂等：同一天同一渠道只投递一次，重启不会重发
    UNIQUE (user_id, report_date, channel)
);

-- ============================================================
-- 审计日志：谁改了什么。运行日志走 OTLP，不入库
-- ============================================================

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail JSONB,
    ip TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created
ON audit_log (created_at DESC);

-- 遗留表清理。avatar_images 仍在使用（微信读书时代的账号头像已被它取代），不要动。
DROP TABLE IF EXISTS test_alter;
DROP TABLE IF EXISTS login_sessions;
DROP TABLE IF EXISTS account_images;

-- 分组名只在同一个用户内唯一：两个用户可以各有自己的「技术」分组。
-- 旧库上该列带有全局唯一约束，先摘掉；NULL user_id（尚未归属的遗留分组）不参与唯一性。
ALTER TABLE account_groups DROP CONSTRAINT IF EXISTS account_groups_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_account_groups_user_name
ON account_groups (user_id, name);

-- 每个用户自己的阅读偏好（过滤词等）。全局同步节奏、SMTP、站点设置仍留在 meta，
-- 由管理员维护。
ALTER TABLE users ADD COLUMN IF NOT EXISTS preferences JSONB NOT NULL DEFAULT '{}'::jsonb;
