import { useState } from 'react';
import { useI18n } from '../../i18n';
import { getSourceLabel } from '../../utils/source';

/** Follow the instance default, connect directly, or use a proxy of one's own. */
const SOURCES = ['weixin', 'bilibili', 'youtube', 'podcast'] as const;

type Mode = 'default' | 'direct' | 'custom';

interface ProxyFieldsProps {
  /** `proxy_default` plus one `proxy_<source>` per source, as the API stores them. */
  values: Record<string, string>;
  onSave: (patch: Record<string, string>) => Promise<void>;
}

/** `''`/`default` are "follow the default", `direct` is a direct connection. */
const modeOf = (value: string): Mode => {
  if (value === 'direct') return 'direct';
  return value && value !== 'default' ? 'custom' : 'default';
};

const without = <T,>(record: Record<string, T>, key: string): Record<string, T> => {
  const rest = { ...record };
  delete rest[key];
  return rest;
};

/**
 * Proxy settings, one decision per source.
 *
 * Bilibili has to stay direct (its exit IP is blacklisted) while YouTube has to be
 * proxied, and a podcast feed is somewhere in between — so a single instance-wide
 * proxy is not enough, and neither is a blank field meaning two things at once.
 */
export function ProxyFields({ values, onSave }: ProxyFieldsProps) {
  const { t } = useI18n();
  // 正在输入的文本；pending 记录「已选了自定义，但值还没输入完」。
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});

  const valueOf = (key: string) => drafts[key] ?? values[key] ?? '';
  const modeOfKey = (key: string) => (pending[key] ? 'custom' : modeOf(valueOf(key)));

  const forget = (key: string) => {
    setDrafts((previous) => without(previous, key));
    setPending((previous) => without(previous, key));
  };

  const commit = async (key: string) => {
    const next = valueOf(key).trim();
    if (next === (values[key] ?? '')) {
      forget(key);
      return;
    }
    await onSave({ [key]: next });
    forget(key);
  };

  const setMode = (source: string, mode: Mode) => {
    const key = `proxy_${source}`;
    if (mode === 'custom') {
      // 输入框立刻出现，但值要等失焦才写：半截的「http://」不该进库。
      setPending((previous) => ({ ...previous, [key]: true }));
      return;
    }
    setPending((previous) => ({ ...previous, [key]: false }));
    setDrafts((previous) => ({ ...previous, [key]: mode }));
    void onSave({ [key]: mode }).then(() => forget(key));
  };

  const urlField = (key: string, placeholder: string) => (
    <input
      className="input"
      id={`${key}-url`}
      value={valueOf(key)}
      placeholder={placeholder}
      onChange={(event) => setDrafts((previous) => ({ ...previous, [key]: event.target.value }))}
      onBlur={() => void commit(key)}
    />
  );

  const modeSelect = (source: string, key: string) => (
    <select
      className="input"
      id={key}
      value={modeOfKey(key)}
      onChange={(event) => setMode(source, event.target.value as Mode)}
    >
      <option value="default">{t('admin.media.proxyFollow', '沿用默认代理')}</option>
      <option value="direct">{t('admin.media.proxyDirect', '直连（不走代理）')}</option>
      <option value="custom">{t('admin.media.proxyCustom', '自定义代理')}</option>
    </select>
  );

  return (
    <>
      <label className="auth-field">
        <span>{t('admin.media.proxyDefault', '默认代理')}</span>
        <input
          className="input"
          id="proxy_default"
          value={valueOf('proxy_default')}
          placeholder={t('admin.media.proxyDefaultPlaceholder', 'http://host:port，留空则默认直连')}
          onChange={(event) => setDrafts((previous) => ({ ...previous, proxy_default: event.target.value }))}
          onBlur={() => void commit('proxy_default')}
        />
      </label>
      {SOURCES.map((source) => {
        const key = `proxy_${source}`;
        const custom = modeOfKey(key) === 'custom';
        return (
          <label className="auth-field" key={source}>
            <span>{`${getSourceLabel(source, t)} · ${t('admin.media.proxy', '代理')}`}</span>
            <div className="proxy-field">
              {modeSelect(source, key)}
              {custom ? urlField(key, 'http://host:port') : null}
            </div>
          </label>
        );
      })}
    </>
  );
}
