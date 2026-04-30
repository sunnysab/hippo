import type { Dispatch, SetStateAction } from 'react';
import { useOutletContext } from 'react-router-dom';
import type { SyncSettingsFormState } from './form';

export interface SettingsRouteContextValue {
  formState: SyncSettingsFormState;
  setFormState: Dispatch<SetStateAction<SyncSettingsFormState>>;
}

export function useSettingsRouteContext() {
  return useOutletContext<SettingsRouteContextValue>();
}
