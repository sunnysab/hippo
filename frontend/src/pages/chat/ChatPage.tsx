import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiSend } from '../../api';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { ConfirmModal } from '../../components/ConfirmModal';
import { streamEvents } from '../articles/stream';

interface Session {
  id: number;
  article_id: number | null;
  title: string;
  updated_at: string | null;
}

interface Message {
  id: number;
  role: string;
  content: string;
}

/** Session list on the left, transcript on the right. */
export function ChatPage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [streaming, setStreaming] = useState('');
  const [busy, setBusy] = useState(false);
  const [question, setQuestion] = useState('');
  const [pendingDelete, setPendingDelete] = useState<Session | null>(null);
  const [renaming, setRenaming] = useState<Session | null>(null);
  const [renameDraft, setRenameDraft] = useState('');

  const loadSessions = useCallback(async () => {
    const payload = await apiGet('/api/chat/session');
    setSessions((payload.items as Session[]) ?? []);
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        await loadSessions();
      } catch {
        /* the empty state covers it */
      }
    })();
  }, [loadSessions]);

  useEffect(() => {
    if (activeId === null) return;
    void (async () => {
      try {
        const payload = await apiGet(`/api/chat/session/${activeId}`);
        setMessages((payload.messages as Message[]) ?? []);
      } catch {
        setMessages([]);
      }
    })();
  }, [activeId]);

  const send = async () => {
    const text = question.trim();
    if (!text || activeId === null) return;
    setBusy(true);
    setStreaming('');
    setMessages((current) => [...current, { id: Date.now(), role: 'user', content: text }]);
    setQuestion('');
    try {
      let answer = '';
      for await (const event of streamEvents(`/api/chat/session/${activeId}/message`, {
        content: text,
      })) {
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
      await loadSessions();
    } catch (error) {
      showToast(error instanceof Error ? error.message : String(error));
    } finally {
      setStreaming('');
      setBusy(false);
    }
  };

  const newSession = async () => {
    const created = (await apiSend('/api/chat/session', 'POST', {})) as unknown as Session;
    await loadSessions();
    setActiveId(created.id);
  };

  const removeSession = async (session: Session) => {
    await apiSend(`/api/chat/session/${session.id}`, 'DELETE', {});
    if (activeId === session.id) {
      setActiveId(null);
      setMessages([]);
    }
    await loadSessions();
  };

  const submitRename = async () => {
    if (!renaming || !renameDraft.trim()) return;
    await apiSend(`/api/chat/session/${renaming.id}/rename`, 'POST', { title: renameDraft.trim() });
    setRenaming(null);
    await loadSessions();
  };

  return (
    <div className="chat-layout">
      <aside className="chat-sessions panel">
        <div className="panel-header">
          <h2 className="panel-title">{t('chat.title', '对话')}</h2>
          <button className="btn ghost" type="button" onClick={() => void newSession()}>
            {t('chat.new', '新对话')}
          </button>
        </div>
        {sessions.length === 0 ? (
          <p className="muted admin-empty">{t('chat.empty', '还没有对话')}</p>
        ) : (
          <ul className="chat-session-list">
            {sessions.map((session) => (
              <li key={session.id}>
                <button
                  className={`chat-session-item${activeId === session.id ? ' is-active' : ''}`}
                  type="button"
                  onClick={() => setActiveId(session.id)}
                >
                  <span className="chat-session-title">{session.title}</span>
                  {session.article_id ? (
                    <span className="admin-badge">{t('chat.article', '文章')}</span>
                  ) : null}
                </button>
                <div className="chat-session-actions">
                  <button
                    className="btn ghost"
                    type="button"
                    onClick={() => {
                      setRenaming(session);
                      setRenameDraft(session.title);
                    }}
                  >
                    {t('chat.rename', '重命名')}
                  </button>
                  <button className="btn ghost" type="button" onClick={() => setPendingDelete(session)}>
                    {t('common.delete', '删除')}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="chat-main panel">
        {activeId === null ? (
          <p className="muted admin-empty">{t('chat.pick', '选择或新建一个对话')}</p>
        ) : (
          <>
            <div className="chat-transcript">
              {messages.map((message) => (
                <div key={message.id} className={`chat-bubble is-${message.role}`}>
                  {message.content}
                </div>
              ))}
              {streaming ? <div className="chat-bubble is-assistant">{streaming}</div> : null}
              {busy && !streaming ? (
                <p className="muted chat-queued">{t('chat.thinking', '正在生成…')}</p>
              ) : null}
            </div>
            <form
              className="chat-ask"
              onSubmit={(event) => {
                event.preventDefault();
                void send();
              }}
            >
              <input
                className="input"
                placeholder={t('chat.placeholder', '输入消息…')}
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                disabled={busy}
              />
              <button className="btn" type="submit" disabled={busy || !question.trim()}>
                {t('articles.ai.send', '发送')}
              </button>
            </form>
          </>
        )}
      </section>

      {renaming ? (
        <div className="modal-overlay">
          <div className="modal">
            <div className="modal-title">{t('chat.rename', '重命名')}</div>
            <input
              className="input"
              value={renameDraft}
              onChange={(event) => setRenameDraft(event.target.value)}
            />
            <div className="toolbar">
              <button className="btn ghost" type="button" onClick={() => setRenaming(null)}>
                {t('common.cancel', '取消')}
              </button>
              <button className="btn" type="button" onClick={() => void submitRename()}>
                {t('common.save', '保存')}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <ConfirmModal
        isOpen={pendingDelete !== null}
        title={t('chat.deleteTitle', '删除对话')}
        message={t('chat.deleteConfirm', '删除后消息不可恢复，确定继续？')}
        confirmLabel={t('common.delete', '删除')}
        onConfirm={() => (pendingDelete ? removeSession(pendingDelete) : Promise.resolve())}
        onClose={() => setPendingDelete(null)}
      />
    </div>
  );
}
