import { useState, useCallback, useRef } from 'react';
import { useArticlesState } from '../store/articles';
import { useReaderSettings } from './useReaderSettings';
import { useMediaQuery } from './useMediaQuery';

/** The reading pane shares one column: outline and interpretation take turns. */
export type ReadingPanel = '' | 'toc' | 'ai';

export function useArticleReader() {
  const { state } = useArticlesState();
  const { config, updateConfig } = useReaderSettings();
  const [sidePanel, setSidePanel] = useState<ReadingPanel>('');
  const [listCollapsed, setListCollapsed] = useState(false);
  const previewRef = useRef<HTMLDivElement>(null);
  const isNarrowViewport = useMediaQuery('(max-width: 1100px)');

  const getSelectedArticleLink = useCallback(() => {
    const article = state.currentArticlePayload?.article ||
      state.articles.find((a) => a.id === state.selectedArticleId);
    return article?.source_url || article?.link || '';
  }, [state.currentArticlePayload, state.articles, state.selectedArticleId]);

  return {
    config,
    updateConfig,
    sidePanel,
    setSidePanel,
    previewRef,
    listCollapsed: !isNarrowViewport && listCollapsed,
    setListCollapsed,
    isNarrowViewport,
    mobileReading: state.mobileReading,
    getSelectedArticleLink,
  };
}
