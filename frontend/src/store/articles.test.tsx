import { act, render } from '@testing-library/react';
import { useEffect, type RefObject } from 'react';
import { describe, expect, it } from 'vitest';
import {
  ArticlesProvider,
  useArticlesState,
  type Article,
  type ArticlesAction,
  type ArticlePayload,
} from './articles';

const article = (id: number, read_at: string | null = null): Article => ({
  id,
  biz: 'gh_1',
  article_id: `a${id}`,
  title: `文章 ${id}`,
  item_show_type: 0,
  author: '',
  digest: '',
  cover: '',
  link: '',
  source_url: '',
  publish_at: 0,
  created_at: '',
  account_nickname: '公众号',
  account_alias: '',
  account_avatar: '',
  account_avatar_url: '',
  group_id: 1,
  group_name: '',
  image_id: null,
  read_at,
});

const payload = (record: Article): ArticlePayload => ({
  article: record,
  content: [],
  content_status: 'ok',
  content_updated_at: null,
  images: [],
});

type State = ReturnType<typeof useArticlesState>['state'];
interface HarnessApi {
  state: () => State;
  dispatch: (action: ArticlesAction) => void;
}

function Harness({ apiRef }: { apiRef: RefObject<HarnessApi | null> }) {
  const { state, dispatch } = useArticlesState();

  useEffect(() => {
    apiRef.current = { state: () => state, dispatch };
  });

  return null;
}

function mount() {
  const apiRef: RefObject<HarnessApi | null> = { current: null };
  render(
    <ArticlesProvider>
      <Harness apiRef={apiRef} />
    </ArticlesProvider>,
  );
  const open = article(2);
  act(() => {
    apiRef.current!.dispatch({ type: 'SET_ARTICLES', articles: [article(1), open], reset: true });
    apiRef.current!.dispatch({ type: 'SELECT_ARTICLE', id: 2, payload: payload(open) });
  });
  // The harness replaces the handle on every render, so always read it fresh.
  return apiRef;
}

describe('read state in the articles store', () => {
  it('patches the listed row and the open payload together', () => {
    const api = mount();

    act(() => {
      api.current!.dispatch({ type: 'SET_ARTICLE_READ', id: 2, readAt: '2026-05-02T11:00:00+00:00' });
    });

    const state = api.current!.state();
    expect(state.articles[0].read_at).toBeNull();
    expect(state.articles[1].read_at).toBe('2026-05-02T11:00:00+00:00');
    expect(state.currentArticlePayload?.article.read_at).toBe('2026-05-02T11:00:00+00:00');
  });

  it('keeps the open payload when another row is toggled', () => {
    const api = mount();
    const before = api.current!.state().currentArticlePayload;

    act(() => {
      api.current!.dispatch({ type: 'SET_ARTICLE_READ', id: 1, readAt: null });
    });

    expect(api.current!.state().currentArticlePayload).toBe(before);
  });
});
