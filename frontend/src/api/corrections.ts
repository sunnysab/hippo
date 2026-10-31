import { apiGet, apiSend } from './index';

export interface CorrectionDraft {
  source_text: string;
  replacement_text: string;
  account_biz: string[];
  enabled: boolean;
}
export interface CorrectionRule extends CorrectionDraft { id: number }
export interface CorrectionAccount { biz: string; nickname: string; source: string; avatar_url: string; is_disabled?: boolean }
export interface CorrectionPreview {
  status: 'ok' | 'no_asr' | 'not_applicable' | 'conflict';
  match_count: number;
  contexts: { before: string; after: string }[];
  existing_rule_id: number | null;
}
export const CORRECTIONS_UPDATED_EVENT = 'hippo:corrections-updated';
export const emitCorrectionsUpdated = () => window.dispatchEvent(new Event(CORRECTIONS_UPDATED_EVENT));
export const listCorrections = async () => (await apiGet('/api/correction') as unknown as { items: CorrectionRule[] }).items;
export async function listCorrectionAccounts() {
  const accounts: CorrectionAccount[] = [];
  let page = 1;
  let received = 0;
  while (true) {
    const result = await apiGet(`/api/account?page=${page}&page_size=200`) as unknown as { accounts: CorrectionAccount[]; total: number };
    received += result.accounts.length;
    accounts.push(...result.accounts.filter((account) => !account.is_disabled));
    if (received >= result.total || result.accounts.length === 0) return accounts;
    page += 1;
  }
}
export const saveCorrection = async (draft: CorrectionDraft, id?: number) => apiSend(id === undefined ? '/api/correction' : `/api/correction/${id}`, id === undefined ? 'POST' : 'PUT', { ...draft }) as unknown as Promise<CorrectionRule>;
export const deleteCorrection = async (id: number) => apiSend(`/api/correction/${id}`, 'DELETE', {});
export const previewCorrection = async (draft: CorrectionDraft, articleId: number, editingRuleId?: number) => apiSend('/api/correction/preview', 'POST', { ...draft, article_id: articleId, ...(editingRuleId === undefined ? {} : { editing_rule_id: editingRuleId }) }) as unknown as Promise<CorrectionPreview>;
