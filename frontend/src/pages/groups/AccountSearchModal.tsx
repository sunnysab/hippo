import { useState, useEffect, useRef, useCallback } from 'react';
import { useGroupsState } from '../../store/groups';
import { useI18n } from '../../i18n';
import { apiGet, apiSend } from '../../api';
import { LoadingIndicator } from '../../components/LoadingIndicator';
import { emitRefresh } from '../../utils/events';

interface AccountSearchModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function AccountSearchModal({ isOpen, onClose }: AccountSearchModalProps) {
  const { state, dispatch } = useGroupsState();
  const { t } = useI18n();
  const [query, setQuery] = useState('');
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const search = useCallback(
    async (nextQuery: string, append = false) => {
      if (state.searchLoading) return;
      if (!nextQuery || nextQuery.length < 2) {
        dispatch({ type: 'SET_SEARCH_RESULTS', results: [], append: false });
        return;
      }
      dispatch({ type: 'SET_SEARCH_LOADING', loading: true });
      try {
        const url = new URL('/api/account/search', window.location.origin);
        url.searchParams.set('q', nextQuery);
        url.searchParams.set('page', String(state.searchPage));
        url.searchParams.set('page_size', '10');
        const payload = await apiGet(url.pathname + url.search);
        const results = (payload.results || []) as Array<{
          biz: string; nickname: string; alias: string;
          round_head_img: string; is_added: boolean; avatar_url: string;
        }>;
        dispatch({ type: 'SET_SEARCH_RESULTS', results, append });
        dispatch({
          type: 'SET_SEARCH_PAGE',
          page: results.length < 10 ? state.searchPage : state.searchPage + 1,
          hasMore: results.length >= 10,
        });
      } catch {
        dispatch({ type: 'SET_SEARCH_RESULTS', results: [], append: false });
      } finally {
        dispatch({ type: 'SET_SEARCH_LOADING', loading: false });
      }
    },
    [state.searchPage, state.searchLoading, dispatch],
  );

  const handleClose = useCallback(() => {
    setQuery('');
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    dispatch({ type: 'SET_SEARCH_RESULTS', results: [], append: false });
    dispatch({ type: 'SET_SEARCH_PAGE', page: 1, hasMore: true });
    onClose();
  }, [dispatch, onClose]);

  const handleAdd = async (biz: string, nickname: string, alias: string | null, round_head_img: string | null) => {
    await apiSend('/api/account', 'POST', {
      biz,
      nickname,
      alias: alias || null,
      round_head_img: round_head_img || null,
      group_id: state.selectedGroupId,
    });
    // Update the result to show "Added"
    dispatch({
      type: 'SET_SEARCH_RESULTS',
      results: state.searchResults.map((r) =>
        r.biz === biz ? { ...r, is_added: true } : r,
      ),
      append: false,
    });
    emitRefresh();
  };

  useEffect(() => {
    const handleEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) handleClose();
    };
    document.addEventListener('keydown', handleEsc);
    return () => document.removeEventListener('keydown', handleEsc);
  }, [handleClose, isOpen]);

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const nextQuery = e.target.value;
    setQuery(nextQuery);
    if (timerRef.current) clearTimeout(timerRef.current);
    dispatch({ type: 'SET_SEARCH_PAGE', page: 1, hasMore: true });
    timerRef.current = setTimeout(() => {
      void search(nextQuery, false);
    }, 300);
  };

  return (
    <div
      className={`modal-overlay${isOpen ? '' : ' is-hidden'}`}
      id="account-search-modal"
      onClick={(e) => { if (e.target === e.currentTarget) handleClose(); }}
    >
      <div className="modal search-modal">
        <div className="modal-header">
          <div className="modal-title">{t('accounts.searchTitle', 'Add Account')}</div>
          <button className="icon-btn" id="btn-account-search-close" type="button" aria-label="Close" onClick={handleClose}>
            <span className="icon">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18.3 5.7 12 12l6.3 6.3-1.4 1.4L10.6 13.4 4.3 19.7 2.9 18.3 9.2 12 2.9 5.7 4.3 4.3l6.3 6.3 6.3-6.3z"/></svg>
            </span>
          </button>
        </div>
        <div className="input modal-search">
          <input
            type="search"
            id="account-search-input"
            placeholder={t('accounts.searchPlaceholder', 'Search accounts')}
            value={query}
            onChange={handleInputChange}
          />
          <span className="icon">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15.5 14h-.79l-.28-.27A6 6 0 1 0 14 15.5l.27.28v.79L20 21.5 21.5 20l-6-6zm-5.5 0a4 4 0 1 1 0-8 4 4 0 0 1 0 8z"/></svg>
          </span>
        </div>
        <LoadingIndicator isLoading={state.searchLoading} id="account-search-loading" />
        <div className={`search-results${state.searchResults.length ? '' : ' is-hidden'}`} id="account-search-results">
          {state.searchResults.map((item) => (
            <div key={item.biz} className="search-item">
              {item.avatar_url ? (
                <img className="account-avatar" src={item.avatar_url} alt="" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }} />
              ) : (
                <div className="account-avatar"></div>
              )}
              <div className="search-meta">
                <div className="account-name">{item.nickname || ''}</div>
                <div className="account-sub">{item.alias || item.biz || ''}</div>
              </div>
              <div className="search-actions">
                {item.is_added ? (
                  <span className="meta-note">{t('accounts.searchAdded', 'Added')}</span>
                ) : (
                  <button
                    className="btn ghost search-add"
                    type="button"
                    onClick={() => handleAdd(item.biz, item.nickname, item.alias, item.round_head_img)}
                  >
                    {t('accounts.searchAdd', 'Add')}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
