import Fuse from '../vendor/fuse/fuse.min.mjs';

document.addEventListener('DOMContentLoaded', () => {
  const root = document.querySelector('[data-deal-filters]');
  if (!root) return;

  const search = root.querySelector('[data-deal-search]');
  const tabs = Array.from(root.querySelectorAll('[data-filter]'));
  const grid = root.parentElement?.querySelector('.deal-grid--browse');
  const noResults = root.parentElement?.querySelector('[data-no-results]');
  const noResultsQuery = root.parentElement?.querySelector('[data-no-results-query]');
  const noResultsHint = root.parentElement?.querySelector('[data-no-results-hint]');
  if (!search || !grid) return;

  const cards = Array.from(grid.querySelectorAll('[data-filter-card="true"]'));
  cards.forEach((card, index) => {
    card.dataset.originalIndex = String(index);
  });

  let activeFilter = 'all';
  const url = new URL(window.location.href);
  const initialQuery = (url.searchParams.get('q') || '').trim();
  if (initialQuery) {
    search.value = initialQuery;
  }

  const synonymMap = {
    tv: ['television', 'streaming', 'stick'],
    television: ['tv', 'streaming'],
    headphones: ['headphone', 'earphones', 'earbuds', 'audio'],
    earbuds: ['earbud', 'headphones', 'audio'],
    speaker: ['speakers', 'bluetooth', 'audio'],
    keyboard: ['mechanical', 'gaming', 'typing'],
    monitor: ['display', 'screen', 'ultrawide'],
    smartwatch: ['watch', 'fitness', 'tracker'],
    dumbbells: ['weights', 'gym', 'fitness'],
    purifier: ['air', 'filter', 'home'],
    laptop: ['notebook', 'computer'],
    phone: ['smartphone', 'mobile']
  };

  const normalize = (value) =>
    String(value || '')
      .toLowerCase()
      .normalize('NFKD')
      .replace(/[^a-z0-9\s]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();

  const tokenize = (value) => normalize(value).split(' ').filter(Boolean);

  const expandTokens = (tokens) => {
    const expanded = new Set(tokens);
    tokens.forEach((token) => {
      const base = token.endsWith('s') ? token.slice(0, -1) : token;
      if (base) expanded.add(base);
      if (base) expanded.add(`${base}s`);

      const synonyms = synonymMap[token] || synonymMap[base] || [];
      synonyms.forEach((word) => expanded.add(word));
    });
    return Array.from(expanded);
  };

  const records = cards.map((card) => ({
    card,
    title: normalize(card.dataset.title),
    summary: normalize(card.dataset.summary),
    tags: normalize(card.dataset.tags),
  }));

  const fuse = new Fuse(records, {
    includeScore: true,
    ignoreLocation: true,
    threshold: 0.4,
    minMatchCharLength: 2,
    keys: [
      { name: 'title', weight: 0.5 },
      { name: 'tags', weight: 0.3 },
      { name: 'summary', weight: 0.2 },
    ],
  });

  const matchesFilter = (card) => {
    if (activeFilter === 'all') return true;
    if (activeFilter === 'top-savings') return card.dataset.topSavings === 'true';
    if (activeFilter === 'latest') return card.dataset.latest === 'true';
    return true;
  };

  const affiliateRank = (el) => (el.dataset.affiliate === 'true' ? 1 : 0);

  const buildSearchScores = (rawQuery) => {
    const query = normalize(rawQuery);
    if (!query) return null;

    if (query.length < 2) {
      const quick = new Map();
      records.forEach((record) => {
        const haystack = `${record.title} ${record.summary} ${record.tags}`;
        if (haystack.includes(query)) {
          quick.set(record.card, 0.99);
        }
      });
      return quick.size > 0 ? quick : null;
    }

    const terms = expandTokens(tokenize(query));
    const requiredTerms = terms.filter((term) => term.length >= 3);
    const queries = [query, ...terms.filter((term) => term !== query)];
    const resultScores = new Map();

    queries.forEach((q) => {
      if (q.length < 2) return;
      const results = fuse.search(q, { limit: cards.length });
      results.forEach(({ item, score }) => {
        if (requiredTerms.length > 0) {
          const haystack = `${item.title} ${item.summary} ${item.tags}`;
          const hasOverlap = requiredTerms.some((term) => haystack.includes(term));
          if (!hasOverlap) return;
        }
        const prev = resultScores.get(item.card);
        const next = typeof score === 'number' ? score : 1;
        if (prev === undefined || next < prev) {
          resultScores.set(item.card, next);
        }
      });
    });

    return resultScores;
  };

  const collapseAllOpenDetails = () => {
    cards.forEach((card) => {
      card.querySelectorAll('details[open]').forEach((detailsEl) => {
        detailsEl.removeAttribute('open');
      });
    });
  };

  const update = () => {
    const rawQuery = search.value || '';
    const hasQuery = normalize(rawQuery).length > 0;
    if (hasQuery) collapseAllOpenDetails();

    const searchScores = buildSearchScores(rawQuery);
    const visibleCards = [];

    cards.forEach((card) => {
      const matchesSearch = !searchScores || searchScores.has(card);
      const visible = matchesFilter(card) && matchesSearch;
      card.classList.toggle('is-hidden', !visible);
      if (visible) visibleCards.push(card);
    });

    let usingFallbackTopSavings = false;
    if (hasQuery && visibleCards.length === 0) {
      cards.forEach((card) => {
        const showFallback = card.dataset.topSavings === 'true';
        card.classList.toggle('is-hidden', !showFallback);
        if (showFallback) visibleCards.push(card);
      });
      usingFallbackTopSavings = visibleCards.length > 0;
    }

    if (noResults) {
      const showNoResults = hasQuery && usingFallbackTopSavings;
      noResults.classList.toggle('is-hidden', !showNoResults);
      if (noResultsHint) noResultsHint.classList.toggle('is-hidden', !showNoResults);
      if (showNoResults && noResultsQuery) {
        noResultsQuery.textContent = rawQuery.trim();
      }
    }

    const sortedVisible = visibleCards.slice().sort((a, b) => {
      if (usingFallbackTopSavings) {
        const byDiscount = parseFloat(b.dataset.discount || '0') - parseFloat(a.dataset.discount || '0');
        if (byDiscount !== 0) return byDiscount;
      }
      if (searchScores) {
        const scoreA = searchScores.get(a) ?? 1;
        const scoreB = searchScores.get(b) ?? 1;
        if (scoreA !== scoreB) return scoreA - scoreB;
      }

      if (activeFilter === 'top-savings') {
        const byDiscount = parseFloat(b.dataset.discount || '0') - parseFloat(a.dataset.discount || '0');
        if (byDiscount !== 0) return byDiscount;
        return affiliateRank(b) - affiliateRank(a);
      }

      if (activeFilter === 'latest') {
        const byDate = parseInt(b.dataset.date || '0', 10) - parseInt(a.dataset.date || '0', 10);
        if (byDate !== 0) return byDate;
        return affiliateRank(b) - affiliateRank(a);
      }

      const byDiscount = parseFloat(b.dataset.discount || '0') - parseFloat(a.dataset.discount || '0');
      if (byDiscount !== 0) return byDiscount;
      const byDate = parseInt(b.dataset.date || '0', 10) - parseInt(a.dataset.date || '0', 10);
      if (byDate !== 0) return byDate;
      const byAffiliate = affiliateRank(b) - affiliateRank(a);
      if (byAffiliate !== 0) return byAffiliate;
      return parseInt(a.dataset.originalIndex || '0', 10) - parseInt(b.dataset.originalIndex || '0', 10);
    });

    cards.forEach((card) => {
      if (!card.classList.contains('is-hidden')) return;
      grid.appendChild(card);
    });
    sortedVisible.forEach((card) => {
      grid.appendChild(card);
    });

    const normalizedQuery = normalize(rawQuery);
    const current = (url.searchParams.get('q') || '').trim().toLowerCase();
    if (normalizedQuery) {
      if (current !== normalizedQuery) {
        url.searchParams.set('q', normalizedQuery);
        window.history.replaceState({}, '', `${url.pathname}?${url.searchParams.toString()}${url.hash}`);
      }
    } else if (url.searchParams.has('q')) {
      url.searchParams.delete('q');
      const queryString = url.searchParams.toString();
      window.history.replaceState({}, '', `${url.pathname}${queryString ? `?${queryString}` : ''}${url.hash}`);
    }
  };

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      activeFilter = tab.dataset.filter;
      tabs.forEach((item) => item.classList.toggle('is-active', item === tab));
      update();
    });
  });

  search.addEventListener('input', update);
  update();
});
