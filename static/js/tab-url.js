// Sync the active Bootstrap tab to the URL, so a tab is somewhere you can link to and come back to.
// A nav marked `data-tab-url` opens the tab named in `?tab=<slug>` on load, and rewrites that one
// query parameter whenever a tab is shown. The slug is the target pane's id with any leading `tab-`
// stripped (so `#tab-vendors` is `?tab=vendors`); a trigger may override it with `data-tab-slug`.
// The parameter name is the value of `data-tab-url`, defaulting to `tab`.
//
// history.replaceState rather than pushState, so clicking through tabs does not pile up history
// entries the Back button then has to walk out of; the URL stays copyable either way. The rest of
// the query string is left untouched.
//
// Progressive enhancement over Bootstrap's tabs, which already need JavaScript to switch at all:
// with the bundle loaded, this only decides which tab starts open and keeps the URL in step. A
// strict Content-Security-Policy forbids inline handlers, which is why this is a file and not an
// `onshown` attribute.
document.addEventListener("DOMContentLoaded", () => {
  const navs = document.querySelectorAll("[data-tab-url]");
  if (!navs.length || typeof bootstrap === "undefined" || !bootstrap.Tab) return;

  navs.forEach(nav => {
    const param = nav.getAttribute("data-tab-url") || "tab";
    const triggers = Array.from(nav.querySelectorAll('[data-bs-toggle="tab"]'));
    const slugOf = btn =>
      btn.getAttribute("data-tab-slug") ||
      (btn.getAttribute("data-bs-target") || "").replace(/^#/, "").replace(/^tab-/, "");

    // Open the tab the URL asks for, if this nav has one by that name. An unknown or absent slug
    // leaves the tab the markup marked active, so the default needs no `?tab=` to be reachable.
    const wanted = new URLSearchParams(location.search).get(param);
    if (wanted) {
      const match = triggers.find(btn => slugOf(btn) === wanted);
      if (match) bootstrap.Tab.getOrCreateInstance(match).show();
    }

    triggers.forEach(btn => {
      btn.addEventListener("shown.bs.tab", () => {
        const slug = slugOf(btn);
        if (!slug) return;
        const url = new URL(location.href);
        url.searchParams.set(param, slug);
        history.replaceState(history.state, "", url);
      });
    });
  });
});
