import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { ProxyFields } from './ProxyFields';

interface MediaSettings {
  enabled: boolean;
  asr_tier: string;
  asr_endpoints: Record<string, string>;
  asr_api_key: string;
  asr_api_key_set: boolean;
  asr_language: string;
  asr_timeout_seconds: number;
  asr_max_new_tokens: number;
  chunk_seconds: number;
  min_tail_seconds: number;
  max_chunk_seconds: number;
  items_per_sync: number;
  max_duration_seconds: number;
  transcribe_max_attempts: number;
  keep_media: boolean;
  keep_media_bitrate: string;
  bilibili_cc_subtitle: boolean;
  bilibili_cookie: string;
  bilibili_cookie_set: boolean;
  youtube_cookies: string;
  proxy_default: string;
  proxy_bilibili: string;
  proxy_youtube: string;
  proxy_podcast: string;
}

interface ProbeResult {
  tier: string;
  ok: boolean;
  detail: string;
}

const EMPTY: MediaSettings = {
  enabled: true,
  asr_tier: 'accurate',
  asr_endpoints: { accurate: '', fast: '' },
  asr_api_key: '',
  asr_api_key_set: false,
  asr_language: 'zh',
  asr_timeout_seconds: 900,
  asr_max_new_tokens: 2048,
  chunk_seconds: 300,
  min_tail_seconds: 60,
  max_chunk_seconds: 500,
  items_per_sync: 5,
  max_duration_seconds: 14400,
  transcribe_max_attempts: 3,
  keep_media: true,
  keep_media_bitrate: '128k',
  bilibili_cc_subtitle: true,
  bilibili_cookie: '',
  bilibili_cookie_set: false,
  youtube_cookies: '',
  proxy_default: '',
  proxy_bilibili: '',
  proxy_youtube: '',
  proxy_podcast: '',
};

const BITRATES = ['original', '64k', '96k', '128k', '192k', '256k'];
const TIERS = ['accurate', 'fast'];

/** Normalise the payload so a missing key from the API cannot blank a field. */
const fromPayload = (payload: Record<string, unknown>): MediaSettings => ({
  ...EMPTY,
  ...payload,
  asr_endpoints: {
    accurate: String((payload.asr_endpoints as Record<string, string> | undefined)?.accurate ?? ''),
    fast: String((payload.asr_endpoints as Record<string, string> | undefined)?.fast ?? ''),
  },
}) as MediaSettings;

export function AdminMediaPage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [settings, setSettings] = useState<MediaSettings>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [newApiKey, setNewApiKey] = useState('');
  const [newCookie, setNewCookie] = useState('');
  const [probes, setProbes] = useState<ProbeResult[] | null>(null);
  const [testing, setTesting] = useState(false);

  const load = useCallback(async () => {
    try {
      const payload = await apiGet('/api/admin/media');
      setSettings(fromPayload(payload as Record<string, unknown>));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (updates: Record<string, unknown>) => {
    try {
      const payload = await apiSend('/api/admin/media', 'PATCH', updates);
      setSettings(fromPayload(payload as Record<string, unknown>));
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
      void load();
    }
  };

  const saveNumber = (key: keyof MediaSettings, raw: string) => {
    const parsed = Number.parseInt(raw, 10);
    if (Number.isNaN(parsed) || parsed === settings[key]) {
      return;
    }
    void save({ [key]: parsed });
  };

  const runTest = async () => {
    setTesting(true);
    try {
      const payload = await apiSend('/api/admin/media/test', 'POST', {});
      setProbes((payload.asr as ProbeResult[]) || []);
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    } finally {
      setTesting(false);
    }
  };

  if (loading) {
    return <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>;
  }

  const apiKeyPlaceholder = settings.asr_api_key_set
    ? settings.asr_api_key || t('admin.media.secretSet', '已配置（留空则不改）')
    : t('admin.media.secretUnset', '未配置');

  return (
    <div className="admin-page">
      <section className="panel">
        <div className="panel-header">
          <div>
            <h2 className="panel-title">{t('admin.media.asr', '转写服务')}</h2>
            <p className="muted">
              {t(
                'admin.media.asrHint',
                'OpenAI 兼容的 /v1/audio/transcriptions。accurate 档更准但慢约 15 倍，fast 档用于批量粗转。',
              )}
            </p>
          </div>
          <div className="toolbar">
            <button className="btn ghost" type="button" disabled={testing} onClick={() => void runTest()}>
              {testing ? t('admin.media.testing', '测试中…') : t('admin.media.test', '测试连接')}
            </button>
          </div>
        </div>

        <label className="auth-check">
          <input
            type="checkbox"
            checked={settings.enabled}
            onChange={(event) => void save({ enabled: event.target.checked })}
          />
          <span>{t('admin.media.enabled', '启用媒体来源与转写')}</span>
        </label>

        <div className="form-grid">
          <label className="auth-field">
            <span>{t('admin.media.tier', '默认档位')}</span>
            <select
              className="input"
              value={settings.asr_tier}
              onChange={(event) => void save({ asr_tier: event.target.value })}
            >
              {TIERS.map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </label>
        </div>

        <div className="form-grid">
          {TIERS.map((tier) => (
            <label className="auth-field" key={tier}>
              <span>{t('admin.media.endpoint', '端点')} · {tier}</span>
              <input
                className="input"
                placeholder="http://host:8317/v1"
                defaultValue={settings.asr_endpoints[tier] || ''}
                onBlur={(event) => void save({ asr_endpoints: { [tier]: event.target.value.trim() } })}
              />
            </label>
          ))}
        </div>

        <div className="form-grid">
          <label className="auth-field">
            <span>{t('admin.media.apiKey', 'API Key')}</span>
            <input
              className="input"
              type="password"
              value={newApiKey}
              placeholder={apiKeyPlaceholder}
              onChange={(event) => setNewApiKey(event.target.value)}
              onBlur={() => {
                if (newApiKey) {
                  void save({ asr_api_key: newApiKey });
                  setNewApiKey('');
                }
              }}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.media.language', '语言')}</span>
            <input
              className="input"
              defaultValue={settings.asr_language}
              onBlur={(event) => void save({ asr_language: event.target.value.trim() })}
            />
          </label>
        </div>
        {settings.asr_api_key_set ? (
          <button
            className="btn ghost"
            type="button"
            onClick={() => void save({ asr_api_key: '' })}
          >
            {t('admin.media.clearApiKey', '清除 API Key')}
          </button>
        ) : null}

        {probes ? (
          <ul className="admin-probe-list">
            {probes.map((probe) => (
              <li key={probe.tier} className={probe.ok ? 'is-ok' : 'is-failed'}>
                <strong>{probe.tier}</strong>
                <span>{probe.detail}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.media.queue', '队列与归档')}</h2>
        </div>
        <div className="form-grid">
          <label className="auth-field">
            <span>{t('admin.media.itemsPerSync', '每次同步新入队条目')}</span>
            <input
              className="input"
              type="number"
              defaultValue={settings.items_per_sync}
              onBlur={(event) => saveNumber('items_per_sync', event.target.value)}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.media.chunkSeconds', '切片长度（秒）')}</span>
            <input
              className="input"
              type="number"
              defaultValue={settings.chunk_seconds}
              onBlur={(event) => saveNumber('chunk_seconds', event.target.value)}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.media.maxDuration', 'Max media duration (seconds)')}</span>
            <input
              className="input"
              type="number"
              defaultValue={settings.max_duration_seconds}
              onBlur={(event) => saveNumber('max_duration_seconds', event.target.value)}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.media.bitrate', 'Archived audio bitrate')}</span>
            <select
              className="input"
              value={settings.keep_media_bitrate}
              onChange={(event) => void save({ keep_media_bitrate: event.target.value })}
            >
              {BITRATES.map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </label>
        </div>
        <label className="auth-check">
          <input
            type="checkbox"
            checked={settings.keep_media}
            onChange={(event) => void save({ keep_media: event.target.checked })}
          />
          <span>{t('admin.media.keepMedia', 'Archive audio and video for playback')}</span>
        </label>
        <p className="muted admin-note">
          {t(
            'admin.media.keepMediaHint',
            'Audio is transcoded at the bitrate above; video is copied as-is up to 720p. Each download is capped at 2 GiB and by the media duration setting above, so longer entries are not archived. Existing entries are not backfilled automatically; retry a single article from its page.',
          )}
        </p>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.media.credentials', '来源凭证与代理')}</h2>
        </div>
        <p className="muted admin-note">
          {t(
            'admin.media.credentialsHint',
            'B站投稿列表与 CC 字幕都需要 SESSDATA；YouTube 必须走代理，B站不能走代理。',
          )}
        </p>
        <div className="form-grid">
          <label className="auth-field">
            <span>{t('admin.media.bilibiliCookie', 'B站 Cookie')}</span>
            <input
              className="input"
              type="password"
              value={newCookie}
              placeholder={settings.bilibili_cookie_set
                ? settings.bilibili_cookie || t('admin.media.secretSet', '已配置（留空则不改）')
                : t('admin.media.secretUnset', '未配置')}
              onChange={(event) => setNewCookie(event.target.value)}
              onBlur={() => {
                if (newCookie) {
                  void save({ bilibili_cookie: newCookie });
                  setNewCookie('');
                }
              }}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.media.youtubeCookies', 'YouTube cookies（Netscape cookies.txt 内容）')}</span>
            <textarea
              className="input"
              rows={4}
              spellCheck={false}
              placeholder={t('admin.media.youtubeCookiesPlaceholder', '留空则不使用登录态；提交后不会回显')}
              defaultValue={settings.youtube_cookies}
              onBlur={(event) => void save({ youtube_cookies: event.target.value })}
            />
          </label>
          <ProxyFields
            values={{
              proxy_default: settings.proxy_default,
              proxy_bilibili: settings.proxy_bilibili,
              proxy_youtube: settings.proxy_youtube,
              proxy_podcast: settings.proxy_podcast,
            }}
            onSave={save}
          />
        </div>
        <label className="auth-check">
          <input
            type="checkbox"
            checked={settings.bilibili_cc_subtitle}
            onChange={(event) => void save({ bilibili_cc_subtitle: event.target.checked })}
          />
          <span>{t('admin.media.ccSubtitle', '有 CC 字幕时优先用它，不做转写')}</span>
        </label>
      </section>
    </div>
  );
}
