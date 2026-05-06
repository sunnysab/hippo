import { useState, useCallback, useEffect, useRef } from 'react';
import { useArticlesState } from '../store/articles';
import { useReaderSettings } from './useReaderSettings';
import { useMediaQuery } from './useMediaQuery';

export function useArticleReader() {
  const { state } = useArticlesState();
  const { config, updateConfig } = useReaderSettings();
  const [readerControlsOpen, setReaderControlsOpen] = useState(false);
  const [listCollapsed, setListCollapsed] = useState(false);
  const readerControlsRef = useRef<HTMLDivElement>(null);
  const readerToggleRef = useRef<HTMLButtonElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const isNarrowViewport = useMediaQuery('(max-width: 720px)');

  useEffect(() => {
    if (!readerControlsOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (readerControlsRef.current?.contains(target)) return;
      if (readerToggleRef.current?.contains(target)) return;
      setReaderControlsOpen(false);
    };

    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [readerControlsOpen]);

  const getSelectedArticleLink = useCallback(() => {
    const article = state.currentArticlePayload?.article ||
      state.articles.find((a) => a.id === state.selectedArticleId);
    return article?.source_url || article?.link || '';
  }, [state.currentArticlePayload, state.articles, state.selectedArticleId]);

  return {
    config,
    updateConfig,
    readerControlsOpen,
    setReaderControlsOpen,
    readerControlsRef,
    readerToggleRef,
    previewRef,
    listCollapsed,
    setListCollapsed,
    isNarrowViewport,
    mobileReading: state.mobileReading,
    getSelectedArticleLink,
  };
}
