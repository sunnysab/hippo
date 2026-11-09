import { useCallback, useEffect, useState } from 'react';
import { CorrectionDialog } from '../../components/CorrectionDialog';
import { deleteCorrection, emitCorrectionsUpdated, listCorrectionAccounts, listCorrections, saveCorrection, type CorrectionAccount, type CorrectionRule } from '../../api/corrections';
import { useI18n } from '../../i18n';

export function CorrectionsPanel() {
  const { t } = useI18n();
  const [rules, setRules] = useState<CorrectionRule[]>([]);
  const [accounts, setAccounts] = useState<CorrectionAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [account, setAccount] = useState('');
  const [status, setStatus] = useState('');
  const [editing, setEditing] = useState<CorrectionRule | null | undefined>(undefined);
  const [busy, setBusy] = useState<number | null>(null);
  const load = useCallback(async () => {
    setLoading(true); setError('');
    try { const [r, a] = await Promise.all([listCorrections(), listCorrectionAccounts()]); setRules(r); setAccounts(a); }
    catch { setError(t('correction.loadFailed', 'Failed to load corrections.')); }
    finally { setLoading(false); }
  }, [t]);
  useEffect(() => {
    let active = true;
    void Promise.all([listCorrections(), listCorrectionAccounts()]).then(([r, a]) => {
      if (active) { setRules(r); setAccounts(a); setLoading(false); }
    }).catch(() => {
      if (active) { setError(t('correction.loadFailed', 'Failed to load corrections.')); setLoading(false); }
    });
    return () => { active = false; };
  }, [t]);
  const toggle = async (rule: CorrectionRule) => {
    setBusy(rule.id); setError('');
    try {
      const saved = await saveCorrection({ source_text: rule.source_text, replacement_text: rule.replacement_text, account_biz: rule.account_biz, enabled: !rule.enabled }, rule.id);
      setRules((old) => old.map((item) => item.id === saved.id ? saved : item)); emitCorrectionsUpdated();
    } catch { setError(t('correction.saveFailed', 'Failed to save.')); }
    finally { setBusy(null); }
  };
  const remove = async (rule: CorrectionRule) => {
    if (!window.confirm(t('correction.deleteConfirm', 'Delete this rule?'))) return;
    setBusy(rule.id); setError('');
    try { await deleteCorrection(rule.id); setRules((old) => old.filter((item) => item.id !== rule.id)); emitCorrectionsUpdated(); }
    catch { setError(t('correction.deleteFailed', 'Failed to delete.')); }
    finally { setBusy(null); }
  };
  const filtered = rules.filter((r) => `${r.source_text} ${r.replacement_text}`.toLowerCase().includes(search.toLowerCase()) && (!account || r.account_biz.includes(account)) && (!status || String(r.enabled) === status));
  return <div className="panel correction-panel">
    <div className="panel-header"><div><h2>{t('correction.title', 'Text corrections')}</h2></div><button className="btn" type="button" onClick={() => setEditing(null)}>{t('correction.create', 'Add correction')}</button></div>
    <div className="toolbar correction-filters">
      <label>{t('correction.search', 'Search text')}<input className="input" value={search} onChange={(e) => setSearch(e.target.value)} /></label>
      <label>{t('correction.accountFilter', 'Account')}<select value={account} onChange={(e) => setAccount(e.target.value)}><option value="">{t('correction.allAccounts', 'All accounts')}</option>{accounts.map((a) => <option key={a.biz} value={a.biz}>{a.nickname}</option>)}</select></label>
      <label>{t('correction.status', 'Status')}<select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">{t('correction.allStatuses', 'All statuses')}</option><option value="true">{t('correction.enabled', 'Enabled')}</option><option value="false">{t('correction.disabled', 'Disabled')}</option></select></label>
    </div>
    {loading ? <p>{t('common.loading', 'Loading…')}</p> : <>{error && <p role="alert">{error} <button type="button" onClick={() => void load()}>{t('common.retry', 'Retry')}</button></p>}{!error && filtered.length === 0 && <p className="muted">{t('correction.empty', 'No corrections found.')}</p>}{filtered.map((r) => <div className="correction-row" key={r.id}><div><strong>{r.source_text} → {r.replacement_text}</strong><div className="muted">{r.account_biz.length ? r.account_biz.map((biz) => accounts.find((a) => a.biz === biz)?.nickname || biz).join('、') : t('correction.general', 'All accounts')}</div></div><div className="toolbar"><label><input type="checkbox" checked={r.enabled} disabled={busy !== null} onChange={() => void toggle(r)} aria-label={`${t('correction.enabled', 'Enabled')}: ${r.source_text}`} /> {t('correction.enabled', 'Enabled')}</label><button type="button" className="btn ghost" onClick={() => setEditing(r)}>{t('correction.edit', 'Edit correction')}</button><button type="button" className="btn ghost" disabled={busy !== null} onClick={() => void remove(r)}>{t('correction.delete', 'Delete')}</button></div></div>)}</>}
    {editing !== undefined && <CorrectionDialog isOpen onClose={() => setEditing(undefined)} rule={editing || undefined} onSaved={(saved) => setRules((old) => [...old.filter((r) => r.id !== saved.id), saved])} />}
  </div>;
}
