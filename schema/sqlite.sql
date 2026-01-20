CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
CREATE TABLE account_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE TABLE accounts (
                biz TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                alias TEXT,
                round_head_img TEXT,
                uin TEXT NOT NULL,
                key TEXT NOT NULL,
                pass_ticket TEXT NOT NULL,
                group_id INTEGER REFERENCES account_groups(id) ON DELETE SET NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                last_synced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE INDEX idx_accounts_group
                ON accounts (group_id)
                ;
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE login_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                cookies_json TEXT NOT NULL,
                nickname TEXT,
                avatar TEXT,
                is_default INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
CREATE TABLE IF NOT EXISTS "articles" (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    biz TEXT NOT NULL REFERENCES accounts(biz) ON DELETE CASCADE,
                    article_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT,
                    digest TEXT,
                    cover TEXT,
                    link TEXT NOT NULL,
                    source_url TEXT,
                    publish_at INTEGER,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (biz, article_id)
                );
CREATE INDEX idx_articles_biz_publish
                ON articles (biz, publish_at DESC)
                ;
