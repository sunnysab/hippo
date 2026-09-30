import { useCallback, useEffect, useRef, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { streamEvents } from './stream';

interface ChatMessage {
  id: number;
  role: string;
  content: string;
  preset?: string | null;
}

interface ReadingSidebarProps {
  articleId: number | null;
  open: boolean;
  onToggle: () => void;
}

type Busy = '' | 'summary' | 'points' | 'ask';

/**
 * Reading companion for the open article: summary, key points and free-form
 * questions all share one session, so a question can refer to the summary.
 */
export function ReadingSidebar({ articleId, open, onToggle }: ReadingSidebarProps) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [busy, setBusy] = useState<Busy>('');
  const [streaming, setStreaming] = useState('');
  const [question, setQuestion] = useState('');
  const [truncated, setTruncated] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // One session per article: the server returns the existing one when the
  // article already has a conversation, so reopening resumes where it left off.
  const ensureSession = useCallback(async (): Promise<number | null> => {
    if (!articleId) return null;
    if (sessionId) return sessionId;
    const created = (await apiSend('/api/chat/session', 'POST', {
      article_id: articleId,
    })) as unknown as { id: number };
    setSessionId(created.id);
    return created.id;
  }, [articleId, sessionId]);

  const loadHistory = useCallback(async (id: number) => {
    try {
      const payload = await apiGet(`/api/chat/session/${id}`, { silent: true });
      setMessages((payload.messages as ChatMessage[]) ?? []);
    } catch {
      setMessages([]);
    }
  }, []);

  useEffect(() => {
    if (!open || !articleId) return;
    void (async () => {
      try {
        const id = await ensureSession();
        if (id) await loadHistory(id);
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      }
    })();
  }, [articleId, ensureSession, loadHistory, open, showToast]);

  useEffect(() => {
    const list = listRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messages, streaming]);

  const run = useCallback(
    async (preset: 'summary' | 'points') => {
      setBusy(preset);
      setStreaming('');
      setTruncated(false);
      try {
        const id = await ensureSession();
        if (!id) return;
        const controller = new AbortController();
        abortRef.current = controller;
        let answer = '';
        for await (const event of streamEvents(
          `/api/chat/session/${id}/message`,
          { preset },
          controller.signal,
        )) {
          if (event.truncated) setTruncated(true);
          if (event.delta) {
            answer += event.delta;
            setStreaming(answer);
          }
          if (event.error) throw new Error(event.error);
          if (event.done) {
            setMessages((current) => [
              ...current,
              { id: event.message_id ?? Date.now(), role: 'assistant', content: answer, preset },
            ]);
          }
        }
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error));
      } finally {
        setStreaming('');
        setBusy('');
        abortRef.current = null;
      }
    },
    [ensureSession, showToast],
  );

  const ask = useCallback(async () => {
    const text = question.trim();
    if (!text) return;
    setBusy('ask');
    setStreaming('');
    setTruncated(false);
    try {
      const id = await ensureSession();
      if (!id) return;
      const controller = new AbortController();
      abortRef.current = controller;
      setMessages((current) => [...current, { id: Date.now(), role: 'user', content: text }]);
      setQuestion('');
      let answer = '';
      for await (const event of streamEvents(
        `/api/chat/session/${id}/message`,
        { content: text },
        controller.signal,
      )) {
        if (event.truncated) setTruncated(true);
        if (event.delta) {
          answer += event.delta;
          setStreaming(answer);
        }
        if (event.error) throw new Error(event.error);
        if (event.done) {
          setMessages((current) => [
            ...current,
            { id: event.message_id ?? Date.now(), role: 'assistant', content: answer },
          ]);
        }
      }
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    } finally {
      setStreaming('');
      setBusy('');
      abortRef.current = null;
    }
  }, [ensureSession, question, showToast]);

  if (!open) {
    return (
      <button className="reader-ai-toggle" type="button" onClick={onToggle}>
        {t('articles.ai.open', '解读')}
      </button>
    );
  }

  const hasSummary = messages.some((item) => item.preset === 'summary');
  const hasPoints = messages.some((item) => item.preset === 'points');

  return (
    <aside className="reader-ai" aria-label={t('articles.ai.title', '文章解读')}>
      <div className="reader-ai-header">
        <h2 className="reader-ai-title">{t('articles.ai.title', '文章解读')}</h2>
        <button className="btn ghost" type="button" onClick={onToggle}>
          {t('articles.ai.close', '收起')}
        </button>
      </div>

      <div className="reader-ai-actions">
        <button
          className="btn"
          type="button"
          disabled={Boolean(busy)}
          onClick={() => void run('summary')}
        >
          {busy === 'summary'
            ? t('articles.ai.working', '生成中…')
            : hasSummary
              ? t('articles.ai.summaryCached', '摘要')
              : t('articles.ai.summary', '摘要')}
        </button>
        <button
          className="btn ghost"
          type="button"
          disabled={Boolean(busy)}
          onClick={() => void run('points')}
        >
          {busy === 'points'
            ? t('articles.ai.working', '生成中…')
            : hasPoints
              ? t('articles.ai.pointsCached', '要点')
              : t('articles.ai.points', '要点')}
        </button>
      </div>

      {truncated ? (
        <p className="muted reader-ai-note">
          {t('articles.ai.truncated', '正文过长，回答基于前一部分内容')}
        </p>
      ) : null}

      <div className="reader-ai-body" ref={listRef}>
        {messages.length === 0 && !streaming ? (
          <p className="muted reader-ai-empty">
            {t('articles.ai.empty', '点「摘要」或「要点」快速了解，也可以直接提问。')}
          </p>
        ) : null}
        {messages.map((message) => (
          <div key={message.id} className={`reader-ai-message is-${message.role}`}>
            {message.content}
          </div>
        ))}
        {streaming ? <div className="reader-ai-message is-assistant">{streaming}</div> : null}
        {busy === 'summary' || busy === 'points' ? (
          <div className="muted reader-ai-queued">{t('articles.ai.queued', '正在生成…')}</div>
        ) : null}
      </div>

      <form
        className="reader-ai-ask"
        onSubmit={(event) => {
          event.preventDefault();
          void ask();
        }}
      >
        <input
          className="input"
          placeholder={t('articles.ai.placeholder', '针对这篇文章提问…')}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          disabled={Boolean(busy)}
        />
        <button className="btn" type="submit" disabled={Boolean(busy) || !question.trim()}>
          {t('articles.ai.send', '发送')}
        </button>
      </form>
    </aside>
  );
}
