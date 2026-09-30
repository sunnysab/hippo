import { describe, expect, it } from 'vitest';
import { streamEvents, type StreamEvent } from './stream';

/** Build a Response whose body arrives in the given chunks. */
function responseWith(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200 });
}

async function collect(...chunks: string[]): Promise<StreamEvent[]> {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => responseWith(...chunks)) as typeof fetch;
  try {
    const events: StreamEvent[] = [];
    for await (const event of streamEvents('/api/chat/session/1/message', {})) {
      events.push(event);
    }
    return events;
  } finally {
    globalThis.fetch = originalFetch;
  }
}

describe('streamEvents', () => {
  it('decodes one event per record', async () => {
    const events = await collect(
      'data: {"delta":"he"}\n\n',
      'data: {"delta":"llo"}\n\n',
      'data: {"done":true,"message_id":3}\n\n',
    );

    expect(events.map((e) => e.delta ?? e.done)).toEqual(['he', 'llo', true]);
    expect(events.at(-1)?.message_id).toBe(3);
  });

  it('reassembles a record split across network reads', async () => {
    // The reader must not treat a chunk boundary as a record boundary.
    const events = await collect('data: {"del', 'ta":"split"}\n\n');

    expect(events).toHaveLength(1);
    expect(events[0].delta).toBe('split');
  });

  it('handles several records arriving in one chunk', async () => {
    const events = await collect('data: {"delta":"a"}\n\ndata: {"delta":"b"}\n\n');
    expect(events.map((e) => e.delta)).toEqual(['a', 'b']);
  });

  it('yields a trailing record that never got its blank line', async () => {
    const events = await collect('data: {"delta":"a"}\n\n', 'data: {"done":true}');
    expect(events.at(-1)?.done).toBe(true);
  });

  it('surfaces an error event instead of throwing', async () => {
    const events = await collect('data: {"error":"LLM 超时"}\n\n');
    expect(events[0].error).toBe('LLM 超时');
  });

  it('ignores malformed records without aborting the stream', async () => {
    const events = await collect('data: not json\n\n', 'data: {"delta":"ok"}\n\n');
    expect(events).toHaveLength(1);
    expect(events[0].delta).toBe('ok');
  });

  it('throws with the server message when the request fails', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ error: '会话不存在' }), { status: 404 })) as typeof fetch;
    try {
      const iterate = async () => {
        for await (const event of streamEvents('/x', {})) {
          throw new Error(`unexpected event: ${JSON.stringify(event)}`);
        }
      };
      await expect(iterate()).rejects.toThrow('会话不存在');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
