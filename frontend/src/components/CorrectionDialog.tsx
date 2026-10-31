import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useI18n } from '../i18n';
import { emitCorrectionsUpdated, listCorrectionAccounts, listCorrections, previewCorrection, saveCorrection, type CorrectionAccount, type CorrectionDraft, type CorrectionPreview, type CorrectionRule } from '../api/corrections';

export interface CorrectionDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSaved: (rule: CorrectionRule) => void;
  rule?: CorrectionRule;
  initialSource?: string;
  currentBiz?: string;
  articleId?: number;
  returnFocusRef?: React.RefObject<HTMLElement | null>;
}

export function CorrectionDialog(props: CorrectionDialogProps) {
  if (!props.isOpen) return null;
  return <CorrectionDialogContent {...props} />;
}

function CorrectionDialogContent({ onClose, onSaved, rule: initialRule, initialSource = '', currentBiz, articleId, returnFocusRef }: CorrectionDialogProps) {
  const [rule, setRule] = useState(initialRule);
  const { t } = useI18n();
  const [draft, setDraft] = useState<CorrectionDraft>(() => rule ? { source_text: rule.source_text, replacement_text: rule.replacement_text, account_biz: rule.account_biz, enabled: rule.enabled } : { source_text: initialSource, replacement_text: '', account_biz: currentBiz ? [currentBiz] : [], enabled: true });
  const [scoped, setScoped] = useState(draft.account_biz.length > 0);
  const [accounts, setAccounts] = useState<CorrectionAccount[]>([]);
  const [rules, setRules] = useState<CorrectionRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState<CorrectionPreview | null>(null);
  const [previewError, setPreviewError] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  const replacement = useRef<HTMLInputElement>(null);
  const closeRef = useRef(onClose);
  const savingRef = useRef(saving);
  useEffect(() => { closeRef.current = onClose; savingRef.current = saving; }, [onClose, saving]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const returnTarget = returnFocusRef?.current ?? previous;
    replacement.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !savingRef.current) { event.preventDefault(); closeRef.current(); }
      if (event.key !== 'Tab' || !dialog.current) return;
      const nodes = Array.from(dialog.current.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]'));
      if (!nodes.length) return;
      const first = nodes[0]; const last = nodes[nodes.length - 1];
      if (event.shiftKey && (document.activeElement === first || !dialog.current.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !dialog.current.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', keydown);
    return () => { document.removeEventListener('keydown', keydown); if (returnTarget?.isConnected) returnTarget.focus(); };
  }, [returnFocusRef]);
  useEffect(() => {
    let active = true;
    void Promise.all([listCorrectionAccounts(), listCorrections()]).then(([a, r]) => { if (active) { setAccounts(a); setRules(r); setLoading(false); } }).catch(() => { if (active) { setError(t('correction.loadFailed', 'Failed to load corrections.')); setLoadFailed(true); setLoading(false); } });
    return () => { active = false; };
  }, [t]);
  const valid = !!draft.source_text.trim() && !!draft.replacement_text.trim() && draft.source_text !== draft.replacement_text && Array.from(draft.source_text).length <= 200 && Array.from(draft.replacement_text).length <= 200 && (!scoped || (draft.account_biz.length > 0 && draft.account_biz.length <= 100 && draft.account_biz.every((biz) => accounts.some((a) => a.biz === biz))));
  const duplicate = rules.find((r) => r.id !== rule?.id && r.source_text === draft.source_text && r.account_biz.length === draft.account_biz.length && r.account_biz.every((biz) => draft.account_biz.includes(biz)));
  useEffect(() => {
    let active = true;
    if (!articleId || !valid || loading) return () => { active = false; };
    const timer = window.setTimeout(() => {
      void previewCorrection(draft, articleId, rule?.id).then((result) => { if (active) { setPreview(result); setPreviewError(false); } }).catch(() => { if (active) { setPreview(null); setPreviewError(true); } });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [articleId, draft, loading, rule?.id, valid]);
  const update = (change: Partial<CorrectionDraft>) => { setPreview(null); setPreviewError(false); setDraft((old) => ({ ...old, ...change })); };
  const editExisting = (id: number) => {
    const existing = rules.find((r) => r.id === id);
    if (existing) { setRule(existing); setScoped(existing.account_biz.length > 0); setPreview(null); setError(''); setDraft({ source_text: existing.source_text, replacement_text: existing.replacement_text, account_biz: existing.account_biz, enabled: existing.enabled }); replacement.current?.focus(); }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid || duplicate || saving || loading || loadFailed) return;
    setError('');
    setSaving(true);
    try {
      const saved = await saveCorrection(draft, rule?.id);
      emitCorrectionsUpdated(); onSaved(saved); onClose();
    } catch (err) { setError((err as Error).message || t('correction.saveFailed', 'Failed to save.')); }
    finally { setSaving(false); }
  };
  return <div className="modal-overlay" onClick={(e) => { if (e.target === e.currentTarget && !saving) onClose(); }}>
    <div className="modal correction-dialog" ref={dialog} role="dialog" aria-modal="true" aria-labelledby="correction-dialog-title">
      <form onSubmit={(e) => { void submit(e); }}>
        <h2 id="correction-dialog-title">{rule ? t('correction.edit', 'Edit correction') : t('correction.create', 'Add correction')}</h2>
        <label>{t('correction.source', 'Original text')}<input className="input" value={draft.source_text} maxLength={400} onChange={(e) => { setError(''); update({ source_text: e.target.value }); }} /></label>
        {Array.from(draft.source_text).length === 1 && <p className="muted">{t('correction.singleWarning', 'A single character may match unintended text.')}</p>}
        <label>{t('correction.replacement', 'Replacement text')}<input ref={replacement} className="input" value={draft.replacement_text} maxLength={400} onChange={(e) => { setError(''); update({ replacement_text: e.target.value }); }} /></label>
        <fieldset><legend>{t('correction.scope', 'Scope')}</legend>
          <label><input type="radio" name="correction-scope" checked={!scoped} onChange={() => { setScoped(false); update({ account_biz: [] }); }} /> {t('correction.general', 'All accounts')}</label>
          <label><input type="radio" name="correction-scope" checked={scoped} onChange={() => { setScoped(true); update({ account_biz: [] }); }} /> {t('correction.selectedAccounts', 'Selected accounts')}</label>
          {scoped && <>{draft.account_biz.filter((biz) => !accounts.some((a) => a.biz === biz)).map((biz) => <label key={biz}><input type="checkbox" checked onChange={() => update({ account_biz: draft.account_biz.filter((item) => item !== biz) })} /> {biz} — {t('correction.unavailable', 'Account unavailable')}</label>)}<label>{t('correction.searchAccounts', 'Search accounts')}<input className="input" value={search} onChange={(e) => setSearch(e.target.value)} /></label><div className="correction-accounts">{accounts.filter((a) => `${a.nickname} ${a.source} ${a.biz}`.toLowerCase().includes(search.toLowerCase())).map((a) => <label key={a.biz}><input type="checkbox" checked={draft.account_biz.includes(a.biz)} onChange={(e) => update({ account_biz: e.target.checked ? [...draft.account_biz, a.biz] : draft.account_biz.filter((biz) => biz !== a.biz) })} /> {a.avatar_url && <img src={a.avatar_url} alt="" />} {a.nickname} · {a.source}</label>)}</div></>}
        </fieldset>
        <label><input type="checkbox" checked={draft.enabled} onChange={(e) => update({ enabled: e.target.checked })} /> {t('correction.enabled', 'Enabled')}</label>
        {!valid && <p className="muted">{t('correction.validation', 'Enter different nonblank texts (up to 200 characters) and select up to 100 accounts for a scoped rule.')}</p>}
        {duplicate && <p role="alert">{t('correction.duplicate', 'A rule with this text and scope already exists.')} <button type="button" className="btn ghost" onClick={() => editExisting(duplicate.id)}>{t('correction.editExisting', 'Edit existing rule')}</button></p>}
        {articleId && preview && <div aria-live="polite"><p>{t(`correction.preview.${preview.status}`, preview.status)} {preview.status === 'ok' && preview.match_count}</p>{preview.contexts.map((c, i) => <div key={i}><div>{c.before}</div><div>→ {c.after}</div></div>)}{preview.status === 'conflict' && preview.existing_rule_id && <button type="button" onClick={() => editExisting(preview.existing_rule_id!)}>{t('correction.editExisting', 'Edit existing rule')}</button>}</div>}
        {articleId && previewError && <p role="alert">{t('correction.previewFailed', 'Preview failed.')}</p>}
        {error && <p role="alert">{error}</p>}
        <div className="toolbar"><button type="button" className="btn ghost" disabled={saving} onClick={onClose}>{t('common.cancel', 'Cancel')}</button><button className="btn" disabled={!valid || !!duplicate || saving || loading || loadFailed}>{saving ? t('common.saving', 'Saving…') : t('common.save', 'Save')}</button></div>
      </form>
    </div>
  </div>;
}
