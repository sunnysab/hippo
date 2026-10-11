/** `mm:ss`, or `h:mm:ss` once there is an hour to show. */
export const formatDuration = (seconds: number | undefined | null): string => {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds)) return '';
  const total = Math.max(Math.floor(seconds), 0);
  const hours = Math.floor(total / 3600);
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const rest = String(total % 60).padStart(2, '0');
  return hours > 0 ? `${hours}:${minutes}:${rest}` : `${minutes}:${rest}`;
};
