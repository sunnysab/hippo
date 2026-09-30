/**
 * Server-sent-events reader.
 *
 * Uses `fetch` rather than `EventSource` for two reasons: EventSource cannot
 * POST or carry the session cookie's request shape, and it silently reconnects
 * — which for a chat answer means duplicating a turn.
 */

export interface StreamEvent {
  delta?: string;
  done?: boolean;
  error?: string;
  truncated?: boolean;
  cached?: boolean;
  message_id?: number;
}

/**
 * POST a body and yield decoded events as they arrive.
 *
 * Chunks are split on the SSE record separator, not on network boundaries: a
 * single `data:` line routinely spans several reads.
 */
export async function* streamEvents(
  url: string,
  body: Record<string, unknown>,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as Record<string, string>;
    throw new Error(payload.error || `请求失败: ${response.status}`);
  }
  if (!response.body) {
    throw new Error('响应没有内容');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const record = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseRecord(record);
        if (event) yield event;
        boundary = buffer.indexOf('\n\n');
      }
    }
    // A final record may arrive without its trailing blank line.
    const tail = parseRecord(buffer);
    if (tail) yield tail;
  } finally {
    reader.cancel().catch(() => {});
  }
}

function parseRecord(record: string): StreamEvent | null {
  const line = record
    .split('\n')
    .find((item) => item.startsWith('data:'));
  if (!line) return null;
  const raw = line.slice(5).trim();
  if (!raw) return null;
  try {
    return JSON.parse(raw) as StreamEvent;
  } catch {
    return null;
  }
}
