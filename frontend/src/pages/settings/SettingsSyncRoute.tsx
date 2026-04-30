import { ActiveTaskPanel } from './ActiveTaskPanel';
import { FailureAlertPanel } from './FailureAlertPanel';
import { SyncHistoryPanel } from './SyncHistoryPanel';
import { SyncSettingsPanel } from './SyncSettingsPanel';
import { useSettingsRouteContext } from './settingsRouteContext';

export function SettingsSyncRoute() {
  const { formState, setFormState } = useSettingsRouteContext();

  return (
    <div className="settings-panel-grid settings-sync-grid">
      <SyncSettingsPanel formState={formState} setFormState={setFormState} />
      <FailureAlertPanel formState={formState} setFormState={setFormState} />
      <ActiveTaskPanel />
      <SyncHistoryPanel />
    </div>
  );
}
