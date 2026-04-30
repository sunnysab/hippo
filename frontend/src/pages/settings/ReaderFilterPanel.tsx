import { useI18n } from '../../i18n';
import { useReaderSettings } from '../../hooks/useReaderSettings';

export function ReaderFilterPanel() {
  const { t } = useI18n();
  const { config, updateConfig } = useReaderSettings();

  return (
    <div className="panel reader-filter-settings">
      <div className="panel-header">
        <div>
          <h2>{t('reader.filterTitle', 'Reader Filters')}</h2>
          <p className="muted">{t('reader.filterSubtitle', 'Local reading preferences applied in article preview.')}</p>
        </div>
      </div>
      <div className="sync-form-sections">
        <section className="sync-form-section">
          <div className="sync-section-title">{t('reader.filterSection', 'Images')}</div>
          <div className="form-grid">
            <label className="switch">
              <span>{t('reader.hideSmallImages', 'Hide small images')}</span>
              <input
                id="reader-hide-small-images"
                type="checkbox"
                checked={config.hideSmall}
                onChange={(event) => updateConfig({ hideSmall: event.target.checked })}
              />
            </label>
            <p className="muted sync-inline-note">{t('reader.hideSmallImagesHint', 'Use local heuristics to reduce decorative or low-value images while reading.')}</p>
          </div>
        </section>
      </div>
    </div>
  );
}
