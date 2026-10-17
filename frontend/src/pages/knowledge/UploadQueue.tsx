import { useI18n } from '../../i18n';
import type { KnowledgeDocument } from '../../api/knowledge';
import type { UploadTask } from './useUploads';

interface UploadQueueProps {
  tasks: UploadTask[];
  paused: boolean;
  baseNames: Record<string, string>;
  onRetry: (taskId: string) => void;
  onRemove: (taskId: string) => void;
  onResume: () => void;
  onOpenDocument: (document: KnowledgeDocument) => void;
}

/** Local failure codes are the hook's own vocabulary, not server sentences. */
const ERROR_KEYS: Record<string, string> = {
  'document.unsupported': 'knowledge.upload.error.unsupported',
  'document.empty': 'knowledge.upload.error.empty',
  'document.too_large': 'knowledge.upload.error.tooLarge',
  'document.file_mismatch': 'knowledge.upload.error.fileMismatch',
  'document.upload_busy': 'knowledge.upload.error.busy',
  'document.request_failed': 'knowledge.upload.error.requestFailed',
  'document.invalid_response': 'knowledge.upload.error.uncertain',
  'document.persistence_failed': 'knowledge.upload.error.uncertain',
  'AUTH_REQUIRED': 'knowledge.upload.error.auth',
  'NETWORK': 'knowledge.upload.error.network',
  'TIMEOUT': 'knowledge.upload.error.timeout',
};

/** The queue is a view of the upload hook: it never requests anything itself. */
export function UploadQueue({
  tasks,
  paused,
  baseNames,
  onRetry,
  onRemove,
  onResume,
  onOpenDocument,
}: UploadQueueProps) {
  const { t } = useI18n();

  return (
    <section className="knowledge-queue panel" aria-label={t('knowledge.upload.queueTitle', 'Upload queue')}>
      <div className="knowledge-queue-head">
        <h3 className="knowledge-queue-title">{t('knowledge.upload.queueTitle', 'Upload queue')}</h3>
        {paused && (
          <div className="knowledge-queue-paused" role="status">
            <span>{t('knowledge.upload.paused', 'The queue is paused.')}</span>
            <button type="button" className="btn ghost" onClick={onResume}>
              {t('knowledge.upload.resume', 'Continue')}
            </button>
          </div>
        )}
      </div>
      <ul className="knowledge-queue-list">
        {tasks.map((task) => {
          const percent = task.total > 0
            ? Math.min(100, Math.round((task.loaded / task.total) * 100))
            : 0;
          const document = task.document;
          return (
            <li className="knowledge-queue-item" key={task.id}>
              <div className="knowledge-queue-main">
                <p className="knowledge-queue-name">{task.file.name}</p>
                <p className="knowledge-queue-meta">
                  {baseNames[task.baseId] !== undefined && <span>{baseNames[task.baseId]}</span>}
                  {/* ``uploaded`` only means the server accepted the bytes; the
                      real import state comes from the document itself. */}
                  <span>{t(`knowledge.upload.phase.${task.phase}`, task.phase)}</span>
                  {document !== undefined && (
                    <span>{t(`knowledge.reader.state.${document.state}`, document.state)}</span>
                  )}
                  {task.phase === 'uploading' && <span>{`${percent}%`}</span>}
                  {task.errorCode !== undefined && (
                    <span className="knowledge-queue-error">
                      {t(ERROR_KEYS[task.errorCode] ?? 'knowledge.upload.error.generic', 'The request failed.')}
                    </span>
                  )}
                  {/* Without a server id there is nothing to GET, and a retry
                      would POST a second record the user never asked for. */}
                  {task.phase === 'uncertain' && document === undefined && (
                    <span className="knowledge-queue-hint">
                      {t('knowledge.upload.uncertainNoId', 'The create result is unconfirmed. Refresh the document list to check for a pending record, then re-select the source file to continue it.')}
                    </span>
                  )}
                </p>
              </div>
              <div className="knowledge-queue-actions">
                {document?.state === 'ready' && (
                  <button type="button" className="btn ghost" onClick={() => onOpenDocument(document)}>
                    {t('knowledge.library.open', 'Open')}
                  </button>
                )}
                {/* ``failed`` retries the job; ``uncertain`` only reconciles with
                    the server, which never re-sends already accepted bytes. */}
                {task.phase === 'failed' && task.mime_type !== null && (
                  <button type="button" className="btn ghost" onClick={() => onRetry(task.id)}>
                    {t('knowledge.upload.retry', 'Retry')}
                  </button>
                )}
                {task.phase === 'uncertain' && task.mime_type !== null && document !== undefined && (
                  <button type="button" className="btn ghost" onClick={() => onRetry(task.id)}>
                    {t('knowledge.upload.check', 'Check status')}
                  </button>
                )}
                {/* The bytes were accepted but the import has not settled; the
                    same check updates the row from the server document. */}
                {task.phase === 'uploaded' && task.mime_type !== null && task.document !== undefined
                  && task.document.state !== 'ready'
                  && task.document.state !== 'failed'
                  && task.document.state !== 'unsupported'
                  && task.document.state !== 'deleted' && (
                  <button type="button" className="btn ghost" onClick={() => onRetry(task.id)}>
                    {t('knowledge.upload.check', 'Check status')}
                  </button>
                )}
                {/* An in-flight row is owned by the hook and cannot be removed. */}
                {task.phase !== 'registering' && task.phase !== 'uploading' && (
                  <button type="button" className="btn ghost" onClick={() => onRemove(task.id)}>
                    {t('knowledge.upload.remove', 'Remove')}
                  </button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
