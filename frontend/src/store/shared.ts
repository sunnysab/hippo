export interface Group {
  id: number;
  name: string;
  account_count: number;
  subscription_count?: number;
  disabled_count?: number;
  article_count?: number;
}

export interface AccountSync {
  history: { status: string; task_id: string | null; error: string | null };
  articles: { pending: number; processing: number; failed: number };
  images?: { pending: number; failed: number };
  listing: { task_id: string; status: string; kind: string } | null;
  last_checked_at: string | null;
}

export interface AccountSyncFailure {
  id: number;
  article_pk: number | null;
  article_id: string;
  title: string | null;
  link: string | null;
  error: string | null;
  attempts: number;
  updated_at: string | null;
}

export interface AccountSyncFailures {
  articles: AccountSyncFailure[];
  images: AccountSyncFailure[];
}

export interface Account {
  biz: string;
  source: string;
  nickname: string;
  alias: string | null;
  round_head_img: string;
  avatar_url: string;
  group_id: number;
  group_name?: string;
  is_disabled: boolean;
  last_synced_at: string | null;
  latest_publish_at: string | null;
  sync_interval_days: number | null;
  article_count: number;
  backfill_pending: boolean;
  backfill_running: boolean;
  sync?: AccountSync | null;
}
