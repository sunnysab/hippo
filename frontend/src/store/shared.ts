export interface Group {
  id: number;
  name: string;
  account_count: number;
  article_count?: number;
  sync_mode: string | null;
  sync_recent_days: number | null;
}

export interface Account {
  biz: string;
  nickname: string;
  alias: string | null;
  round_head_img: string;
  avatar_url: string;
  group_id: number;
  group_name?: string;
  is_disabled: boolean;
  last_synced_at: string | null;
  sync_mode: string | null;
  sync_recent_days: number | null;
  sync_interval_days: number | null;
  article_count: number;
}

export interface SyncModeDefaults {
  mode: string;
  recent_days: number;
}

export const syncDefaults: SyncModeDefaults = {
  mode: 'incremental',
  recent_days: 7,
};
