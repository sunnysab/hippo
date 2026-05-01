const REFRESH_EVENT = 'hippo:refresh';
const TOAST_EVENT = 'hippo:toast';

export const emitRefresh = () => {
  window.dispatchEvent(new Event(REFRESH_EVENT));
};

export const onRefresh = (listener: () => void) => {
  window.addEventListener(REFRESH_EVENT, listener);
  return () => window.removeEventListener(REFRESH_EVENT, listener);
};

export const emitToast = (message: string) => {
  window.dispatchEvent(new CustomEvent<string>(TOAST_EVENT, { detail: message }));
};

export const onToast = (listener: (message: string) => void) => {
  const handler = (event: Event) => {
    listener((event as CustomEvent<string>).detail);
  };
  window.addEventListener(TOAST_EVENT, handler);
  return () => window.removeEventListener(TOAST_EVENT, handler);
};
