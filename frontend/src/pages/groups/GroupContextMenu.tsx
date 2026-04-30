import { ContextMenu } from '../../components/ContextMenu';
import { useI18n } from '../../i18n';
import { useToast } from '../../hooks/useToast';
import { copyToClipboard } from '../../utils/clipboard';

interface GroupContextMenuProps {
  groupId: number;
  x: number;
  y: number;
  onClose: () => void;
}

export function GroupContextMenu({ groupId, x, y, onClose }: GroupContextMenuProps) {
  const { t } = useI18n();
  const { showToast } = useToast();

  const handleRSS = async () => {
    const url = new URL('/api/feed/mixed', window.location.origin);
    url.searchParams.set('group_id', String(groupId));
    url.searchParams.set('limit', '50');
    url.searchParams.set('format', 'rss');
    try {
      await copyToClipboard(url.toString());
      showToast(t('groups.rssCopied', 'RSS address copied.'));
    } catch {
      showToast(`${t('groups.rssPrompt', 'RSS address')}: ${url.toString()}`);
    }
    onClose();
  };

  return (
    <ContextMenu id="group-context-menu" isOpen x={x} y={y} onClose={onClose}>
      <button className="context-item" id="group-menu-rss" type="button" onClick={handleRSS}>
        RSS
      </button>
    </ContextMenu>
  );
}
