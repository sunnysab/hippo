import { apiGet } from '../../api';

/** Per-source state the console renders. Mirrors `GET /api/admin/source`. */
export interface SourceBacklog {
  pending: number;
  processing: number;
  failed: number;
  done: number;
}

export interface SourceOverview {
  source: string;
  /** Catalogue rows from this source. */
  channels: number;
  enabled_channels: number;
  /** Subscriptions across all users, so a catalogue nobody follows is visible. */
  followers: number;
  last_synced_at: string | null;
  /** `{field, configured}` for sources that take a credential, `null` otherwise. */
  credential: { field: string; configured: boolean } | null;
  /** The proxy in effect, already resolved against the instance default. */
  proxy: string;
  backlog: SourceBacklog | null;
}

export interface SourceOverviewPayload {
  sources: SourceOverview[];
  media_enabled: boolean;
}

/**
 * The source list comes from the backend, not a constant here: which sources
 * exist and which accept a credential are things the server decides.
 */
export const loadSourceOverview = async (): Promise<SourceOverviewPayload> => {
  const payload = await apiGet('/api/admin/source');
  return {
    sources: (payload.sources as SourceOverview[]) || [],
    media_enabled: Boolean(payload.media_enabled),
  };
};

export const pendingTotal = (backlog: SourceBacklog | null): number =>
  (backlog?.pending ?? 0) + (backlog?.processing ?? 0);
