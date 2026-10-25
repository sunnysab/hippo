import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';

interface Topic {
  slug: string;
  name: string;
}

interface Vocabulary {
  version: number;
  topics: Topic[];
}

interface SiteSettings {
  registration_enabled: boolean;
  site_name: string;
  public_base_url: string;
  vocabulary: Vocabulary;
}

const EMPTY_VOCABULARY: Vocabulary = { version: 1, topics: [] };

const EMPTY: SiteSettings = {
  registration_enabled: false,
  site_name: 'Hippo',
  public_base_url: '',
  vocabulary: EMPTY_VOCABULARY,
};

/** Mirrors the server's rule: a slug is an ASCII identifier, the label is Chinese. */
const SLUG_PATTERN = /^[a-z][a-z0-9-]*$/;

const normalize = (payload: Record<string, unknown>): SiteSettings => ({
  registration_enabled: Boolean(payload.registration_enabled),
  site_name: String(payload.site_name ?? 'Hippo'),
  public_base_url: String(payload.public_base_url ?? ''),
  vocabulary: {
    version: Number((payload.vocabulary as Vocabulary | undefined)?.version ?? 1),
    topics: Array.isArray((payload.vocabulary as Vocabulary | undefined)?.topics)
      ? ((payload.vocabulary as Vocabulary).topics as Topic[])
      : [],
  },
});

export function AdminSitePage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [settings, setSettings] = useState<SiteSettings>(EMPTY);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const payload = await apiGet('/api/admin/site');
      setSettings(normalize(payload));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (updates: Partial<SiteSettings>) => {
    try {
      const payload = await apiSend('/api/admin/site', 'PATCH', updates);
      setSettings(normalize(payload));
      showToast(t('admin.site.saved', '站点设置已保存'));
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    }
  };

  const updateTopic = (index: number, patch: Partial<Topic>) => {
    const topics = settings.vocabulary.topics.map((topic, position) =>
      position === index ? { ...topic, ...patch } : topic,
    );
    setSettings({ ...settings, vocabulary: { ...settings.vocabulary, topics } });
  };

  const saveVocabulary = async (topics: Topic[] = settings.vocabulary.topics) => {
    // A half-typed row is not a vocabulary; the server would reject it and the
    // toast would blame the wrong thing. Wait until the row is complete.
    const cleaned = topics.filter((topic) => topic.slug.trim() || topic.name.trim());
    if (cleaned.some((topic) => !SLUG_PATTERN.test(topic.slug) || !topic.name.trim())) {
      showToast(t('admin.site.vocabularyInvalid', '标识只能是小写字母、数字和连字符，名称不能为空'));
      return;
    }
    await save({ vocabulary: { version: settings.vocabulary.version, topics: cleaned } });
  };

  if (loading) {
    return <div className="muted admin-empty">{t('admin.loading', '加载中…')}</div>;
  }

  return (
    <div className="admin-page">
      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.site.title', '站点设置')}</h2>
        </div>
        <div className="form-grid">
          <label className="auth-field">
            <span>{t('admin.site.name', '站点名称')}</span>
            <input
              className="input"
              value={settings.site_name}
              onChange={(event) => setSettings({ ...settings, site_name: event.target.value })}
              onBlur={() => void save({ site_name: settings.site_name })}
            />
          </label>
          <label className="auth-field">
            <span>{t('admin.site.baseUrl', 'Public Base URL')}</span>
            <input
              className="input"
              placeholder="https://hippo.example.com"
              value={settings.public_base_url}
              onChange={(event) => setSettings({ ...settings, public_base_url: event.target.value })}
              onBlur={() => void save({ public_base_url: settings.public_base_url })}
            />
          </label>
        </div>
        <p className="muted admin-note">
          {t(
            'admin.site.baseUrlHint',
            '用于拼接验证与重置邮件里的链接；留空时按请求 Host 推断。',
          )}
        </p>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.site.registration', '注册')}</h2>
        </div>
        <label className="auth-check">
          <input
            type="checkbox"
            checked={settings.registration_enabled}
            onChange={(event) => void save({ registration_enabled: event.target.checked })}
          />
          <span>{t('admin.site.registrationEnabled', '开放注册')}</span>
        </label>
        <p className="muted admin-note">
          {t(
            'admin.site.registrationHint',
            '关闭后新账号无法注册（接口返回 403），已有账号不受影响。',
          )}
        </p>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('admin.site.vocabulary', '方向词表')}</h2>
        </div>
        <p className="muted admin-note">
          {t(
            'admin.site.vocabularyHint',
            '文章分类只能从这些方向里选。改动后版本号会自动 +1，已有分类会在后台分批重算。',
          )}
          {' · '}
          {t('admin.site.vocabularyVersion', '当前版本')} {settings.vocabulary.version}
        </p>
        {settings.vocabulary.topics.map((topic, index) => (
          <div className="form-grid" key={index}>
            <label className="auth-field">
              <span>{t('admin.site.topicSlug', '标识')}</span>
              <input
                className="input"
                value={topic.slug}
                onChange={(event) => updateTopic(index, { slug: event.target.value })}
                onBlur={() => void saveVocabulary()}
              />
            </label>
            <label className="auth-field">
              <span>{t('admin.site.topicName', '名称')}</span>
              <input
                className="input"
                value={topic.name}
                onChange={(event) => updateTopic(index, { name: event.target.value })}
                onBlur={() => void saveVocabulary()}
              />
            </label>
            <button
              className="btn ghost"
              type="button"
              onClick={() => {
                setSettings({
                  ...settings,
                  vocabulary: {
                    ...settings.vocabulary,
                    topics: settings.vocabulary.topics.filter((_, position) => position !== index),
                  },
                });
                void saveVocabulary([
                  ...settings.vocabulary.topics.filter((_, position) => position !== index),
                ]);
              }}
            >
              {t('admin.site.topicRemove', '删除')}
            </button>
          </div>
        ))}
        <button
          className="btn"
          type="button"
          onClick={() => {
            const topics = [...settings.vocabulary.topics, { slug: '', name: '' }];
            setSettings({ ...settings, vocabulary: { ...settings.vocabulary, topics } });
          }}
        >
          {t('admin.site.topicAdd', '添加方向')}
        </button>
      </section>
    </div>
  );
}
