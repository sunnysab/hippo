import { useEffect, useState } from 'react';
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

  useEffect(() => {
    if (!isOpen) return;
    const handleEsc = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose();
    };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [isOpen, onClose, submitting]);

  if (!isOpen) return null;

  const handleConfirm = async () => {
    if (submitting) return;
    setSubmitting(true);
    try {
      await onConfirm();
      onClose();
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="modal-overlay"
      onClick={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
      }}
    >
      <div className="modal confirm-modal">
        <div className="modal-title">{title}</div>
        <p className="muted confirm-modal-copy">{message}</p>
        <div className="toolbar confirm-modal-actions">
          <button className="btn ghost" type="button" disabled={submitting} onClick={onClose}>
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
