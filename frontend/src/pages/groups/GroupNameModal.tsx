import { useEffect, useState } from 'react';
import { useI18n } from '../../i18n';

interface GroupNameModalProps {
  isOpen: boolean;
  title: string;
  initialValue: string;
  placeholder: string;
  submitLabel: string;
  onClose: () => void;
  onSubmit: (name: string) => Promise<void>;
}

export function GroupNameModal({
  isOpen,
  title,
  initialValue,
  placeholder,
  submitLabel,
  onClose,
  onSubmit,
}: GroupNameModalProps) {
  const { t } = useI18n();
  const [name, setName] = useState(initialValue);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    const handleEsc = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !saving) onClose();
    };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [isOpen, onClose, saving]);

  const handleSubmit = async () => {
    const trimmed = name.trim();
    if (!trimmed || saving) return;
    setSaving(true);
    try {
      await onSubmit(trimmed);
      onClose();
    } finally {
      setSaving(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div
      className="modal-overlay"
      id="group-name-modal"
      onClick={(event) => {
        if (event.target === event.currentTarget && !saving) onClose();
      }}
    >
      <div className="modal group-name-modal">
        <div className="modal-title">{title}</div>
        <div className="input group-name-modal-input">
          <input
            type="text"
            value={name}
            placeholder={placeholder}
            autoFocus
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                void handleSubmit();
              }
            }}
          />
        </div>
        <div className="toolbar group-name-modal-actions">
          <button className="btn ghost" type="button" disabled={saving} onClick={onClose}>
            {t('common.cancel', '取消')}
          </button>
          <button className="btn" type="button" disabled={saving || !name.trim()} onClick={() => { void handleSubmit(); }}>
            {submitLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
