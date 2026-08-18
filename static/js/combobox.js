/**
 * Combobox upgrade for free-text fields that have a list of known values.
 *
 * Server-rendered markup is an `<input data-combobox list="...">` plus its
 * `<datalist>`, which already gives free text and native matching with no
 * JavaScript. This takes over to fix what the native control does badly:
 *
 * 1. **Substring matching.** Browsers vary and some only match a prefix, so
 *    typing "6430" would not find "Intel Xeon Gold 6430".
 * 2. **Showing the list on focus**, so a submitter discovers "Dell Inc."
 *    already exists rather than typing "Dell" and forking the catalog.
 * 3. **Similarity matching**, which is the reason this matters here. These
 *    fields arrive prefilled with whatever the hardware reported, and that is
 *    frequently not the catalog's spelling of the same thing: lscpu says
 *    "Intel(R) Xeon(R) Gold 6430" where the catalog has "Xeon Gold 6430", DMI
 *    says "Dell" where the vendor is "Dell Inc.". A literal substring test
 *    finds neither, so a field full of a weird vendor string would show no
 *    suggestions at all - exactly when suggestions are most useful.
 *
 * Similarity is deliberately generous but ranked: exact and containment
 * matches first, then shared words, then character overlap for typos. Anything
 * that only matched fuzzily is labeled "similar", because a suggestion that
 * does not contain what you typed needs to look like a suggestion rather than
 * a match.
 *
 * The `list` attribute and the datalist are removed once this runs, so the
 * native popup and this one never appear together. Free text is always
 * accepted: hardware the catalog has never seen has to be typeable.
 */

/** Lowercase, drop punctuation, collapse runs of spaces.
 *
 * This is what lets "Intel(R) Xeon(R) Gold 6430" and "Xeon Gold 6430" be
 * compared at all: the trademark marks, brackets and commas that vendors put
 * in product strings carry no meaning for matching. "Dell, Inc." and
 * "Dell Inc." normalize to the same thing.
 */
function comboNormalize(value) {
  return (value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

/** Sorensen-Dice coefficient over character bigrams: 0 (nothing) to 1 (same).
 *
 * Catches near-misses no word or substring test can - a transposed digit, a
 * missing hyphen, "Riptide" against "Rip tide".
 */
function comboDice(a, b) {
  if (a === b) return 1;
  if (a.length < 2 || b.length < 2) return 0;
  const counts = new Map();
  for (let i = 0; i < a.length - 1; i++) {
    const pair = a.slice(i, i + 2);
    counts.set(pair, (counts.get(pair) || 0) + 1);
  }
  let hits = 0;
  for (let i = 0; i < b.length - 1; i++) {
    const pair = b.slice(i, i + 2);
    const left = counts.get(pair);
    if (left) {
      counts.set(pair, left - 1);
      hits += 1;
    }
  }
  return (2 * hits) / (a.length + b.length - 2);
}

/** First letters of a multi-word string: "hewlett packard enterprise" -> "hpe".
 *
 * Vendors are routinely reported as an acronym in one place and spelled out in
 * the other, and an acronym shares no words and almost no character pairs with
 * its expansion, so nothing else here would connect them.
 */
function comboInitials(normalized) {
  const words = normalized.split(" ").filter(Boolean);
  return words.length > 1 ? words.map(word => word[0]).join("") : "";
}

/** How well `option` answers `query`, 0 to 1. See comboRank for the tiers. */
function comboSimilarity(query, option) {
  const q = comboNormalize(query);
  const o = comboNormalize(option);
  if (!q || !o) return 0;
  if (q === o) return 1;

  let best = 0;
  if (o.startsWith(q)) best = 0.95;
  else if (o.includes(q)) best = 0.9;
  // The option sits inside what was typed. This is the collector's case:
  // "xeon gold 6430" is inside "intel r xeon r gold 6430".
  else if (q.includes(o)) best = 0.85;

  // Acronyms, in either direction: "HPE" against the spelled-out vendor, or
  // the spelled-out vendor against a catalog entry that is the acronym.
  const compact = text => text.replace(/ /g, "");
  if (comboInitials(o) === compact(q) || comboInitials(q) === compact(o)) {
    best = Math.max(best, 0.8);
  }

  const qWords = q.split(" ").filter(Boolean);
  const oWords = new Set(o.split(" ").filter(Boolean));
  const shared = qWords.filter(word => oWords.has(word)).length;
  if (shared) {
    // Scaled by how much of the query the option explains, so "Xeon Gold 6430"
    // beats "Xeon Gold 5420" for a 6430 query without either being excluded.
    best = Math.max(best, 0.45 + 0.35 * (shared / qWords.length));
  }

  return Math.max(best, 0.8 * comboDice(q, o));
}

/** Options worth offering for `query`, best first.
 *
 * An empty query offers everything, which is how someone browsing discovers
 * what already exists.
 */
function comboRank(query, options, floor = 0.34) {
  if (!comboNormalize(query)) {
    return options.map(value => ({ value, score: 1, literal: true }));
  }
  return options
    .map(value => ({
      value,
      score: comboSimilarity(query, value),
      // A containment match is what the user typed; anything weaker is a
      // guess at what they meant, and gets labeled as one.
      literal: comboSimilarity(query, value) >= 0.85,
    }))
    .filter(hit => hit.score >= floor)
    .sort((a, b) => b.score - a.score || a.value.localeCompare(b.value));
}

/**
 * Which entries a menu shows: the ranked matches, capped, then the pinned ones.
 *
 * Pure and exported so tests/js/combobox_check.js can exercise it without a DOM,
 * which is the same reason the ranking functions above are shaped this way. The rule
 * it encodes is easy to state and easy to get wrong: a cap may truncate *choices*,
 * because that is what searching is for, but it must never truncate an *action*.
 * "+ Propose a new vendor…" is the last option in the publisher pickers, and an empty
 * query preserves the original order, so before pinning it fell off the bottom as
 * soon as a thirteenth vendor existed.
 *
 * @param query        what the user typed
 * @param options      [{value, label}], in the order the server sent them
 * @param pinnedValues option values that must always appear
 * @param max          cap on the ranked (unpinned) entries
 */
function comboVisible(query, options, pinnedValues = [], max = 12) {
  const pinned = pinnedValues
    .map(value => options.find(option => option.value === value))
    .filter(Boolean);
  const labels = options.map(option => option.label);
  const ranked = comboRank(query, labels)
    .map(hit => options.find(option => option.label === hit.value))
    .filter(option => option && !pinned.includes(option))
    .slice(0, max);
  return ranked.concat(pinned);
}

/**
 * The class a search input should wear, given the class of the select it replaces.
 *
 * Bootstrap's ``.form-select`` paints a dropdown caret and reserves padding for it,
 * so copying the select's class straight across made the search box look exactly like
 * a native dropdown - which is why nobody could tell it was typeable. Size modifiers
 * are translated rather than dropped, because losing ``-sm`` inside a dense table
 * would be its own bug.
 *
 * Pure and exported for tests/js/combobox_check.js, like comboRank and comboVisible.
 */
function comboInputClass(selectClass) {
  const swaps = {
    "form-select": "form-control",
    "form-select-sm": "form-control-sm",
    "form-select-lg": "form-control-lg",
  };
  const classes = (selectClass || "")
    .split(/\s+/)
    .filter(Boolean)
    .map(cls => swaps[cls] || cls);
  if (!classes.some(cls => /^form-control(-sm|-lg)?$/.test(cls))) {
    classes.push("form-control");
  }
  if (!classes.includes("combobox-input")) classes.push("combobox-input");
  return classes.join(" ");
}

// Guarded so the ranking functions above can be required by the node check in
// tests/js/, which has no DOM.
if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", () => {
  const MAX_SHOWN = 12;

  document.querySelectorAll("input[data-combobox]").forEach(setup);
  document.querySelectorAll("select[data-combobox]").forEach(setupSelect);
  document.querySelectorAll("select[data-picker]").forEach(setupPicker);

  /** Take the user to whatever a pinned option unlocks.
   *
   * Scrolls the target into view and focuses it. Without this, picking
   * "+ Add a new publisher" set a hidden select's value and closed the menu, so from
   * the submitter's side nothing happened at all - the fields it enables live in a
   * separate card, and the only hint was a sentence inside that card telling you to
   * have already done this.
   *
   * A no-op when the select names no target or the selector matches nothing, so the
   * attribute stays optional and a stale selector degrades to the old behaviour
   * rather than throwing.
   */
  function followPin(select) {
    const selector = select.dataset.comboboxPinTarget;
    if (!selector) return;
    const target = document.querySelector(selector);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    // After the scroll starts, so the focus ring is visible when it lands.
    setTimeout(() => target.focus({ preventScroll: true }), 150);
  }

  /** Shared dropdown shell: a menu positioned under `anchor`. */
  function makeMenu(anchor) {
    const wrap = document.createElement("div");
    wrap.className = "combobox-wrap";
    anchor.parentNode.insertBefore(wrap, anchor);
    wrap.appendChild(anchor);
    const menu = document.createElement("div");
    menu.className = "dropdown-menu combobox-menu";
    menu.setAttribute("role", "listbox");
    wrap.appendChild(menu);
    return { wrap, menu };
  }

  /** A <select> the reviewer has to pick a real entry from.
   *
   * The select keeps the value, so server-side validation is untouched and the
   * field still works with JavaScript off; this only replaces the picking. A
   * catalog select runs to hundreds of options, where a native dropdown means
   * scrolling and the browser's prefix-only type-ahead.
   */
  function setupSelect(select) {
    const options = Array.from(select.options)
      .filter(option => option.value)
      .map(option => ({ value: option.value, label: option.textContent.trim() }));
    if (!options.length) return;

    // Values that must appear in the menu whatever the query, and whatever the
    // MAX_SHOWN cap does to the rest. Named on the select as
    // ``data-combobox-pin="__new__,..."``.
    //
    // This exists because the cap silently hides an *action*. The publisher pickers
    // end their option list with "+ Propose a new vendor…", and an empty query keeps
    // the original order, so past twelve vendors that entry falls off the bottom and
    // the only way to reach the inline-vendor flow is to guess that typing "propose"
    // finds it. A list of ordinary choices being truncated is fine - that is what
    // searching is for. An escape hatch being truncated is not.
    const pinnedValues = (select.dataset.comboboxPin || "")
      .split(",").map(value => value.trim()).filter(Boolean);
    const pinned = pinnedValues
      .map(value => options.find(option => option.value === value))
      .filter(Boolean);

    const input = document.createElement("input");
    // ``search``, not ``text``: browsers give it a clear affordance and a native clear
    // button, and it is what the control actually is.
    input.type = "search";
    // Translated, not copied. Copying ``select.className`` gave the input Bootstrap's
    // ``.form-select``, which paints a dropdown caret and the padding to clear it - so
    // the search box looked exactly like a native dropdown and nobody could tell it
    // was typeable. Size modifiers are carried across, because losing ``-sm`` inside a
    // dense table would be its own bug.
    input.className = comboInputClass(select.className);
    input.setAttribute("role", "combobox");
    input.setAttribute("autocomplete", "off");
    input.placeholder = select.dataset.placeholder || "Search…";
    const chosen = options.find(option => option.value === select.value);
    if (chosen) input.value = chosen.label;

    select.classList.add("visually-hidden");
    select.setAttribute("tabindex", "-1");
    select.insertAdjacentElement("afterend", input);
    const { menu } = makeMenu(input);

    let shown = [];
    let active = -1;

    function open(yes) {
      menu.classList.toggle("show", yes);
      if (!yes) active = -1;
    }

    function render() {
      // Match on the label, but only when it differs from the current
      // selection, so focusing a filled field lists everything again.
      const query = input.value === (chosen ? chosen.label : "") ? "" : input.value;
      shown = comboVisible(query, options, pinnedValues, MAX_SHOWN);
      const isPinned = option => pinned.includes(option);
      menu.replaceChildren();
      shown.forEach((option, index) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "dropdown-item"
          + (index === active ? " active" : "")
          + (isPinned(option) ? " combobox-pinned" : "");
        item.setAttribute("role", "option");
        item.textContent = option.label;
        item.addEventListener("mousedown", event => {
          event.preventDefault();
          select.value = option.value;
          input.value = option.label;
          select.dispatchEvent(new Event("change", { bubbles: true }));
          open(false);
          // Choosing an *action* should visibly do something. "+ Add a new publisher"
          // used to set a value and close the menu, leaving the page looking
          // unchanged while the fields it unlocked sat in a card further down that
          // nobody scrolled to. ``data-combobox-pin-target`` names where to go.
          if (isPinned(option)) followPin(select);
        });
        menu.appendChild(item);
      });
      open(shown.length > 0);
    }

    input.addEventListener("focus", () => { input.select(); render(); });
    input.addEventListener("input", () => { active = -1; render(); });
    input.addEventListener("blur", () => setTimeout(() => open(false), 120));
    input.addEventListener("keydown", event => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!shown.length) { render(); return; }
        active = (active + (event.key === "ArrowDown" ? 1 : -1) + shown.length)
          % shown.length;
        render();
      } else if (event.key === "Enter" && active >= 0) {
        event.preventDefault();
        menu.children[active].dispatchEvent(new Event("mousedown"));
      } else if (event.key === "Escape") {
        open(false);
      } else if (event.key === "Backspace" && !input.value) {
        select.value = "";     // clearing the box clears the choice
      }
    });
  }

  /** Search-and-add list for a multi-select.
   *
   * A scrolling multi-select hid what was already attached and made ctrl-click
   * the only way to add a second entry. This shows each attachment as its own
   * removable row and adds one at a time, which is also how the proposed and
   * already-linked components become visible rather than being a highlighted
   * region somewhere in a list.
   */
  /* Search-and-add over a <select multiple>. A native multi-select is fine for
   * a handful of options and unusable for a catalog: scrolling a list of
   * thousands of CPU models to ctrl-click two of them is not a picker. The
   * <select> stays in the DOM as the source of truth for the request, hidden.
   *
   * Configurable per instance, because it serves both the reviewer's component
   * linking and the hardware comparison:
   *   data-picker-placeholder  search field placeholder
   *   data-picker-empty        text shown when nothing is chosen
   *   data-picker-max          cap on selections (0 or absent = no cap)
   *   data-picker-ordered      first pick is meaningful; offer reordering
   */
  function setupPicker(select) {
    const options = Array.from(select.options).map((option, index) => ({
      value: option.value,
      label: option.textContent.trim(),
      selected: option.selected,
      // Selection order, which the DOM does not record. Server-side "the first
      // one is the baseline" needs it: a <select multiple> submits its values in
      // option order, so what a reader picked first was never what arrived
      // first. Pre-selected options keep their document order.
      order: option.selected ? index : -1,
    }));
    if (!options.length) return;

    const placeholder = select.dataset.pickerPlaceholder || "Search to add\u2026";
    const emptyText = select.dataset.pickerEmpty || "Nothing linked yet.";
    const max = parseInt(select.dataset.pickerMax || "0", 10) || 0;
    const ordered = select.hasAttribute("data-picker-ordered");
    let ticks = options.length;

    select.classList.add("visually-hidden");
    select.setAttribute("tabindex", "-1");

    const holder = document.createElement("div");
    const list = document.createElement("div");
    list.className = "list-group list-group-flush mb-2";
    const search = document.createElement("input");
    search.type = "text";
    search.className = "form-control";
    search.placeholder = placeholder;
    search.setAttribute("autocomplete", "off");
    const note = document.createElement("div");
    note.className = "form-text";
    holder.append(list, search, note);
    select.insertAdjacentElement("afterend", holder);
    const { menu } = makeMenu(search);

    function chosen() {
      return options
        .filter(option => option.selected)
        .sort((a, b) => a.order - b.order);
    }

    function sync(notify) {
      const picked = chosen();
      // Reorder the real options so the request carries selection order. The
      // alternative - a second hidden field - would leave two sources of truth.
      picked.forEach(entry => {
        const option = Array.from(select.options)
          .find(candidate => candidate.value === entry.value);
        if (option) select.appendChild(option);
      });
      Array.from(select.options).forEach(option => {
        const match = options.find(entry => entry.value === option.value);
        option.selected = Boolean(match && match.selected);
      });

      list.replaceChildren();
      if (!picked.length) {
        const empty = document.createElement("div");
        empty.className = "list-group-item text-secondary small px-0";
        empty.textContent = emptyText;
        list.appendChild(empty);
      }
      picked.forEach((option, index) => {
        const row = document.createElement("div");
        row.className =
          "list-group-item d-flex justify-content-between align-items-center gap-2 px-0";
        const name = document.createElement("span");
        name.textContent = option.label;
        if (ordered && index === 0) {
          const badge = document.createElement("span");
          badge.className = "badge text-bg-secondary ms-2";
          badge.textContent = "baseline";
          name.appendChild(badge);
        }
        const buttons = document.createElement("span");
        buttons.className = "d-flex gap-1";
        if (ordered && index > 0) {
          const promote = document.createElement("button");
          promote.type = "button";
          promote.className = "btn btn-sm btn-outline-secondary";
          promote.textContent = "Make baseline";
          promote.addEventListener("click", () => {
            option.order = -1;                 // sorts ahead of every other pick
            chosen().forEach((entry, position) => { entry.order = position; });
            sync(true);
          });
          buttons.appendChild(promote);
        }
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-sm btn-outline-danger";
        remove.textContent = "Remove";
        remove.addEventListener("click", () => {
          option.selected = false;
          sync(true);
        });
        buttons.appendChild(remove);
        row.append(name, buttons);
        list.appendChild(row);
      });

      note.textContent = max && picked.length >= max
        ? "Showing the maximum of " + max + ". Remove one to add another."
        : "";
      // Programmatic changes to a select fire no event, and a form that submits
      // on change would never see this. Skipped on the initial render so the
      // page does not request itself on load.
      if (notify) select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    let shown = [];

    function render() {
      if (max && chosen().length >= max) {
        menu.classList.remove("show");
        return;
      }
      const available = options.filter(option => !option.selected);
      shown = comboRank(search.value, available.map(option => option.label))
        .slice(0, MAX_SHOWN)
        .map(hit => available.find(option => option.label === hit.value));
      menu.replaceChildren();
      shown.forEach(option => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "dropdown-item";
        item.textContent = option.label;
        item.addEventListener("mousedown", event => {
          event.preventDefault();
          option.selected = true;
          option.order = ticks++;
          search.value = "";
          sync(true);
          // Stay open with the remaining options. Closing meant clicking out and
          // back in to add a second item, which is the whole task on a
          // comparison page. render() closes it by itself once the list is empty
          // or the cap is reached, so "open" here never means "open and useless".
          render();
          search.focus();
        });
        menu.appendChild(item);
      });
      menu.classList.toggle("show", shown.length > 0);
    }

    search.addEventListener("focus", render);
    search.addEventListener("input", render);
    search.addEventListener("blur", () => setTimeout(() => menu.classList.remove("show"), 120));
    search.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();          // never submit the form from here
        if (shown.length) menu.children[0].dispatchEvent(new Event("mousedown"));
      } else if (event.key === "Escape") {
        menu.classList.remove("show");
      }
    });
    sync(false);
  }

  function setup(input) {
    const listId = input.getAttribute("list");
    const datalist = listId ? document.getElementById(listId) : null;
    if (!datalist) return;

    const options = Array.from(datalist.options)
      .map(option => option.value)
      .filter(Boolean);
    input.removeAttribute("list");
    datalist.remove();
    if (!options.length) return;

    const wrap = document.createElement("div");
    wrap.className = "combobox-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    const menu = document.createElement("div");
    menu.className = "dropdown-menu combobox-menu";
    menu.setAttribute("role", "listbox");
    wrap.appendChild(menu);

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-autocomplete", "list");

    let shown = [];
    let active = -1;

    function open(yes) {
      menu.classList.toggle("show", yes);
      input.setAttribute("aria-expanded", yes ? "true" : "false");
      if (!yes) active = -1;
    }

    function choose(value) {
      input.value = value;
      open(false);
      // Let anything listening for a value change (HTMX, validation) see it.
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function render() {
      const all = comboRank(input.value, options);
      shown = all.slice(0, MAX_SHOWN);
      menu.replaceChildren();

      shown.forEach((hit, index) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "dropdown-item" + (index === active ? " active" : "");
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", index === active ? "true" : "false");
        item.textContent = hit.value;
        if (!hit.literal) {
          const tag = document.createElement("span");
          tag.className = "text-secondary small ms-2";
          tag.textContent = "similar";
          item.appendChild(tag);
        }
        // mousedown, not click: blur would close the menu first otherwise.
        item.addEventListener("mousedown", event => {
          event.preventDefault();
          choose(hit.value);
        });
        menu.appendChild(item);
      });

      if (all.length > shown.length) {
        const hint = document.createElement("div");
        hint.className = "dropdown-item disabled small text-secondary";
        hint.textContent = `${all.length - shown.length} more - keep typing`;
        menu.appendChild(hint);
      }

      open(shown.length > 0);
      if (active >= 0 && menu.children[active]) {
        menu.children[active].scrollIntoView({ block: "nearest" });
      }
    }

    input.addEventListener("input", () => {
      active = -1;
      render();
    });
    input.addEventListener("focus", render);
    // Delayed so a click on an item still lands.
    input.addEventListener("blur", () => setTimeout(() => open(false), 120));

    input.addEventListener("keydown", event => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!menu.classList.contains("show")) {
          render();
          return;
        }
        if (!shown.length) return;
        const step = event.key === "ArrowDown" ? 1 : -1;
        active = (active + step + shown.length) % shown.length;
        render();
      } else if (event.key === "Enter") {
        // Only intercept when a suggestion is highlighted, so Enter still
        // submits the form the rest of the time.
        if (menu.classList.contains("show") && active >= 0) {
          event.preventDefault();
          choose(shown[active].value);
        }
      } else if (event.key === "Escape") {
        open(false);
      }
    });
  }
});

// Exported for the node check in tests/js/; ignored by the browser.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    comboNormalize, comboDice, comboSimilarity, comboRank, comboVisible, comboInputClass,
  };
}
