import type { Dispatch, SetStateAction } from 'react';
import { useSettingsState, type SyncSettings } from '../../store/settings';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { apiSend, isAuthError } from '../../api';
import { buildFilterSettingsPayload, type SyncSettingsFormState } from './form';

interface FilterSettingsPanelProps {
  formState: SyncSettingsFormState;
  setFormState: Dispatch<SetStateAction<SyncSettingsFormState>>;
}

export function FilterSettingsPanel({
  formState,
  setFormState,
}: FilterSettingsPanelProps) {
  const { dispatch } = useSettingsState();
  const { t } = useI18n();
  const { showToast } = useToast();

  const handleSave = async () => {
    try {
      const payload = await apiSend('/api/settings', 'PATCH', buildFilterSettingsPayload(formState));
      dispatch({ type: 'SET_SYNC_SETTINGS', payload: payload as unknown as SyncSettings });
      showToast(t('sync.filterSaved', 'Filter settings saved.'));
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('sync.filterSaveFailed', 'Failed to save filter settings.'));
    }
  };

  return (
    <div className="panel sync-filter-settings">
      <div className="panel-header">
        <div>
          <h2>{t('sync.filterTitle', 'Article Filters')}</h2>
          <p className="muted">{t('sync.filterSubtitle', 'Skip matched articles before they enter the synced library.')}</p>
        </div>
        <div className="toolbar">
          <button className="btn" id="btn-sync-filter-save" type="button" onClick={handleSave}>
            {t('sync.save', 'Save')}
          </button>
        </div>
      </div>
      <div className="sync-form-sections">
        <section className="sync-form-section">
          <div className="sync-section-title">{t('sync.sectionFilter', 'Filter')}</div>
          <div className="form-grid">
            <label className="sync-textarea-field">
              <span>{t('sync.articleExcludeKeywords', 'Exclude article keywords')}</span>
              <textarea
                id="sync-article-exclude-keywords"
                rows={5}
                placeholder={'promo\nad'}
                value={formState.articleExcludeKeywords}
                onChange={(event) => setFormState((prev) => ({ ...prev, articleExcludeKeywords: event.target.value }))}
              ></textarea>
              <small className="muted">{t('sync.articleExcludeKeywordsHint', 'One keyword per line, or separate by comma / semicolon.')}</small>
            </label>
          </div>
        </section>
      </div>
    </div>
  );
}
