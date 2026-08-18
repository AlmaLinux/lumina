# AGENTS instructions

- If you're not entirely sure how to use a library or module, look it up. Find the source code,
  docs, whatever. It's ok to ask the user for a link to the documentation or code if needed.
  - Django source code: githubRepo `django/django`
- Add clean code with sensible comments. Consider what you're implementing and the context, as well
  as how you can generalize functions to reduce code duplication and sources of bugs.
- If you can reuse something already implemented elsewhere, do it. Add the least amount of code possible (but make sure all error conditions are covered!)
- NEVER try to commit your changes. The user will deal with commits, you are not to touch `git commit` ever.
  - Be very careful when trying to run `git checkout` to undo some changes, there may be staged changes and you shouldn't overrite them.
- For containers, we use podman instead of docker

## Software versions and guidelines
### MariaDB
- We will use MariaDB 10.11 with InnoDB table format.
### Valkey
- We will use Valkey 8.1.


## Python Coding Guidelines

- Write for Python 3.14. Do NOT write code to support earlier versions of Python. Always use modern Python practices appropriate for Python 3.14.
- Always use full type annotations, generics, and other modern practices.
- Always use full, absolute imports for paths.
- ALWAYS use `@override` decorators to override methods from base classes.
  This is a modern Python practice and helps avoid bugs.
- Avoid writing trivial wrapper functions.
- Prefer f-strings over %-formatting.

### Types and Type Annotations

- Always use full type annotations, generics, and other modern practices.
- Use modern union syntax: `str | None` instead of `Optional[str]`, `dict[str]` instead
  of `Dict[str]`, `list[str]` instead of `List[str]`, etc.
- Never use/import `Optional` for new code.
- Use modern enums like `StrEnum` if appropriate.
- One exception to common practice on enums: If an enum has many values that are
  strings, and they have a literal value as a string (like in a JSON protocol), it’s
  fine to use lower_snake_case for enum values to match the actual value.
  This is more readable than LONG_ALL_CAPS_VALUES, and you can simply set the value to
  be the same as the name for each.
  For example:
  ```python
  class MediaType(Enum):
    """
    Media types. For broad categories only, to determine what processing
    is possible.
    """

    text = "text"
    image = "image"
    audio = "audio"
    video = "video"
    webpage = "webpage"
    binary = "binary"
  ```

## Writing style

These rules apply to everything with words in it: user-facing strings, comments, docstrings,
commit messages, documentation, and templates.

- **Serial comma, always.** Write "validate, benchmark, and submit", not "validate, benchmark and
  submit". Three or more items take a comma before the final `and` or `or`. Two items do not: "the
  driver and the toolkit" is correct as it stands. This is a house rule with no exceptions, so a
  reviewer never has to decide.
- **American English.** "behavior", not "behaviour". "canceled", not "cancelled".
- **No em-dashes.** Use a comma, a colon, or a full stop. A hyphen surrounded by spaces is fine
  where a dash is genuinely wanted.
- Follow the AlmaLinux brand book for product names and capitalization.

### Guidelines for Comments

- Comments should be EXPLANATORY: Explain *WHY* something is done a certain way and not
  just *what* is done.

- Comments should be CONCISE: Remove all extraneous words.

- DO NOT use comments to state obvious things or repeat what is evident from the code.
  Here is an example of a comment that SHOULD BE REMOVED because it simply repeats the
  code, which is distracting and adds no value:
  ```python
  if self.failed == 0:
      # All successful
      return "All tasks finished successfully"
  ```

### Guidelines for Backward Compatibility

- When changing code in a library or general function, if a change to an API or library
  will break backward compatibility, MENTION THIS to the user.

- DO NOT implement additional code for backward compatiblity (such as extra methods or
  variable aliases or comments about backward compatibility) UNLESS the user has
  confirmed that it is necessary.

## Template Coding Guidelines

- Use `{% empty %}` in templates, instead of `{% if %}{% for _ in _ %}{% endfor %}{% else %}`
- Don't use javascript confirmations. There is no shared modal partial yet, so a
  destructive action gets a real confirmation page or an intermediate POST.
- If you add new email templates listed in `settings.py`, make sure they're listed in `configured_email_template_names`
- never use the character `’` (U+2019), use `'`.
- Form styling: call `bootstrapify(form)` from `lumina/core/forms.py` at the end of a
  form's `__init__`. It sets the Bootstrap class each widget needs, and knows that
  `CheckboxSelectMultiple` and `RadioSelect` descend from `ChoiceWidget` rather than
  from `CheckboxInput` - miss that and every option renders as a full-width text
  input. There is no `StyledForm`/`StyledModelForm` and no `core/_form_field*.html`;
  this helper is the convention.
- Icons: `ti ti-*` (Tabler) on public pages, `bi bi-*` (Bootstrap Icons) on admin
  pages. The two icon sets are loaded by `base_public.html` and `base_admin.html`
  respectively, so using the wrong prefix renders nothing.
- The HTMX partial pattern: one view, two templates, chosen on
  `request.headers.get("HX-Request") == "true"`. A view that answers with a fragment
  must **only** do so for HTMX - a partial returned to a plain navigation renders as
  the whole document. Bounce other requests with
  `redirect_preserving_query()` from `lumina/core/http.py`.
- Django template comments: `{# ... #}` is **single-line only**. A hash-comment that wraps to another line is NOT a comment - Django renders everything from the opening `{#` onward as literal page text because the closing `#}` is never found (the scanner stops at the first newline). For any comment that spans more than one line, use `{% comment %}...{% endcomment %}`. This bug has shipped to the rendered UI multiple times; default to `{% comment %}` whenever the comment won't obviously fit on a single line.

## DRY + Single Source of Truth (required)

The shared primitives that already exist. Reach for these before writing a second
copy of the same rule, because every one of them was extracted *after* copies had
already drifted apart and caused a bug:

| Where | What |
|---|---|
| `lumina/core/certification.py` | `ValidationLevel`, `LEVEL_RANK`, `level_outranks`, `highest_level`. The trust tiers are totally ordered: community < almalinux < **vendor**. Both catalogs read this one ranking. |
| `lumina/core/forms.py` | `bootstrapify(form)` - the Bootstrap widget classes. Lived in three copies (hardware, software, vendors); only one handled checkbox grids. |
| `lumina/core/http.py` | `redirect_preserving_query()` - for an HTMX fragment endpoint reached by a plain navigation. |
| `lumina/taxonomy/filters.py` | `apply_category_filters()` - query params to category filters. `join_field` is the only thing that differs between catalogs. |
| `lumina/vendors/services.py` | `derive_allowed_levels`, `can_edit_listing`, `is_claimable`, `vendor_facet`, `OWNED_LISTING_MODELS`. Anything about a user's standing relative to a vendor, or about which models a vendor owns, belongs here rather than in a catalog app. |

`OWNED_LISTING_MODELS` in particular is the single enumeration of "models with both a
`vendor` and an `owner_vendor` FK". `merge_vendors` used to hardcode its own list and
would have silently orphaned software rows. A test walks every installed model and
fails if the constant misses one - so adding a fourth listing type is one line.

Before introducing new helpers/constants:
- Search the repo for existing equivalents (setting names, helper functions, payload shapes) and reuse them.
- Do not add “wrapper” functions that merely forward arguments or return `settings.*` unless they add real semantics and are used in 2+ places.
- Avoid convoluted constructions like `signed = username in { str(u).strip() for u in (getattr(agreement, "users", []) or []) if str(u).strip() }` when simply `signed = username in agreement.users` will do. You need to have a very valid reason for writing convoluted code.
- Avoid getattr (required)
  - Do not use `getattr()` for normal application code.
  - Prefer direct access (obj.attr, settings.X, module.NAME) and let errors surface during tests.
  - Only use `getattr()` when one of these is true:
    - You’re dealing with duck-typed / optional interfaces (e.g., template tags handling User | AnonymousUser | SimpleNamespace).
    - You’re interacting with threadlocals / request objects where the attribute may or may not exist; prefer `hasattr()` + direct access, or try/except AttributeError.
    - You’re probing optional third-party APIs (feature detection), where the attribute genuinely may not exist.
  - If you use `getattr()`, you must:
    - Add a short comment explaining why direct access isn’t safe here.
    - Avoid “double defaults” (don’t mirror defaults already defined in settings or upstream data prep).
- Treat any new `getattr()` in core app code as a regression unless justified by one of the allowed cases above.

When you notice duplicated logic across files:
- Refactor only if it reduces the number of implementations/branches. Moving code into a new module is not enough if the same logic still exists in multiple wrappers.
- Prefer a small shared primitive API (e.g. `make_signed_token(payload)` / `read_signed_token(token)`) over per-feature wrappers.
- Prefer removing indirection over adding it (don’t introduce `_ttl()` / `_salt()` helpers that just return settings).

Common-code-first rule (required):
- If a request changes behavior that is already computed by an existing helper/function, DO NOT re-implement that logic in views/templates/callers.
- First, adapt the shared helper so it can serve both old and new call sites (for example: return a queryset/list used by both a boolean check and additional filtering).
- Then make existing call sites consume that helper output (instead of cloning query/filter code nearby).
- Only add a new helper when it becomes the single source of truth used by 2+ call sites immediately.
- If you are about to copy even ~3 lines of business-rule query logic from another function, stop and refactor the original shared function instead.

Guardrails:
- Avoid “double defaults” (a default in settings + another default in code) because it silently diverges.
- Prefer deleting code over adding code during refactors.
- If a fix starts ballooning into lots of new production code, pause and reassess. Prefer the smallest change that satisfies the requirement, and avoid adding new layers/abstractions unless the product behavior truly needs it.

When tests/mocks constrain refactors:
- Prefer the smallest change that satisfies the failing test (or new requirement). Why: tests encode current behavior and constraints; working with them reduces risk and review overhead.
- Do not introduce new abstractions, layers, or reshaped APIs just to make a refactor feel “cleaner”. Why: this is a common source of scope creep and makes future changes harder to reason about.
- If a larger redesign is genuinely needed, stop and surface it explicitly (what breaks, what needs to change, what the migration plan is). Why: it should be a deliberate decision, not an incidental byproduct of a bugfix.
- Do not expand mocks/stubs to accommodate a new architecture unless the product behavior changed. Why: tests should validate behavior, not be rewritten to follow an unrequested design.

Pre-change checklist (must answer mentally before finishing):
- Did I add a fallback that’s already configured elsewhere?
- Did I reduce the number of implementations, or just relocate code?
- Can any new wrappers be deleted without changing call sites? If yes, delete them.

- Do Test-Driven development: Add tests before you implement a new feature, make sure they cover all the
  failure scenarios and that they really *do* fail. Then implement the new feature and make sure your tests pass.
  - Whenever I report a problem or request a new feature, I WANT YOU TO CREATE A TEST CASE FIRST, RUN THE TESTS TO SHOW THE FAILURE, AND ONLY THEN DO YOU FIX IT (and then run the tests again)
- DO NOT write trivial or obvious tests that are evident directly from code, such as
  assertions that confirm the value of a constant setting.
- DO NOT write trivial tests that test something we know already works, like
  instantiating a Pydantic object.

## Test tips
- You don't need to restart the web container after code changes; it refreshes automatically.
- **Name test classes `<Thing>Tests`, or better, use flat `def test_*()` functions.**
  `pyproject.toml` narrows `python_classes` to `["Test*Case", "*Tests"]` because
  production classes are named `TestRun`, `TestResult`, `TestRunSerializer`, and so on,
  and a `Test*` pattern makes pytest try to collect all of them. The failure mode is
  silent: a class named `TestFoo` is not collected, not reported, and not counted, so
  its tests simply never run. 63 such classes held 210 dormant tests, two of which had
  stopped matching the code they were guarding. A flat function cannot fall out of
  collection.
- Data migrations seed real rows - CPU and GPU families and their vendors (Intel, AMD,
  NVIDIA), and AlmaLinux releases in some settings. So `Vendor.objects.create(name="Intel")`
  raises on the unique slug, `Component.objects.get(name=...)` can find two, and
  asserting a total row count is asserting against a moving target. Use
  `get_or_create`, scope lookups to the object you made (`created_by=...`), and assert
  presence rather than counts.
- **A conditional `UniqueConstraint` is not enforced on MariaDB.** Django's MariaDB
  backend reports `supports_partial_indexes = False` and skips it, raising system
  check `models.W036`. The rule then holds in SQLite tests and not in production. If
  the condition is only `Q(<nullable fk>__isnull=False)` it is redundant anyway -
  SQL already treats NULLs as distinct in a unique index - so drop it and get real
  enforcement. A genuinely restrictive condition (`VendorClaim`'s open-status one)
  has to be enforced in the service layer inside a transaction instead.
- A test DB has **no** `AlmaLinuxRelease` rows unless something creates them, and
  `ingest` resolves a run's release by lookup rather than creating one. Without a
  release a run lands with `alma_release=None`, records no compatibility, and attests
  nothing. `lumina/results/tests/conftest.py` seeds 8, 9, and 10 for that package.
