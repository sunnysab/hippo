(() => {
  const { state, $, apiGet, apiSend, t, copyToClipboard, showToast } = window.Hippo;
  const ARTICLE_FILTER_PARAM_KEYS = ['group', 'account', 'type', 'q', 'sort'];
  const ARTICLE_SORT_PUBLISH_AT_DESC = 'publish_at_desc';
  const ARTICLE_SORT_RELEVANCE_DESC = 'relevance_desc';
  const ARTICLE_SORT_VALUES = new Set([ARTICLE_SORT_PUBLISH_AT_DESC, ARTICLE_SORT_RELEVANCE_DESC]);
  const articleAccountCache = new Map();
  const ITEM_SHOW_TYPE_META = {
    0: { key: 'articles.type.0', fallback: 'Regular Article', tone: 'regular' },
    5: { key: 'articles.type.5', fallback: 'Video Share', tone: 'video' },
    6: { key: 'articles.type.6', fallback: 'Music Share', tone: 'music' },
    7: { key: 'articles.type.7', fallback: 'Audio Share', tone: 'audio' },
    8: { key: 'articles.type.8', fallback: 'Picture Share', tone: 'picture' },
    10: { key: 'articles.type.10', fallback: 'Text Share', tone: 'text' },
    11: { key: 'articles.type.11', fallback: 'Article Share', tone: 'share' },
    17: { key: 'articles.type.17', fallback: 'Short Post', tone: 'short' },
  };
  const ITEM_SHOW_TYPE_ORDER = [0, 5, 6, 7, 8, 10, 11, 17];

  const setArticleSearchLoading = (isLoading) => {
    const loading = $('#article-search-loading');
    if (!loading) return;
    loading.classList.toggle('is-hidden', !isLoading);
  };

  const setArticleListLoading = (isLoading) => {
    const loading = $('#article-list-loading');
    if (!loading) return;
    loading.classList.toggle('is-hidden', !isLoading);
  };

  const showArticleContextMenu = (article, x, y) => {
    const menu = $('#article-context-menu');
    if (!menu) return;
    hideArticleImageContextMenu();
    const link = article?.source_url || article?.link || '';
    menu.dataset.link = link;
    menu.dataset.articleId = String(article?.id || '');
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
    menu.classList.remove('is-hidden');
    const openBtn = $('#article-menu-open');
    const copyBtn = $('#article-menu-copy');
    const disabled = !link;
    if (openBtn) openBtn.disabled = disabled;
    if (copyBtn) copyBtn.disabled = disabled;
  };

  const hideArticleContextMenu = () => {
    const menu = $('#article-context-menu');
    if (!menu) return;
    menu.classList.add('is-hidden');
    menu.dataset.link = '';
    menu.dataset.articleId = '';
  };

  const showArticleImageContextMenu = (imageId, x, y) => {
    const menu = $('#article-image-context-menu');
    if (!menu || !imageId) return;
    hideArticleContextMenu();
    menu.dataset.imageId = String(imageId);
    menu.style.left = `${x}px`;
    menu.style.top = `${y}px`;
    menu.classList.remove('is-hidden');
  };

  const hideArticleImageContextMenu = () => {
    const menu = $('#article-image-context-menu');
    if (!menu) return;
    menu.classList.add('is-hidden');
    menu.dataset.imageId = '';
  };

  const hideAllArticleContextMenus = () => {
    hideArticleContextMenu();
    hideArticleImageContextMenu();
  };

  const formatDate = (value) => {
    if (!value) return '';
    let date;
    if (typeof value === 'number') {
      date = new Date(value * 1000);
    } else {
      date = new Date(value);
    }
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleDateString('zh-CN');
  };

  const formatDateTime = (value) => {
    if (!value) return '';
    let date;
    if (typeof value === 'number') {
      date = new Date(value * 1000);
    } else {
      date = new Date(value);
    }
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleString('zh-CN');
  };

  const escapeHtml = (value) =>
    String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');

  const getItemShowTypeMeta = (value) => {
    const normalized = Number(value);
    if (!Number.isFinite(normalized)) return null;
    return ITEM_SHOW_TYPE_META[normalized] || null;
  };

  const getItemShowTypeLabel = (value) => {
    const meta = getItemShowTypeMeta(value);
    if (!meta) return t('articles.meta.unknown', 'Unknown');
    return t(meta.key, meta.fallback);
  };

  const renderItemShowTypeBadge = (value, options = {}) => {
    const meta = getItemShowTypeMeta(value);
    if (!meta) return '';
    const compactClass = options.compact ? ' item-show-type-badge-compact' : '';
    return `<span class="item-show-type-badge item-show-type-${meta.tone}${compactClass}">${escapeHtml(getItemShowTypeLabel(value))}</span>`;
  };

  const renderTypeLegend = () => {
    const container = $('#article-type-legend');
    if (!container) return;
    container.innerHTML = ITEM_SHOW_TYPE_ORDER
      .map((itemShowType) => renderItemShowTypeBadge(itemShowType, { compact: true }))
      .join('');
  };

  const buildArticleHeader = (article) => {
    if (!article) return null;
    const header = document.createElement('div');
    header.className = 'article-header';

    const title = document.createElement('h1');
    title.className = 'article-preview-title';
    title.textContent = article.title || '';
    const titleRow = document.createElement('div');
    titleRow.className = 'article-preview-title-row';
    titleRow.appendChild(title);
    if (article.item_show_type !== null && article.item_show_type !== undefined) {
      const typeWrap = document.createElement('div');
      typeWrap.className = 'article-preview-type';
      typeWrap.innerHTML = renderItemShowTypeBadge(article.item_show_type);
      titleRow.appendChild(typeWrap);
    }
    header.appendChild(titleRow);

    const meta = document.createElement('div');
    meta.className = 'article-preview-meta';

    const account = document.createElement('div');
    account.className = 'article-preview-account';
    const avatarUrl = article.account_avatar_url || article.account_avatar || '';
    if (avatarUrl) {
      const avatar = document.createElement('img');
      avatar.className = 'article-preview-avatar';
      avatar.src = avatarUrl;
      avatar.alt = article.account_nickname || article.account_alias || article.biz || '';
      avatar.onerror = () => {
        avatar.style.display = 'none';
      };
      account.appendChild(avatar);
    } else {
      const placeholder = document.createElement('div');
      placeholder.className = 'article-preview-avatar placeholder';
      account.appendChild(placeholder);
    }

    const accountName = document.createElement('span');
    accountName.className = 'article-preview-name';
    accountName.textContent = article.account_nickname || article.account_alias || article.biz || '';
    account.appendChild(accountName);

    const metaItems = document.createElement('div');
    metaItems.className = 'article-preview-items';

    const addMetaItem = (label, value) => {
      const item = document.createElement('div');
      item.className = 'article-preview-item';
      const labelEl = document.createElement('span');
      labelEl.className = 'article-preview-label';
      labelEl.textContent = label;
      const valueEl = document.createElement('span');
      valueEl.className = 'article-preview-value';
      valueEl.textContent = value || t('articles.meta.unknown', 'Unknown');
      item.appendChild(labelEl);
      item.appendChild(valueEl);
      metaItems.appendChild(item);
    };

    addMetaItem(t('articles.meta.author', 'Author'), article.author || '');
    addMetaItem(t('articles.meta.type', 'Type'), getItemShowTypeLabel(article.item_show_type));
    addMetaItem(
      t('articles.meta.publishedAt', 'Published'),
      formatDateTime(article.publish_at || article.created_at),
    );

    meta.appendChild(account);
    meta.appendChild(metaItems);
    header.appendChild(meta);

    return header;
  };

  const renderArticleList = () => {
    const list = $('#article-list');
    list.innerHTML = '';
    state.articles.forEach((article) => {
      const card = document.createElement('div');
      card.className = 'article-card' + (state.selectedArticleId === article.id ? ' is-active' : '');
      const thumb = article.image_id ? `/api/image/${article.image_id}` : '';
      const avatar = article.account_avatar_url || '';
      const digest = article.digest || '';
      card.innerHTML = `
        ${thumb ? `<img class="article-thumb" src="${thumb}" alt="" onerror="this.style.display='none'">` : '<div class="article-thumb placeholder"></div>'}
        <div class="article-info">
          <div class="article-title-row">
            <div class="article-title">${escapeHtml(article.title || '')}</div>
            ${renderItemShowTypeBadge(article.item_show_type, { compact: true })}
          </div>
          <div class="article-meta">
            ${avatar ? `<img class="article-avatar" src="${avatar}" alt="" onerror="this.style.display='none'">` : ''}
            <span>${escapeHtml(article.account_nickname || '')}</span>
            <span>${escapeHtml(formatDate(article.publish_at))}</span>
          </div>
          <div class="article-digest"></div>
        </div>
      `;
      const digestEl = card.querySelector('.article-digest');
      if (digestEl) {
        digestEl.textContent = digest;
        digestEl.title = digest;
      }
      card.addEventListener('click', () => selectArticle(article.id));
      card.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        showArticleContextMenu(article, event.clientX, event.clientY);
      });
      list.appendChild(card);
    });
    if (!state.articles.length) {
      list.innerHTML = `<div class="empty-state">${t('articles.emptyList', 'No articles found.')}</div>`;
    }
    // Remove manual load more button logic from UI if present, as it is now infinite scroll
    $('#btn-article-more').parentElement.style.display = 'none';
  };

  const loadArticles = async (reset = true) => {
    if (state.isArticleLoading) return;
    state.isArticleLoading = true;
    
    if (reset) {
      state.articlePage = 1;
      state.articles = [];
      state.hasMoreArticles = true;
    }
    const groupId = $('#article-group-filter')?.value;
    const accountBiz = $('#article-account-filter')?.value;
    const itemShowType = $('#article-type-filter')?.value;
    const search = $('#article-search')?.value.trim();
    const sort = resolveArticleSort(Boolean(search));
    const shouldShowLoading = Boolean(search);
    const shouldShowListLoading = reset && !search;
    setArticleSearchLoading(shouldShowLoading);
    if (shouldShowListLoading) {
      setArticleListLoading(true);
      const list = $('#article-list');
      if (list) list.innerHTML = '';
    }
    const url = new URL('/api/article', window.location.origin);
    if (groupId) url.searchParams.set('group_id', groupId);
    if (accountBiz) url.searchParams.set('biz', accountBiz);
    if (itemShowType) url.searchParams.set('item_show_type', itemShowType);
    if (search) url.searchParams.set('q', search);
    url.searchParams.set('sort', sort);
    url.searchParams.set('page', state.articlePage.toString());
    url.searchParams.set('page_size', state.articlePageSize.toString());
    
    try {
        const payload = await apiGet(url.pathname + url.search);
        const newArticles = payload.articles || [];
        
        if (newArticles.length < state.articlePageSize) {
            state.hasMoreArticles = false;
        } else {
            state.hasMoreArticles = true;
        }

        if (reset) {
             state.articles = newArticles;
        } else {
             state.articles = state.articles.concat(newArticles);
        }
        renderArticleList();
    } finally {
        state.isArticleLoading = false;
        if (shouldShowLoading) {
          setArticleSearchLoading(false);
        }
        if (shouldShowListLoading) {
          setArticleListLoading(false);
        }
    }
  };

  const populateArticleGroupFilter = async () => {
    const select = $('#article-group-filter');
    if (!select) return;
    const currentValue = select.value;
    let groups = Array.isArray(state.groups) ? state.groups : [];
    try {
      const payload = await apiGet('/api/group');
      groups = payload.groups || [];
      state.groups = groups;
      state.defaultGroupId = payload.default_group_id || state.defaultGroupId;
    } catch (err) {
      console.warn('Failed to load group options for articles', err);
    }

    select.innerHTML = '';
    const allOption = document.createElement('option');
    allOption.value = '';
    allOption.textContent = t('filters.allGroups', 'All Groups');
    select.appendChild(allOption);

    groups.forEach((group) => {
      const opt = document.createElement('option');
      opt.value = String(group.id);
      opt.textContent = group.name;
      select.appendChild(opt);
    });

    if (currentValue && Array.from(select.options).some((opt) => opt.value === currentValue)) {
      select.value = currentValue;
    }
  };

  const populateArticleAccountFilter = async (options = {}) => {
    const select = $('#article-account-filter');
    if (!select) return;
    const force = Boolean(options?.force);
    const groupId = $('#article-group-filter')?.value || '';
    const cacheKey = String(groupId || '');
    const currentValue = select.value;

    const applyOptions = (accounts) => {
      select.innerHTML = '';
      const allOption = document.createElement('option');
      allOption.value = '';
      allOption.textContent = t('filters.allAccounts', 'All Accounts');
      select.appendChild(allOption);
      (Array.isArray(accounts) ? accounts : []).forEach((account) => {
        const opt = document.createElement('option');
        opt.value = account.biz;
        opt.textContent = account.nickname;
        select.appendChild(opt);
      });
      if (currentValue && Array.from(select.options).some((opt) => opt.value === currentValue)) {
        select.value = currentValue;
      }
    };

    const cached = articleAccountCache.get(cacheKey);
    if (!force && cached?.accounts) {
      applyOptions(cached.accounts);
      return;
    }

    const url = new URL('/api/account', window.location.origin);
    if (groupId) url.searchParams.set('group_id', groupId);
    url.searchParams.set('page_size', '200');
    let payload;
    try {
      payload = await apiGet(url.pathname + url.search);
    } catch (err) {
      console.warn('Failed to load account options for articles', err);
      applyOptions([]);
      return;
    }

    const accounts = Array.isArray(payload?.accounts) ? payload.accounts : [];
    articleAccountCache.set(cacheKey, { accounts });
    applyOptions(accounts);
  };

  const getArticleHashRoute = () => {
    const hash = window.location.hash || '';
    const cleaned = hash.replace(/^#\/?/, '').trim();
    if (!cleaned) {
      return { tab: null, params: new URLSearchParams() };
    }
    const queryIndex = cleaned.indexOf('?');
    if (queryIndex === -1) {
      return { tab: cleaned, params: new URLSearchParams() };
    }
    return {
      tab: cleaned.slice(0, queryIndex).trim() || null,
      params: new URLSearchParams(cleaned.slice(queryIndex + 1)),
    };
  };

  const hasArticleFilterParams = (params) =>
    ARTICLE_FILTER_PARAM_KEYS.some((key) => params.has(key));

  const getArticleUrlParams = () => {
    const { tab, params } = getArticleHashRoute();
    if (tab === 'articles') {
      return new URLSearchParams(params);
    }
    return new URLSearchParams();
  };

  const getDefaultArticleSort = (hasSearch) =>
    hasSearch ? ARTICLE_SORT_RELEVANCE_DESC : ARTICLE_SORT_PUBLISH_AT_DESC;

  const normalizeArticleSort = (value, hasSearch) => {
    const normalized = String(value || '').trim().toLowerCase();
    if (!ARTICLE_SORT_VALUES.has(normalized)) {
      return getDefaultArticleSort(hasSearch);
    }
    if (normalized === ARTICLE_SORT_RELEVANCE_DESC && !hasSearch) {
      return ARTICLE_SORT_PUBLISH_AT_DESC;
    }
    return normalized;
  };

  const resolveArticleSort = (hasSearch) => {
    const params = getArticleUrlParams();
    const sortSelect = $('#article-sort');
    const selectedSort = sortSelect ? sortSelect.value : '';
    const resolvedSort = params.has('sort')
      ? normalizeArticleSort(selectedSort, hasSearch)
      : getDefaultArticleSort(hasSearch);
    if (sortSelect && sortSelect.value !== resolvedSort) {
      sortSelect.value = resolvedSort;
    }
    return resolvedSort;
  };

  const updateArticleUrlParams = () => {
    const groupId = $('#article-group-filter')?.value;
    const accountBiz = $('#article-account-filter')?.value;
    const itemShowType = $('#article-type-filter')?.value;
    const search = $('#article-search')?.value.trim();
    const hasSearch = Boolean(search);
    const sortFilter = $('#article-sort');
    const currentSort = normalizeArticleSort(sortFilter?.value || '', hasSearch);
    const defaultSort = getDefaultArticleSort(hasSearch);
    if (sortFilter && sortFilter.value !== currentSort) {
      sortFilter.value = currentSort;
    }

    const params = getArticleUrlParams();
    if (groupId) params.set('group', groupId);
    else params.delete('group');
    if (accountBiz) params.set('account', accountBiz);
    else params.delete('account');
    if (itemShowType) params.set('type', itemShowType);
    else params.delete('type');
    if (search) params.set('q', search);
    else params.delete('q');
    if (currentSort !== defaultSort) params.set('sort', currentSort);
    else params.delete('sort');

    const url = new URL(window.location);
    ARTICLE_FILTER_PARAM_KEYS.forEach((key) => {
      url.searchParams.delete(key);
    });
    const query = params.toString();
    const target = `${url.pathname}${url.search}#/articles${query ? `?${query}` : ''}`;
    window.history.replaceState({}, '', target);
  };

  const applyArticleUrlParams = async () => {
    const params = getArticleUrlParams();
    const groupId = params.get('group');
    const accountBiz = params.get('account');
    const itemShowType = params.get('type');
    const search = params.get('q');
    const sort = params.get('sort');

    const groupFilter = $('#article-group-filter');
    const accountFilter = $('#article-account-filter');
    const searchInput = $('#article-search');
    const sortFilter = $('#article-sort');
    const typeFilter = $('#article-type-filter');

    await populateArticleGroupFilter();

    // Apply group filter first (affects account options)
    if (groupId && groupFilter) {
      groupFilter.value = groupId;
    }
    // Always populate account filter (needed for account selection)
    await populateArticleAccountFilter();

    // Apply account filter
    if (accountBiz && accountFilter) {
      // Check if value exists in options
      const optionExists = Array.from(accountFilter.options).some(opt => opt.value === accountBiz);
      if (optionExists) {
        accountFilter.value = accountBiz;
      }
    }
    if (itemShowType && typeFilter) {
      const optionExists = Array.from(typeFilter.options).some(opt => opt.value === itemShowType);
      if (optionExists) {
        typeFilter.value = itemShowType;
      }
    }

    // Apply search
    if (search && searchInput) {
      searchInput.value = search;
    }
    const hasSearch = Boolean(search && search.trim());
    if (sortFilter) {
      sortFilter.value = normalizeArticleSort(sort, hasSearch);
    }

    // Load articles with applied filters
    await loadArticles(true);
    updateArticleUrlParams();
  };

  const parseWechatUrl = (urlStr) => {
    try {
      let fullUrl = urlStr.trim();
      if (fullUrl.startsWith('//')) {
        fullUrl = `https:${fullUrl}`;
      }
      if (!/^https?:\/\//i.test(fullUrl)) {
        return null;
      }
      const url = new URL(fullUrl);
      if (url.hostname !== 'mp.weixin.qq.com') {
        return null;
      }
      const biz = url.searchParams.get('__biz');
      const mid = url.searchParams.get('mid');
      const idx = url.searchParams.get('idx');
      if (biz && mid && idx) {
        return { biz, mid, idx };
      }
    } catch (err) {
      return null;
    }
    return null;
  };

  const renderInline = (text) => {
    const escaped = escapeHtml(text || '');

    return escaped
      .replace(/\\\|/g, '|')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, url) => {
        const rawUrl = url.replace(/&amp;/g, '&').trim();
        const meta = parseWechatUrl(rawUrl);
        if (meta) {
          return `<a href="${url}" target="_blank" rel="noopener noreferrer" data-hippo-biz="${meta.biz}" data-hippo-mid="${meta.mid}" data-hippo-idx="${meta.idx}" class="js-article-link">${label}</a>`;
        }
        return `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`;
      })
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/\n/g, '<br>');
  };

  const renderArticleContent = (payload) => {
    const container = $('#article-preview');
    container.innerHTML = '';
    const reader = document.createElement('div');
    reader.className = 'reader';
    if (!payload) {
      reader.innerHTML = `<div class="empty-state">${t('articles.empty', 'Select an article to preview.')}</div>`;
      container.appendChild(reader);
      return;
    }

    const header = buildArticleHeader(payload.article);
    if (header) {
      reader.appendChild(header);
    }

    if (!Array.isArray(payload.content)) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      const status = String(payload?.content_status || '').trim().toLowerCase();
      if (status === 'invalid') {
        empty.textContent = t(
          'articles.contentInvalid',
          'Failed to parse article content. Please try syncing again.',
        );
      } else {
        empty.textContent = t(
          'articles.contentMissing',
          'Article content is not available yet. Please wait for sync to finish or sync again.',
        );
      }
      reader.appendChild(empty);
      container.appendChild(reader);
      return;
    }

    if (payload.content.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = t('articles.contentEmpty', 'This article has no content.');
      reader.appendChild(empty);
      container.appendChild(reader);
      return;
    }
    const fragment = document.createDocumentFragment();
    payload.content.forEach((block) => {
      if (!block || typeof block !== 'object') return;
      if (block.type === 'paragraph') {
        const p = document.createElement('p');
        p.innerHTML = renderInline(block.text || '');
        fragment.appendChild(p);
        return;
      }
      if (block.type === 'heading') {
        const level = Math.min(Math.max(Number(block.level) || 2, 2), 4);
        const h = document.createElement(`h${level}`);
        h.innerHTML = renderInline(block.text || '');
        fragment.appendChild(h);
        return;
      }
      if (block.type === 'image') {
        if (!block.image_id) {
          return;
        }
        const hideSmall = $('#reader-hide-small-images')?.checked || false;
        
        // Check for GIF based on URL if available, or we might need metadata.
        // The block usually doesn't have mime type directly, but let's check.
        // If we can't check GIF easily, we'll skip that part or rely on image loading.
        // Actually, we can check image dimensions on load.
        
        const figure = document.createElement('figure');
        figure.dataset.imageId = String(block.image_id);
        const img = document.createElement('img');
        if (block.image_id) {
          img.src = `/api/image/${block.image_id}`;
        }
        img.alt = block.alt || '';
        img.loading = 'lazy';
        img.dataset.imageId = String(block.image_id);
        img.addEventListener('contextmenu', (event) => {
          event.preventDefault();
          event.stopPropagation();
          showArticleImageContextMenu(block.image_id, event.clientX, event.clientY);
        });
        
        if (hideSmall) {
            img.onload = function() {
                const w = this.naturalWidth;
                const h = this.naturalHeight;
                // Rule: < 500x500 OR aspect ratio < 1.2 (assuming rect means squarish/portrait?)
                // "rectangular or aspect ratio < 1.2" is ambiguous.
                // "小于500*500 的（矩形或长宽比小于1.2的）图形"
                // Interpretation: If (w < 500 AND h < 500) AND (aspect_ratio < 1.2) -> Hide?
                // Or maybe "small rectangular images" generally.
                // Let's assume: if (w < 500 && h < 500) -> check shape.
                // If it is a small icon/spacer/footer image.
                
                // Let's implement: Hide if (w < 500 && h < 500).
                // Plus GIFs. We need to detect GIF.
                
                // Detecting GIF from <img> tag is hard without extension.
                // We can fetch head or check api response content_type if available.
                // The images list in payload has content_type!
                
                let isGif = false;
                const imageMeta = (payload.images || []).find(i => i.id === block.image_id);
                if (imageMeta && imageMeta.content_type && imageMeta.content_type.includes('gif')) {
                    isGif = true;
                }
                
                if (isGif) {
                    figure.style.display = 'none';
                    return;
                }

                if (w < 500 && h < 500) {
                     // Check aspect ratio
                     const ratio = w / h;
                     // "矩形或长宽比小于1.2" -> "Rectangle or ratio < 1.2"
                     // Every image is a rectangle. Maybe "Square-ish"?
                     // Let's assume user means "small icons or buttons".
                     // If both dims < 500, it's small.
                     // Let's just hide all < 500x500 for now based on common sense for "small images".
                     // "长宽比小于1.2" -> w/h < 1.2
                     if (ratio < 1.2) {
                         figure.style.display = 'none';
                     }
                }
            };
        }
        
        figure.appendChild(img);
        fragment.appendChild(figure);
      }
    });
    reader.appendChild(fragment);
    container.appendChild(reader);
    
    // Bind click events for internal article links
    container.querySelectorAll('.js-article-link').forEach(link => {
        link.addEventListener('click', async (e) => {
            const biz = link.dataset.hippoBiz;
            const mid = link.dataset.hippoMid;
            const idx = link.dataset.hippoIdx;
            
            e.preventDefault();
            try {
                const articleId = `${mid}-${idx}`;
                const searchUrl = new URL('/api/article', window.location.origin);
                searchUrl.searchParams.set('article_id', articleId);
                searchUrl.searchParams.set('biz', biz);

                const res = await apiGet(searchUrl.pathname + searchUrl.search);
                if (res.articles && res.articles.length > 0) {
                     selectArticle(res.articles[0].id);
                } else {
                     window.open(link.href, '_blank');
                }
            } catch (err) {
                console.warn('Failed to resolve internal link', err);
                window.open(link.href, '_blank');
            }
        });
    });
  };

  const resetArticlePreviewScroll = () => {
    const container = $('#article-preview');
    if (!container) return;
    container.scrollTop = 0;
  };

  const selectArticle = async (id) => {
    state.selectedArticleId = id;
    hideAllArticleContextMenus();
    renderArticleList();
    const payload = await apiGet(`/api/article/${id}`);
    state.currentArticlePayload = payload;
    renderArticleContent(payload);
    resetArticlePreviewScroll();
  };

  const blockArticleImage = async (imageId) => {
    if (!imageId) return;
    const container = $('#article-preview');
    const scrollTop = container ? container.scrollTop : 0;
    await apiSend(`/api/image/${imageId}/block`, 'POST', {});
    if (state.selectedArticleId) {
      const payload = await apiGet(`/api/article/${state.selectedArticleId}`);
      state.currentArticlePayload = payload;
      renderArticleContent(payload);
      if (container) {
        container.scrollTop = scrollTop;
      }
    }
    showToast(t('articles.imageBlocked', 'Image blocked.'));
  };

  const initReaderControls = () => {
    const apply = () => {
      const root = document.documentElement;
      const width = $('#reader-width')?.value || '860';
      const font = $('#reader-font')?.value || '17';
      const lineHeight = $('#reader-line')?.value || '1.8';
      const letter = $('#reader-letter')?.value || '0.2';
      const serif = $('#reader-serif')?.checked || false;
      const hideSmall = $('#reader-hide-small-images')?.checked || false;
      root.style.setProperty('--reader-width', `${width}px`);
      root.style.setProperty('--reader-font', `${font}px`);
      root.style.setProperty('--reader-line', lineHeight);
      root.style.setProperty('--reader-letter', `${letter}px`);
      root.style.setProperty('--reader-family', serif ? 'var(--font-serif)' : 'var(--font-sans)');
      const snapshot = {
        width,
        font,
        lineHeight,
        letter,
        serif,
        hideSmall,
      };
      localStorage.setItem('hippo-reader', JSON.stringify(snapshot));

      // Re-render if content exists
      const container = $('#article-preview');
      if (container.querySelector('.reader') && state.selectedArticleId) {
        // We need the article data to re-render, but we don't store full content in state usually.
        // However, the images are rendered based on block data.
        // The simplest way to apply image filter without refetching is to just hide/show based on class,
        // but the requirements involve checking image dimensions which might not be loaded yet,
        // or we can check the block attributes if available.
        // Actually, let's just re-fetch the article content from cache if possible, or just re-trigger render
        // But renderArticleContent takes payload.
        // Let's modify renderArticleContent to read the setting.
      }
      
      // Force re-render of current article if any
      if (state.selectedArticleId && state.currentArticlePayload) {
        renderArticleContent(state.currentArticlePayload);
      }
    };

    const saved = localStorage.getItem('hippo-reader');
    if (saved) {
      try {
        const config = JSON.parse(saved);
        if (config.width) $('#reader-width').value = config.width;
        if (config.font) $('#reader-font').value = config.font;
        if (config.lineHeight) $('#reader-line').value = config.lineHeight;
        if (config.letter) $('#reader-letter').value = config.letter;
        if (config.serif !== undefined) $('#reader-serif').checked = config.serif;
        if (config.hideSmall !== undefined) $('#reader-hide-small-images').checked = config.hideSmall;
      } catch (err) {
        console.warn('Failed to parse reader config', err);
      }
    }
    ['#reader-width', '#reader-font', '#reader-line', '#reader-letter', '#reader-serif', '#reader-hide-small-images'].forEach((selector) => {
      const input = $(selector);
      if (input) input.addEventListener('input', apply);
    });
    apply();
  };

  const initArticleLayout = () => {
    const layout = $('.article-layout');
    const toggle = $('#btn-article-toggle');
    const updateLabel = () => {
      const collapsed = layout.classList.contains('is-collapsed');
      const label = t(
        collapsed ? 'articles.showList' : 'articles.hideList',
        collapsed ? 'Show List' : 'Hide List',
      );
      toggle.setAttribute('title', label);
      toggle.setAttribute('aria-label', label);
    };
    if (toggle) {
      toggle.addEventListener('click', () => {
        layout.classList.toggle('is-collapsed');
        updateLabel();
      });
    }
    updateLabel();

    // Infinite Scroll for Article List
    const listScroll = $('#article-list');
    if (listScroll) {
      listScroll.addEventListener('scroll', () => {
        if (listScroll.scrollTop + listScroll.clientHeight >= listScroll.scrollHeight - 50) {
          // Check if already loading or no more data (implementation dependent)
          // For now, simple throttle or check state could work.
          // Ideally we should track 'isLoading' and 'hasMore'.
          if (!state.isArticleLoading && state.hasMoreArticles) {
             state.articlePage += 1;
             loadArticles(false);
          }
        }
      });
    }

    const resizer = $('#article-resizer');
    if (resizer) {
      const root = document.documentElement;
      const listPanel = $('.article-list');
      const saved = localStorage.getItem('hippo-article-width');
      if (saved) {
        root.style.setProperty('--article-list-width', saved);
      }
      const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
      let startX = 0;
      let startWidth = 0;
      const onMove = (event) => {
        const clientX = event.touches ? event.touches[0].clientX : event.clientX;
        const delta = clientX - startX;
        const width = clamp(startWidth + delta, 220, 800);
        root.style.setProperty('--article-list-width', `${width}px`);
      };
      const onUp = () => {
        document.body.style.cursor = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.removeEventListener('touchmove', onMove);
        document.removeEventListener('touchend', onUp);
        const current = getComputedStyle(root).getPropertyValue('--article-list-width').trim();
        if (current) {
          localStorage.setItem('hippo-article-width', current);
        }
      };
      const getStartWidth = () => {
        if (listPanel) {
          const rect = listPanel.getBoundingClientRect();
          if (rect.width > 0) return rect.width;
        }
        const current = getComputedStyle(root).getPropertyValue('--article-list-width').trim();
        const parsed = parseFloat(current);
        return Number.isFinite(parsed) && parsed > 0 ? parsed : 300;
      };
      resizer.addEventListener('mousedown', (event) => {
        startX = event.clientX;
        startWidth = getStartWidth();
        document.body.style.cursor = 'col-resize';
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
      resizer.addEventListener('touchstart', (event) => {
        startX = event.touches[0].clientX;
        startWidth = getStartWidth();
        document.addEventListener('touchmove', onMove);
        document.addEventListener('touchend', onUp);
      });
    }

    const readerCopy = $('#reader-copy');
    if (readerCopy) {
      readerCopy.addEventListener('click', async (e) => {
        e.stopPropagation();
        const content = document.querySelector('.article-preview-body .reader');
        if (content) {
          const text = content.innerText;
          try {
            await copyToClipboard(text);
            showToast(t('articles.copied', '已复制全文'));
          } catch (err) {
            console.error(err);
            showToast(t('articles.copyFailed', '复制失败'));
          }
        }
      });
    }

    const readerToggle = $('#reader-toggle');
    const readerControls = $('#reader-controls');
    if (readerToggle && readerControls) {
      readerToggle.addEventListener('click', (e) => {
        e.stopPropagation();
        readerControls.classList.remove('is-hidden');
        readerControls.classList.toggle('is-open');
      });
      readerControls.addEventListener('click', (e) => {
        e.stopPropagation();
      });
      document.addEventListener('click', () => {
        readerControls.classList.add('is-hidden');
        readerControls.classList.remove('is-open');
      });
    }
  };

  const bindEvents = () => {
    // $('#btn-article-refresh').addEventListener('click', () => loadArticles(true));
    $('#btn-article-more').addEventListener('click', () => {
      state.articlePage += 1;
      loadArticles(false);
    });
    $('#article-group-filter').addEventListener('change', async () => {
      await populateArticleAccountFilter();
      await loadArticles(true);
      updateArticleUrlParams();
    });
    $('#article-account-filter').addEventListener('change', () => {
      loadArticles(true);
      updateArticleUrlParams();
    });
    $('#article-type-filter').addEventListener('change', () => {
      loadArticles(true);
      updateArticleUrlParams();
    });
    $('#article-sort').addEventListener('change', () => {
      updateArticleUrlParams();
      loadArticles(true);
    });
    $('#article-search').addEventListener('input', () => {
      clearTimeout(state.articleTimer);
      state.articleTimer = setTimeout(() => {
        loadArticles(true);
        updateArticleUrlParams();
      }, 300);
    });

    const openBtn = $('#article-menu-open');
    if (openBtn) {
      openBtn.addEventListener('click', () => {
        const menu = $('#article-context-menu');
        const link = menu?.dataset.link;
        if (!link) return;
        window.open(link, '_blank', 'noopener,noreferrer');
        hideAllArticleContextMenus();
      });
    }
    const copyBtn = $('#article-menu-copy');
    if (copyBtn) {
      copyBtn.addEventListener('click', async () => {
        const menu = $('#article-context-menu');
        const link = menu?.dataset.link;
        if (!link) return;
        try {
          await copyToClipboard(link);
          showToast(t('articles.linkCopied', 'Link copied.'));
        } catch (err) {
          showToast(link);
        }
        hideAllArticleContextMenus();
      });
    }

    const blockImageBtn = $('#article-image-menu-block');
    if (blockImageBtn) {
      blockImageBtn.addEventListener('click', async () => {
        const menu = $('#article-image-context-menu');
        const imageId = Number(menu?.dataset.imageId || 0);
        if (!imageId) return;
        blockImageBtn.disabled = true;
        try {
          await blockArticleImage(imageId);
          hideAllArticleContextMenus();
        } catch (err) {
          showToast(err?.message || t('articles.imageBlockFailed', 'Failed to block image.'));
        } finally {
          blockImageBtn.disabled = false;
        }
      });
    }

    document.addEventListener('click', (event) => {
      const menu = $('#article-context-menu');
      const imageMenu = $('#article-image-context-menu');
      if (
        menu
        && !menu.classList.contains('is-hidden')
        && menu.contains(event.target)
      ) {
        return;
      }
      if (
        imageMenu
        && !imageMenu.classList.contains('is-hidden')
        && imageMenu.contains(event.target)
      ) {
        return;
      }
      hideAllArticleContextMenus();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        hideAllArticleContextMenus();
      }
    });
    window.addEventListener(
      'scroll',
      () => {
        hideAllArticleContextMenus();
      },
      true,
    );
  };

  const refresh = async () => {
    await populateArticleGroupFilter();
    await populateArticleAccountFilter({ force: true });
    await loadArticles(true);
  };

  const init = async () => {
    renderTypeLegend();
    initReaderControls();
    initArticleLayout();
    bindEvents();
    const params = getArticleUrlParams();
    if (hasArticleFilterParams(params)) {
      await applyArticleUrlParams();
    } else {
      await refresh();
    }
  };

  window.HippoArticles = {
    init,
    refresh,
    loadArticles,
    populateArticleGroupFilter,
    populateArticleAccountFilter,
    updateArticleUrlParams,
    applyArticleUrlParams,
  };
})();
