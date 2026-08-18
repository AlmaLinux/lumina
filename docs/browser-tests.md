# Browser tests

Tests that drive a real browser over the real pages, in `tests/browser/`. They exist because this
project has repeatedly shipped broken interface while the whole server-side suite stayed green: in
every case the markup was correct and the rendering was not.

They run with everything else, on every `pytest`. They were gated behind a marker at first, which
is the usual arrangement and the wrong one here: a suite you have to remember to run separately
catches things after the fact. They add about half a minute.

On a machine with no browser installed they skip themselves with a sentence saying what to
install, so the ordinary suite still runs.

## Running them

```bash
sudo dnf install -y chromium          # AlmaLinux needs EPEL first: dnf install -y epel-release
pip install -e '.[dev,browser]'
python -m pytest
```

`pytest -m 'not browser'` skips them for a faster inner loop; `pytest -m browser` runs only them.

No separate settings module. `lumina.settings.test` puts its database in a file rather than in
memory, which costs nothing measurable and is what a live server needs: an in-memory SQLite
database is per connection, so with one the reference data the migrations insert disappeared after
the first test that loaded a page, and the request threads raced each other closing the one shared
connection and segfaulted the interpreter. Neither failure announced itself as a database problem.
The visible symptom was a page rendering perfectly against an empty catalog.

### Choosing the browser

Chromium is a system package everywhere, and Playwright is only the driver. Resolution order:

1. `LUMINA_BROWSER_EXECUTABLE`, if set.
2. `/usr/bin/chromium-browser`, `/usr/bin/chromium`, `/usr/bin/google-chrome`,
   `/usr/bin/google-chrome-stable`.
3. Playwright's own managed browser, for anyone who has run `playwright install`.

Nothing downloads a browser at test time.

### When one fails

A screenshot and the page's HTML land in `/tmp/lumina-browser/`, named after the test. Override the
directory with `LUMINA_BROWSER_ARTIFACTS`. The CI job uploads it as an artifact.

## In CI

The `test` job in `.github/workflows/ci.yml` runs in an `almalinux:10` container, installs
`chromium` from EPEL, and runs the whole suite including these. It sets `LUMINA_BROWSER_EXECUTABLE`
explicitly so a missing package fails the job rather than skipping the tests, `--shm-size=2g`
because Chromium cannot render a page of any size in a container's default 64 MB, and uploads the
failure artifacts.

`test-mariadb` passes `-m 'not browser'`. That job exists to answer a question about the database
engine, and nothing these assert depends on it.

## What is in here, and what is not

Worth a browser test:

- **Did the click reach the server.** The reviewer's Approve button is attached by `form=` to a
  form defined in another card. If that association breaks, the click makes no request at all: no
  navigation, no error, nothing in the log.
- **Computed layout.** Content escaping its container, a page that scrolls sideways, an element
  rendered at zero height.
- **CSS-only disclosure.** The `.reveal-*` controls are a hidden checkbox and a sibling selector.
  The fields are in the HTML in both states, so no response-body assertion can tell whether the
  label does anything. It once did nothing on one of the two layouts for exactly this reason.
- **Which form a control ended up in.** Decided by the parser after it repairs the markup, so
  `form form` matches nothing even on a page whose source really does nest them.
- **Icon glyphs.** The two layouts load different icon fonts and the wrong class is a blank space.

Not worth one, and deliberately still server-side: anything about status codes, redirects,
messages, permissions, or what a page says. Those are faster and clearer as ordinary tests, and
there are around 1900 of them.

## The recorded backlog

`tests/browser/test_every_page.py` visits every page once and checks it at three viewports. Nine
narrow-viewport combinations fail today, listed in `KNOWN_NARROW` with the offending element named
rather than excluded.

The list is strict in both directions. A new failure that is not on it fails the test, and a page
that stops failing also fails the test, with "remove it from KNOWN_NARROW". So it stays a to-do
list rather than becoming a record of what used to be broken.

Two causes between them: wide tables that do not scroll inside their column, because a grid column
defaults to `min-width: auto` and grows to fit its content, and the public navbar's link row not
collapsing until below 1024.
