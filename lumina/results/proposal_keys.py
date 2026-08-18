"""The key format of a run's ``listing_proposal`` blob, declared once.

The blob is a flat dict of form field names, so its keys *are* a wire format between
three places: the form that builds the fields, the merge that accumulates them, and
the approval that consumes them. It was spelled out in all three - class attributes on
``RunListingProposalForm``, module constants in ``results.services``, and bare string
literals in ``apply_proposal_metadata`` - so a rename would have had to be made in
three places and would have silently half-worked if it were not.

Not hoisted to ``lumina.core``: hardware's own submit form uses different spellings
(``release_support_``, ``release_min_minor_``). Sharing across the two would change a
stored format rather than deduplicate a declaration.
"""
from __future__ import annotations

RELEASE_PREFIX = "release_"
RELEASE_MINOR_PREFIX = "release_minor_"
CATEGORY_PREFIX = "cat_"
PROPOSE_PREFIX = "propose_"


def is_release_key(key: str) -> bool:
    """Whether ``key`` is a release *tick*, not its minor floor.

    ``release_minor_9`` starts with ``release_`` too, which is the whole reason this
    is a function. The overlap guard was implemented twice - once with a comment
    explaining it, once without - and getting it wrong means reading a minor number
    as a major.
    """
    return key.startswith(RELEASE_PREFIX) and not key.startswith(RELEASE_MINOR_PREFIX)


def release_major(key: str) -> int | None:
    """The major a release tick names, or None if the key is not one."""
    if not is_release_key(key):
        return None
    try:
        return int(key[len(RELEASE_PREFIX):])
    except ValueError:
        return None


def minor_key(major: int) -> str:
    return f"{RELEASE_MINOR_PREFIX}{major}"


def release_key(major: int) -> str:
    return f"{RELEASE_PREFIX}{major}"
