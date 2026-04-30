import { FilterSettingsPanel } from './FilterSettingsPanel';
import { ReaderFilterPanel } from './ReaderFilterPanel';
import { useSettingsRouteContext } from './settingsRouteContext';

export function SettingsFilterRoute() {
  const { formState, setFormState } = useSettingsRouteContext();

  return (
    <div className="settings-panel-grid settings-filter-grid">
      <FilterSettingsPanel formState={formState} setFormState={setFormState} />
      <ReaderFilterPanel />
    </div>
  );
}
