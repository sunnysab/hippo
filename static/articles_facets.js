(() => {
  const normalizeFacetValue = (value) => String(value ?? '');

  const buildArticleFacetVisibility = (options = {}) => {
    const items = Array.isArray(options.items) ? options.items.filter(Boolean) : [];
    const activeValue = normalizeFacetValue(options.activeValue);
    const expanded = Boolean(options.expanded);
    const parsedLimit = Number(options.collapsedLimit);
    const collapsedLimit = Number.isFinite(parsedLimit) ? Math.max(1, Math.trunc(parsedLimit)) : 5;
    const isCollapsible = items.length > collapsedLimit;

    if (!isCollapsible || expanded) {
      return {
        visibleItems: items,
        hiddenCount: 0,
        isCollapsible,
      };
    }

    let visibleItems = items.slice(0, collapsedLimit);
    const activeIndex = items.findIndex((item) => normalizeFacetValue(item.value) === activeValue);

    if (activeIndex >= collapsedLimit) {
      visibleItems = [
        ...items.slice(0, Math.max(collapsedLimit - 1, 0)),
        items[activeIndex],
      ];
    }

    return {
      visibleItems,
      hiddenCount: items.length - visibleItems.length,
      isCollapsible,
    };
  };

  window.HippoArticleFacets = {
    ...(window.HippoArticleFacets || {}),
    buildArticleFacetVisibility,
  };
})();
