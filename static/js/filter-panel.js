/**
 * Filter-panel interactions for the catalog sidebar.
 *
 * Three behaviors, each keyed off data-attributes already in the HTML:
 *
 * 1. **Collapse after N items**: `.filter-values[data-collapsed-limit]` hides
 *    items beyond the limit and shows a "Show N more / Show less" toggle.
 * 2. **Search within a category**: `input[data-category-search]` filters the
 *    visible checkboxes on keyup by matching label text.
 * 3. **Collapsible category cards**: clicking a `.filter-group-title` toggles
 *    the body of its card so users can shrink sidebar real-estate.
 * 4. **Submit on change**: a `select[data-submit-on-change]` submits its own form, for a control
 *    whose value changes more of the page than an HTMX partial covers.
 *
 * Runs once on DOMContentLoaded. The filter panel is NOT swapped by HTMX
 * (only #results is), so this script's state persists across filter changes.
 */
document.addEventListener("DOMContentLoaded", () => {
  // --- 0. Enter in a search box must not submit the filter form ----------
  // Every search box in the panel sits *inside* the filter form, whose own
  // hx-trigger includes "submit". Pressing Enter therefore fired a form
  // submission on top of the search, which navigated away from the page - the
  // vendor box could land you on its own fragment endpoint, rendering
  // "No vendor matches that name" as an entire document.
  //
  // Nothing is lost by swallowing it: these boxes search as you type, so Enter
  // has no work left to do.
  document.querySelectorAll("form input[type='search']").forEach(input => {
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") event.preventDefault();
    });
  });

  // --- 1. Collapse after N items -----------------------------------------
  document.querySelectorAll(".filter-values[data-collapsed-limit]").forEach(list => {
    const limit = parseInt(list.dataset.collapsedLimit, 10) || 10;
    const items = Array.from(list.children);
    if (items.length <= limit) return;

    const extra = items.slice(limit);
    extra.forEach(el => el.hidden = true);

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "btn btn-link btn-sm p-0 mt-1 filter-expand-toggle";
    toggle.textContent = `Show ${extra.length} more…`;
    let expanded = false;

    toggle.addEventListener("click", () => {
      expanded = !expanded;
      extra.forEach(el => el.hidden = !expanded);
      toggle.textContent = expanded
        ? "Show less"
        : `Show ${extra.length} more…`;
    });

    list.after(toggle);
  });

  // --- 2. Search within a category ---------------------------------------
  document.querySelectorAll("input[data-category-search]").forEach(input => {
    const card = input.closest("[data-category]");
    if (!card) return;
    const items = card.querySelectorAll(".filter-values > *");
    const toggle = card.querySelector(".filter-expand-toggle");

    input.addEventListener("input", () => {
      const q = input.value.trim().toLowerCase();

      // While searching, force-expand everything so matches deep in the
      // list are visible; collapse state is restored when the search clears.
      if (q) {
        items.forEach(el => {
          const label = el.textContent || "";
          el.hidden = !label.toLowerCase().includes(q);
        });
        if (toggle) toggle.hidden = true;
      } else {
        // Clear search: restore the collapsed-limit behavior.
        const limit = parseInt(
          card.querySelector(".filter-values")?.dataset.collapsedLimit || "999",
          10,
        );
        items.forEach((el, i) => {
          el.hidden = i >= limit;
        });
        if (toggle) {
          toggle.hidden = false;
          toggle.textContent = `Show ${Math.max(0, items.length - limit)} more…`;
        }
      }
    });
  });

  // --- 4. Submit on change -----------------------------------------------
  //
  // For a control that changes the *question*, not just the answer. The comparison page's "Compare
  // CPUs / GPUs" selector sat in an HTMX form targeting only the results table, so switching to
  // GPUs re-rendered the table and left the model picker full of CPUs - reported as GPUs not being
  // offered at all. Its options come from the server per kind, so the whole page has to come back,
  // and that also clears a selection of keys that mean nothing under the new kind.
  //
  // A plain form submit rather than anything HTMX: this is a navigation. With JavaScript off the
  // form's own button does the same thing, which is why the markup keeps one.
  document.querySelectorAll("select[data-submit-on-change]").forEach(select => {
    select.addEventListener("change", () => {
      const form = select.form;
      if (!form) return;
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    });
  });

  // --- 3. Collapsible category cards -------------------------------------
  document.querySelectorAll(".filter-group-title").forEach(title => {
    const card = title.closest("[data-category]");
    if (!card) return;

    // Wrap the collapsible content (everything after the title) so we can
    // toggle it as a unit. It's already there as siblings of the title.
    const body = card.querySelector(".card-body");
    if (!body) return;

    // Collect children after the title element.
    const collapsible = Array.from(body.children).filter(el => el !== title);

    title.style.cursor = "pointer";
    title.style.userSelect = "none";

    // Chevron indicator.
    const chevron = document.createElement("span");
    chevron.className = "float-end";
    chevron.textContent = "▾";
    title.appendChild(chevron);

    let open = true;
    title.addEventListener("click", () => {
      open = !open;
      collapsible.forEach(el => el.hidden = !open);
      chevron.textContent = open ? "▾" : "▸";
    });
  });
});
