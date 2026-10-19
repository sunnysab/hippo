import { FilterSettingsPanel } from './FilterSettingsPanel';
import { ReaderFilterPanel } from './ReaderFilterPanel';

export function SettingsFilterRoute() {
  return (
    <div className="settings-panel-grid settings-filter-grid">
      <FilterSettingsPanel />
      <ReaderFilterPanel />
    </div>
  );
}
