/** `mm:ss`, or `h:mm:ss` once there is an hour to show. */
export const formatDuration = (seconds: number | undefined | null): string => {
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds)) return '';
  const total = Math.max(Math.floor(seconds), 0);
  const hours = Math.floor(total / 3600);
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
  const rest = String(total % 60).padStart(2, '0');
  return hours > 0 ? `${hours}:${minutes}:${rest}` : `${minutes}:${rest}`;
};

/**
 * Where to open a source video at a timestamp.
 *
 * Bilibili and YouTube both read `?t=<seconds>` on the video page, which is the
 * only offset a reader can follow when the player is a third-party iframe: the
 * transcript anchors of a video entry without a kept audio copy link here.
 */
export const timestampHref = (url: string, seconds: number): string | null => {
  const target = (url || '').trim();
  if (!target || seconds <= 0) return null;
  return `${target}${target.includes('?') ? '&' : '?'}t=${Math.floor(seconds)}`;
};
