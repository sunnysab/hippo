import { useState, useCallback, useRef } from 'react';
import { useArticlesState } from '../store/articles';
import { useReaderSettings } from './useReaderSettings';
import { useMediaQuery } from './useMediaQuery';

export function useArticleReader() {
  const { state } = useArticlesState();
  const { config, updateConfig } = useReaderSettings();
  const [aiOpen, setAiOpen] = useState(false);
  const [listCollapsed, setListCollapsed] = useState(false);
  const previewRef = useRef<HTMLDivElement>(null);
  const isNarrowViewport = useMediaQuery('(max-width: 720px)');

  const getSelectedArticleLink = useCallback(() => {
    const article = state.currentArticlePayload?.article ||
      state.articles.find((a) => a.id === state.selectedArticleId);
    return article?.source_url || article?.link || '';
  }, [state.currentArticlePayload, state.articles, state.selectedArticleId]);

  return {
    config,
    updateConfig,
    aiOpen,
    setAiOpen,
    previewRef,
    listCollapsed,
    setListCollapsed,
    isNarrowViewport,
    mobileReading: state.mobileReading,
    getSelectedArticleLink,
  };
}
