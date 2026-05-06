import { useI18n } from '../../i18n';
import type { ReaderConfig } from '../../hooks/useReaderSettings';

interface ReaderControlsProps {
  isOpen: boolean;
  controlsRef: React.RefObject<HTMLDivElement | null>;
  config: ReaderConfig;
  updateConfig: (patch: Partial<ReaderConfig>) => void;
}

export function ReaderControls({ isOpen, controlsRef, config, updateConfig }: ReaderControlsProps) {
  const { t } = useI18n();

  return (
    <div
      className={`reader-controls${isOpen ? ' is-open' : ''}`}
      id="reader-controls"
      ref={controlsRef}
    >
      <label>
        <span>{t('reader.width', 'Width')}</span>
        <input id="reader-width" type="range" min="400" max="1200" step="10" value={config.width} onChange={(e) => updateConfig({ width: e.target.value })} />
      </label>
      <label>
        <span>{t('reader.font', 'Font Size')}</span>
        <input id="reader-font" type="range" min="12" max="32" step="1" value={config.font} onChange={(e) => updateConfig({ font: e.target.value })} />
      </label>
      <label>
        <span>{t('reader.lineHeight', 'Line Height')}</span>
        <input id="reader-line" type="range" min="1.2" max="3.0" step="0.05" value={config.lineHeight} onChange={(e) => updateConfig({ lineHeight: e.target.value })} />
      </label>
      <label>
        <span>{t('reader.letter', 'Letter Spacing')}</span>
        <input id="reader-letter" type="range" min="0" max="4" step="0.1" value={config.letter} onChange={(e) => updateConfig({ letter: e.target.value })} />
      </label>
      <label className="switch">
        <input id="reader-serif" type="checkbox" checked={config.serif} onChange={(e) => updateConfig({ serif: e.target.checked })} />
        <span>{t('reader.serif', 'Serif Font')}</span>
      </label>
      <div className="reader-controls-section">
        <label className="switch">
          <input
            id="reader-hide-small-images"
            type="checkbox"
            checked={config.hideSmall}
            onChange={(event) => updateConfig({ hideSmall: event.target.checked })}
          />
          <span>{t('reader.hideSmallImages', 'Hide small images')}</span>
        </label>
        <p className="muted reader-controls-note">
          {t('reader.hideSmallImagesHint', 'Use local heuristics to reduce decorative or low-value images while reading.')}
        </p>
      </div>
    </div>
  );
}
