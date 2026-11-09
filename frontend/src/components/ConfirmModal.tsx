import { useEffect, useId, useState } from 'react';
import { useI18n } from '../i18n';

interface ConfirmModalProps {
  isOpen: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

export function ConfirmModal({
  isOpen,
  title,
  message,
  confirmLabel,
  onClose,
  onConfirm,
}: ConfirmModalProps) {
  const { t } = useI18n();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const titleId = useId();
  const messageId = useId();

  useEffect(() => {
    if (!isOpen) return;
    const handleEsc = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) {
        setError('');
        onClose();
      }
    };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [isOpen, onClose, submitting]);

  if (!isOpen) return null;

  const handleConfirm = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError('');
    try {
      await onConfirm();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : t('common.operationFailed', '操作失败，请重试。'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="modal-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget && !submitting) {
          setError('');
          onClose();
        }
      }}
    >
      <div className="modal confirm-modal" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={messageId}>
        <div className="modal-title" id={titleId}>{title}</div>
        <p className="muted confirm-modal-copy" id={messageId}>{message}</p>
        {error && <p role="alert">{error}</p>}
        <div className="toolbar confirm-modal-actions">
          <button className="btn ghost" type="button" disabled={submitting} onClick={() => { setError(''); onClose(); }}>
            {t('common.cancel', '取消')}
          </button>
          <button className="btn" type="button" disabled={submitting} onClick={() => { void handleConfirm(); }}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
