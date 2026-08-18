/*
 * Live "what will this do" panel for the propose-listing form, plus the per-field
 * matches-existing / is-new badges.
 *
 * The form asks a submitter to describe hardware and then says nothing about the
 * consequences. Whether a name they typed joins an existing catalog entry or mints a
 * near-duplicate, whether ticking AlmaLinux 9 adds a confirmation or restates one already
 * recorded, whether a component is about to be created: all of it was knowable and none of
 * it was shown. This computes the net effect as they type.
 *
 * Server-side data only, from `#submission-baseline`. The browser cannot know what the
 * catalog holds, and re-deriving any of it here would be a second implementation of rules
 * that live in `results/services.py`.
 *
 * The logic is a handful of pure functions so `tests/js/summary_check.js` can exercise it
 * under node with no DOM, the same arrangement `combobox.js` uses.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;                 // node, for the checks
  } else {
    root.SubmissionSummary = api;
    if (typeof document !== "undefined") {
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", api.init);
      } else {
        api.init();
      }
    }
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const fold = (value) => (value || "").trim().toLowerCase();

  /*
   * Whether a typed value already names something in the catalog.
   *
   * Case- and space-insensitive, because "dell inc." and "Dell Inc." are the same company
   * and a submitter should not create a second one by capitalising differently. Deliberately
   * exact beyond that: "PowerEdge R750" must not report itself as matching "PowerEdge
   * R750xd", which is a different machine.
   */
  function matchesKnown(value, known) {
    const wanted = fold(value);
    if (!wanted) return false;
    return (known || []).some((entry) => fold(entry) === wanted);
  }

  /*
   * Before/after for one text field.
   *
   * `null` when there is nothing to say - either the value is untouched or there is no
   * listing yet to compare against - so the caller can drop the row rather than print
   * "unchanged" nine times.
   */
  function fieldChange(label, before, after) {
    const from = (before || "").trim();
    const to = (after || "").trim();
    if (from === to) return null;
    if (!to) return { label, kind: "cleared", from, to };
    if (!from) return { label, kind: "added", from, to };
    return { label, kind: "changed", from, to };
  }

  /*
   * What approving does to one AlmaLinux release.
   *
   * Three outcomes worth distinguishing, and the reason this is not just "adds support":
   *
   *  - `new`      the listing does not claim this major at all yet.
   *  - `attest`   already claimed, but not by this person, so approving adds a
   *               confirmation.
   *  - `noop`     already claimed and already confirmed by them. Nothing changes, and
   *               saying so is more useful than an encouraging lie.
   *
   * There was a fourth, `widen`, for a run lowering the per-major minor floor. Hardware
   * certifies per major now, so a claim has no floor to broaden.
   */
  function releaseEffect(major, existing, alreadyMine) {
    if (!existing) return { major, kind: "new" };
    if (alreadyMine) return { major, kind: "noop" };
    return { major, kind: "attest" };
  }

  /*
   * The whole summary, as data. Rendering is separate so the shape can be asserted
   * without a DOM.
   */
  function summarize(baseline, values) {
    const listing = baseline.listing;
    const lines = [];

    if (!listing) {
      lines.push({
        tone: "new",
        text: `Creates a new catalog listing: ${values.vendor_name || "?"} ` +
              `${values.name || "?"}`,
      });
    } else {
      lines.push({
        tone: "info",
        text: `Adds evidence to the existing listing ${listing.label}.`,
      });
      const fields = [
        ["Name", listing.name, values.name],
        ["Model number", listing.model_number, values.model_number],
        ["Description", listing.description, values.description],
        ["Spec sheet URL", listing.vendor_spec_url, values.vendor_spec_url],
      ];
      fields.forEach(([label, before, after]) => {
        if (after === undefined) return;      // field not on this form
        const change = fieldChange(label, before, after);
        if (change) lines.push({ tone: "change", change });
      });
    }

    (values.releases || []).forEach((entry) => {
      const existing = (baseline.versions || {})[String(entry.major)];
      lines.push({
        tone: "release",
        effect: releaseEffect(entry.major, existing, existing && existing.mine),
      });
    });

    (baseline.components || []).forEach((component) => {
      if ((values.excluded || []).indexOf(component.key) !== -1) {
        lines.push({
          tone: "excluded",
          text: `${component.kind_label} ${component.label} will not be added.`,
        });
      } else if (component.will_create) {
        lines.push({
          tone: "new",
          text: `Creates a new ${component.kind_label.toLowerCase()} entry: ` +
                `${component.label}` +
                (component.new_vendor ? ", and a new manufacturer for it" : ""),
        });
      } else {
        lines.push({
          tone: "match",
          text: `${component.kind_label} ${component.label} attaches to ` +
                `${component.matches}.`,
        });
      }
    });

    return lines;
  }

  // --- DOM wiring ---------------------------------------------------------------

  const RELEASE_PREFIX = "release_";
  // Legacy blobs and, until every form is redeployed, legacy markup can still carry
  // `release_minor_*` inputs. They are not release ticks and never were, which is why this
  // prefix has to stay known even though nothing reads its value any more.
  const MINOR_PREFIX = "release_minor_";

  function readValues(form, baseline) {
    const value = (name) => {
      const field = form.elements[name];
      return field ? field.value : undefined;
    };
    const releases = [];
    Array.prototype.forEach.call(
      form.querySelectorAll(`input[type=checkbox][name^="${RELEASE_PREFIX}"]`),
      (box) => {
        if (box.name.indexOf(MINOR_PREFIX) === 0) return;
        if (!box.checked) return;
        releases.push({
          major: parseInt(box.name.slice(RELEASE_PREFIX.length), 10),
        });
      },
    );
    // The boxes are an include list: ticked means "add this". So the exclusions are the
    // ones left unticked, which is also why this cannot just read :checked.
    const excluded = Array.prototype.map.call(
      form.querySelectorAll('input[name="included_ties"]:not(:checked)'),
      (box) => box.value,
    );
    return {
      vendor_name: value("vendor_name"),
      name: value("name"),
      model_number: value("model_number"),
      description: value("description"),
      vendor_spec_url: value("vendor_spec_url"),
      releases: releases.sort((a, b) => b.major - a.major),
      excluded,
    };
  }

  const TONE_CLASS = {
    new: "text-warning",
    match: "text-success",
    info: "text-secondary",
    excluded: "text-secondary",
  };

  function releaseText(effect) {
    const name = `AlmaLinux ${effect.major}`;
    switch (effect.kind) {
      case "new":
        return [`Adds ${name} to this listing, confirmed by you.`, "text-warning"];
      case "attest":
        return [`Adds your confirmation to ${name}.`, "text-success"];
      default:
        return [`${name} is already claimed and already confirmed by you - no change.`,
                "text-secondary"];
    }
  }

  function render(body, lines) {
    body.textContent = "";
    const list = document.createElement("ul");
    list.className = "list-unstyled mb-0";
    lines.forEach((line) => {
      const item = document.createElement("li");
      item.className = "mb-1";
      if (line.change) {
        const { label, kind, from, to } = line.change;
        item.className += " small";
        if (kind === "added") {
          item.textContent = `${label}: sets "${to}" (was empty).`;
        } else if (kind === "cleared") {
          item.textContent = `${label}: leaves "${from}" as it is (blank means no change).`;
        } else {
          item.textContent = `${label}: "${from}" becomes "${to}".`;
        }
      } else if (line.effect) {
        const [text, cls] = releaseText(line.effect);
        item.className += ` small ${cls}`;
        item.textContent = text;
      } else {
        item.className += ` small ${TONE_CLASS[line.tone] || ""}`;
        item.textContent = line.text;
      }
      list.appendChild(item);
    });
    body.appendChild(list);
  }

  /*
   * The inline badge beside a free-text identity field.
   *
   * The text is live, because the answer changes with every keystroke and a badge that
   * went stale would be worse than none. The *element* is not ours: the template renders
   * one per field and this only ever sets its text and class.
   *
   * It used to create the span itself, appended to ``input.parentNode``, and that was a
   * bug. ``combobox.js`` moves the input into a ``.combobox-wrap`` div after the page
   * loads, so the parent changed under us between refreshes: the first badge was orphaned
   * in the old container reading "matches an existing catalog entry" while a second was
   * created in the new one reading "new - will be created". Both were on screen at once,
   * which is exactly as confusing as it sounds.
   */
  function badgeFor(form, fieldName, known) {
    const input = form.elements[fieldName];
    const badge = form.querySelector(`[data-match-badge="${fieldName}"]`);
    if (!input || !badge) return;
    const value = (input.value || "").trim();
    if (!value) {
      badge.className = "small";
      badge.textContent = "";
      return;
    }
    if (matchesKnown(value, known)) {
      badge.className = "small ms-1 text-success";
      badge.textContent = "matches an existing catalog entry";
    } else {
      badge.className = "small ms-1 text-warning";
      badge.textContent = "new - will be created";
    }
  }

  function init() {
    const payload = document.getElementById("submission-baseline");
    const card = document.querySelector("[data-summary]");
    if (!payload || !card) return;
    let baseline;
    try {
      baseline = JSON.parse(payload.textContent);
    } catch (err) {
      return;                              // nothing useful to show; leave it hidden
    }
    const form = card.closest("form");
    const body = card.querySelector("[data-summary-body]");
    if (!form || !body) return;

    const badges = [
      ["vendor_name", (baseline.known || {}).vendor],
      ["name", (baseline.known || {}).system],
      ["model_number", null],
    ];

    const refresh = () => {
      render(body, summarize(baseline, readValues(form, baseline)));
      badges.forEach(([fieldName, known]) => {
        if (known) badgeFor(form, fieldName, known);
      });
    };

    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);
    card.classList.remove("d-none");
    refresh();
  }

  return { init, matchesKnown, fieldChange, releaseEffect, summarize, readValues };
});
