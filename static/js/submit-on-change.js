// A `select[data-submit-on-change]` submits its own form when its value changes: a control that is
// really a navigation, filtering the page it sits on. Extracted so it can run on the admin pages
// too (filter-panel.js, which carries the same behavior, is a public-only bundle), and so the
// behavior is not an inline `onchange=` attribute, which a strict Content-Security-Policy forbids.
//
// With JavaScript off, the form's own submit button does the same thing, which is why the markup
// keeps one.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("select[data-submit-on-change]").forEach(select => {
    select.addEventListener("change", () => {
      const form = select.form;
      if (!form) return;
      if (typeof form.requestSubmit === "function") form.requestSubmit();
      else form.submit();
    });
  });
});
