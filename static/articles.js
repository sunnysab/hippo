(() => {
  const { state, $, apiGet, t } = window.Hippo;

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
          <div class="article-title">${article.title}</div>
          <div class="article-meta">
            ${avatar ? `<img class="article-avatar" src="${avatar}" alt="" onerror="this.style.display='none'">` : ''}
            <span>${article.account_nickname || ''}</span>
            <span>${formatDate(article.created_at)}</span>
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
    const search = $('#article-search')?.value.trim();
    const url = new URL('/api/article', window.location.origin);
    if (groupId) url.searchParams.set('group_id', groupId);
    if (accountBiz) url.searchParams.set('biz', accountBiz);
    if (search) url.searchParams.set('q', search);
    url.searchParams.set('content', '1');
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
    }
  };

  const populateArticleAccountFilter = async () => {
    const groupId = $('#article-group-filter')?.value;
    const url = new URL('/api/account', window.location.origin);
    if (groupId) url.searchParams.set('group_id', groupId);
    url.searchParams.set('page_size', '200');
    const payload = await apiGet(url.pathname + url.search);
    const select = $('#article-account-filter');
    select.innerHTML = '';
    const allOption = document.createElement('option');
    allOption.value = '';
    allOption.textContent = t('filters.allAccounts', 'All Accounts');
    select.appendChild(allOption);
    payload.accounts.forEach((account) => {
      const opt = document.createElement('option');
      opt.value = account.biz;
      opt.textContent = account.nickname;
      select.appendChild(opt);
    });
  };

  const renderInline = (text) => {
    const escaped = String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
    return escaped
      .replace(/\\\|/g, '|')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/\n/g, '<br>');
  };

  const renderArticleContent = (payload) => {
    const container = $('#article-preview');
    container.innerHTML = '';
    const reader = document.createElement('div');
    reader.className = 'reader';
    if (!payload || !Array.isArray(payload.content)) {
      reader.innerHTML = `<div class="empty-state">${t('articles.empty', 'Select an article to preview.')}</div>`;
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
        const img = document.createElement('img');
        if (block.image_id) {
          img.src = `/api/image/${block.image_id}`;
        }
        img.alt = block.alt || '';
        img.loading = 'lazy';
        
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
  };

  const selectArticle = async (id) => {
    state.selectedArticleId = id;
    renderArticleList();
    const payload = await apiGet(`/api/article/${id}`);
    state.currentArticlePayload = payload;
    renderArticleContent(payload);
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
      root.style.setProperty('--reader-family', serif ? '"Source Serif 4", Georgia, serif' : '"Source Sans 3", sans-serif');
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
      resizer.addEventListener('mousedown', (event) => {
        startX = event.clientX;
        startWidth = parseFloat(getComputedStyle(root).getPropertyValue('--article-list-width'));
        document.body.style.cursor = 'col-resize';
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
      resizer.addEventListener('touchstart', (event) => {
        startX = event.touches[0].clientX;
        startWidth = parseFloat(getComputedStyle(root).getPropertyValue('--article-list-width'));
        document.addEventListener('touchmove', onMove);
        document.addEventListener('touchend', onUp);
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
    });
    $('#article-account-filter').addEventListener('change', () => loadArticles(true));
    $('#article-search').addEventListener('input', () => {
      clearTimeout(state.articleTimer);
      state.articleTimer = setTimeout(() => loadArticles(true), 300);
    });
  };

  const refresh = async () => {
    await populateArticleAccountFilter();
    await loadArticles(true);
  };

  const init = async () => {
    initReaderControls();
    initArticleLayout();
    bindEvents();
    await refresh();
  };

  window.HippoArticles = {
    init,
    refresh,
    loadArticles,
    populateArticleAccountFilter,
  };
})();
