document.addEventListener('DOMContentLoaded', () => {
  const rows = Array.from(document.querySelectorAll('[data-home-filter-row]'));
  if (rows.length === 0) return;
  const mobileQuery = window.matchMedia('(max-width: 54rem)');

  rows.forEach((row) => {
    const toggle = row.querySelector('[data-home-filter-search-toggle]');
    const form = row.querySelector('[data-home-filter-search-form]');
    const input = row.querySelector('[data-home-filter-search-input]');
    const submit = row.querySelector('.home-filter-rail__search-submit');
    const close = row.querySelector('[data-home-filter-search-close]');

    if (!toggle || !form || !input || !submit || !close) return;

    const updateMobileSubmitLabel = () => {
      submit.textContent = 'Go';
      submit.setAttribute('aria-label', 'Search deals');
    };

    const openSearch = () => {
      row.classList.add('is-searching');
      toggle.setAttribute('aria-expanded', 'true');
      updateMobileSubmitLabel();
      window.requestAnimationFrame(() => {
        input.focus();
        input.select();
      });
    };

    const closeSearch = () => {
      row.classList.remove('is-searching');
      toggle.setAttribute('aria-expanded', 'false');
      updateMobileSubmitLabel();
    };

    toggle.addEventListener('click', () => {
      if (row.classList.contains('is-searching')) {
        closeSearch();
      } else {
        openSearch();
      }
    });

    const openFromInput = () => {
      if (!mobileQuery.matches) return;
      if (row.classList.contains('is-searching')) return;
      openSearch();
    };

    input.addEventListener('focus', openFromInput);
    input.addEventListener('pointerdown', openFromInput);

    close.addEventListener('click', () => {
      closeSearch();
      toggle.focus();
    });

    form.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeSearch();
        toggle.focus();
      }
    });

    document.addEventListener('click', (event) => {
      if (!row.classList.contains('is-searching')) return;
      if (row.contains(event.target)) return;
      closeSearch();
    });

    if (mobileQuery.addEventListener) {
      mobileQuery.addEventListener('change', updateMobileSubmitLabel);
    } else if (mobileQuery.addListener) {
      mobileQuery.addListener(updateMobileSubmitLabel);
    }

    updateMobileSubmitLabel();
  });
});
