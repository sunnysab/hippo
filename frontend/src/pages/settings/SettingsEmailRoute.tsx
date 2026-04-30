import { EmailPanel } from './EmailPanel';
import { useSettingsRouteContext } from './settingsRouteContext';

export function SettingsEmailRoute() {
  const { formState, setFormState } = useSettingsRouteContext();

  return (
    <div className="settings-panel-grid settings-single-column">
      <EmailPanel formState={formState} setFormState={setFormState} />
    </div>
  );
}
