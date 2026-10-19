import { createContext, useContext, useReducer, type ReactNode, type Dispatch } from 'react';

export interface Article {
  id: number;
  biz: string;
  article_id: string;
  title: string;
  item_show_type: number | null;
  author: string;
  digest: string;
  cover: string;
  link: string;
  source_url: string;
  publish_at: number;
  created_at: string;
  account_nickname: string;
  account_alias: string;
  account_avatar: string;
  account_avatar_url: string;
  group_id: number;
  group_name: string;
  image_id: number | null;
  /** Source discriminator, e.g. `weixin`. Labels live in `utils/source`. */
  source: string;
  /** Platform-neutral kind (`article` / `video` / `audio` / …), set by the API. */
  content_kind?: string | null;
  /** Length of the audio or video behind this entry, when there is one. */
  duration_seconds?: number | null;
  /** When this user read it; null means unread. */
  read_at: string | null;
  /** When this user starred it; null means not starred. */
  starred_at: string | null;
}

export interface ArticleContentBlock {
  type: 'paragraph' | 'heading' | 'image' | 'code' | 'video' | 'audio' | 'link' | 'text';
  text?: string;
  level?: number;
  image_id?: number;
  alt?: string;
  /**
   * Seconds into the recording this paragraph starts at. Video and podcast
   * articles carry it, which is what the anchor in front of a paragraph seeks to.
   */
  t?: number;
  /** Player to embed for a video block. The provider is the backend's business. */
  embed_url?: string;
  video_id?: string;
  provider?: string;
  cover_url?: string;
  /** Where an audio block plays from (the archived file, or the original). */
  url?: string;
  duration?: number;
}

export interface ArticleImageMeta {
  id: number;
  content_type?: string;
  [key: string]: unknown;
}

export interface ArticlePayload {
  article: Article;
  content: ArticleContentBlock[] | null;
  content_status: string;
  content_updated_at: string | null;
  images: ArticleImageMeta[];
}

export interface FacetItem {
  item_show_type: number;
  count: number;
}

export interface FacetPayload {
  total: number;
  item_show_type_facets: FacetItem[];
}

interface ArticlesState {
  articles: Article[];
  articlePage: number;
  articlePageSize: number;
  hasMoreArticles: boolean;
  isArticleLoading: boolean;
  selectedArticleId: number | null;
  currentArticlePayload: ArticlePayload | null;
  filtersCollapsed: boolean;
  mobileReading: boolean;
  typeFacetsExpanded: boolean;
  lastFacetPayload: FacetPayload | null;
}

export type ArticlesAction =
  | { type: 'SET_ARTICLES'; articles: Article[]; reset: boolean }
  | { type: 'SET_PAGE'; page: number; hasMore: boolean }
  | { type: 'SET_LOADING'; loading: boolean }
  | { type: 'SELECT_ARTICLE'; id: number | null; payload: ArticlePayload | null }
  | { type: 'SET_FILTERS_COLLAPSED'; collapsed: boolean }
  | { type: 'SET_MOBILE_READING'; reading: boolean }
  | { type: 'SET_TYPE_FACETS_EXPANDED'; expanded: boolean }
  | { type: 'SET_ARTICLE_READ'; id: number; readAt: string | null }
  | { type: 'SET_ARTICLE_STAR'; id: number; starredAt: string | null }
  | { type: 'SET_FACET_PAYLOAD'; payload: FacetPayload | null };

const initialState: ArticlesState = {
  articles: [],
  articlePage: 1,
  articlePageSize: 20,
  hasMoreArticles: true,
  isArticleLoading: false,
  selectedArticleId: null,
  currentArticlePayload: null,
  filtersCollapsed: true,
  mobileReading: false,
  typeFacetsExpanded: false,
  lastFacetPayload: null,
};

function reducer(state: ArticlesState, action: ArticlesAction): ArticlesState {
  switch (action.type) {
    case 'SET_ARTICLES':
      return {
        ...state,
        articles: action.articles,
      };
    case 'SET_PAGE':
      return { ...state, articlePage: action.page, hasMoreArticles: action.hasMore };
    case 'SET_LOADING':
      return { ...state, isArticleLoading: action.loading };
    case 'SELECT_ARTICLE':
      return {
        ...state,
        selectedArticleId: action.id,
        currentArticlePayload: action.payload,
      };
    case 'SET_FILTERS_COLLAPSED':
      return { ...state, filtersCollapsed: action.collapsed };
    case 'SET_MOBILE_READING':
      return { ...state, mobileReading: action.reading };
    case 'SET_TYPE_FACETS_EXPANDED':
      return { ...state, typeFacetsExpanded: action.expanded };
    case 'SET_ARTICLE_READ':
      // The open article and its row must agree, so both are patched here.
      return {
        ...state,
        articles: state.articles.map((article) => (
          article.id === action.id ? { ...article, read_at: action.readAt } : article
        )),
        currentArticlePayload: state.currentArticlePayload?.article.id === action.id
          ? {
            ...state.currentArticlePayload,
            article: { ...state.currentArticlePayload.article, read_at: action.readAt },
          }
          : state.currentArticlePayload,
      };
    case 'SET_ARTICLE_STAR':
      return {
        ...state,
        articles: state.articles.map((article) => (
          article.id === action.id ? { ...article, starred_at: action.starredAt } : article
        )),
        currentArticlePayload: state.currentArticlePayload?.article.id === action.id
          ? {
            ...state.currentArticlePayload,
            article: { ...state.currentArticlePayload.article, starred_at: action.starredAt },
          }
          : state.currentArticlePayload,
      };
    case 'SET_FACET_PAYLOAD':
      return { ...state, lastFacetPayload: action.payload };
    default:
      return state;
  }
}

const ArticlesContext = createContext<{
  state: ArticlesState;
  dispatch: Dispatch<ArticlesAction>;
} | null>(null);

export function ArticlesProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  return (
    <ArticlesContext.Provider value={{ state, dispatch }}>
      {children}
    </ArticlesContext.Provider>
  );
}

export function useArticlesState() {
  const ctx = useContext(ArticlesContext);
  if (!ctx) throw new Error('useArticlesState must be used within ArticlesProvider');
  return ctx;
}
