import { useCallback, useEffect, useRef, useState } from 'react';
import { apiGet, apiSend, isAuthError } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { onRefresh } from '../../utils/events';
import { buildFilterSettingsPayload } from './form';

interface PreferencesPayload {
  article_exclude_keywords?: string;
}

/**
 * The caller's personal reading filter.
 *
 * It deliberately owns its state instead of reading the shared sync-settings
 * form: the sync knobs are instance-wide and admin-only, while these keywords
 * only narrow this reader's own list. Saving here must not dispatch a global
 * settings change, because the library is the same list before and after.
 */
export function FilterSettingsPanel() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [keywords, setKeywords] = useState('');
  const [loadFailed, setLoadFailed] = useState(false);
  // Once the reader edits, a refresh (or a slow first load) must not replace the draft.
  const dirtyRef = useRef(false);
  // A per-read sequence number: a read that is no longer the newest never lands.
  const requestIdRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    try {
      const payload = await apiGet('/api/settings/preferences') as unknown as PreferencesPayload;
      if (requestId === requestIdRef.current && !dirtyRef.current) {
        setKeywords(String(payload.article_exclude_keywords ?? ''));
        setLoadFailed(false);
      }
    } catch {
      if (requestId === requestIdRef.current && !dirtyRef.current) setLoadFailed(true);
    }
  }, []);

  useEffect(() => {
    // The read updates state after its await, never inside the render pass.
    void (async () => {
      await load();
    })();
  }, [load]);

  useEffect(() => {
    const handler = () => {
      if (dirtyRef.current) return;
      void load();
    };
    return onRefresh(handler);
  }, [load]);

  const handleSave = async () => {
    try {
      const payload = await apiSend(
        '/api/settings/preferences',
        'PATCH',
        buildFilterSettingsPayload({ articleExcludeKeywords: keywords }),
      ) as unknown as PreferencesPayload;
      setKeywords(String(payload.article_exclude_keywords ?? ''));
      dirtyRef.current = false;
      showToast(t('sync.filterSaved', 'Reading filters saved.'));
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('sync.filterSaveFailed', 'Failed to save reading filters.'));
    }
  };

  return (
    <div className="panel sync-filter-settings">
      <div className="panel-header">
        <div>
          <h2>{t('sync.filterTitle', 'Reading Filters')}</h2>
          <p className="muted">{t('sync.filterSubtitle', 'Narrows your own article list; the shared sync and library are unaffected.')}</p>
        </div>
        <div className="toolbar">
          <button className="btn" id="btn-sync-filter-save" type="button" onClick={handleSave}>
            {t('sync.save', 'Save')}
          </button>
        </div>
      </div>
      <div className="form-grid">
        <label className="sync-textarea-field">
          <span>{t('sync.articleExcludeKeywords', 'Exclude article keywords')}</span>
          <textarea
            id="sync-article-exclude-keywords"
            rows={5}
            placeholder={'promo\nad'}
            value={keywords}
            onChange={(event) => {
              dirtyRef.current = true;
              setKeywords(event.target.value);
            }}
          ></textarea>
          <small className="muted">{t('sync.articleExcludeKeywordsHint', 'One keyword per line, or separate by comma / semicolon.')}</small>
          {loadFailed ? (
            <small className="muted" id="sync-filter-load-failed">
              {t('sync.filterLoadFailed', 'Failed to load your reading filters.')}
            </small>
          ) : null}
        </label>
      </div>
    </div>
  );
}
