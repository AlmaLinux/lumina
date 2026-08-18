"""What a submitter may attach to a manual hardware submission, and how it is served.

The attachment field was a bare ``FileField(required=False)``: no extension check, no
size limit, no digest. Combined with an nginx ``/media/`` block that served the file
directly from the application's origin with no ``Content-Disposition``, an uploaded
``.html`` was stored cross-site scripting aimed at reviewers, reachable from a link on
the reviewer's own page.

Two defenses, and they are not redundant:

- The **allowlist** here keeps obviously wrong things out of the store and tells the
  submitter at upload time. It depends on the list being right.
- The **response headers** make a hostile file inert whatever its extension, which is
  the half that does not depend on anyone's list being complete.

Neither is manifest verification and neither pretends to be. Nothing opens the file. A
declared submission has no manifest to check against, which is why its tier is capped
at community instead of these being asked to carry weight they cannot.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from lumina.core.files import (
    EVIDENCE_EXTENSIONS,
    max_evidence_bytes,
    validate_evidence_file,
)


def _upload(name: str, size: int = 10):
    return SimpleUploadedFile(name, b"x" * size)


# --- the allowlist -------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "results.log", "dmidecode.txt", "lspci.out", "report.pdf", "screenshot.png",
    "inventory.json", "notes.md", "output.tar.gz", "bundle.zst",
])
def test_ordinary_evidence_is_accepted(name):
    validate_evidence_file(_upload(name))


@pytest.mark.parametrize("name", [
    "payload.html", "payload.htm", "logo.svg", "script.js", "shell.php",
    "tool.exe", "lib.so", "macro.xlsm", "page.xhtml",
])
def test_files_that_execute_or_render_are_refused(name):
    """``.svg`` is in this list on purpose.

    It reads like an image and is a document that can carry script, which makes it the
    most common way an "image upload" becomes XSS. It is the one exclusion here a
    reader is most likely to think is a mistake.
    """
    with pytest.raises(ValidationError):
        validate_evidence_file(_upload(name))


def test_svg_is_not_quietly_in_the_image_group():
    """Guards the list itself, not the function. Adding ``.svg`` while extending the
    image types would pass every other test in this file."""
    assert ".svg" not in EVIDENCE_EXTENSIONS
    assert ".html" not in EVIDENCE_EXTENSIONS
    assert ".png" in EVIDENCE_EXTENSIONS


def test_a_file_with_no_extension_is_refused():
    """Nginx would serve it as the default type, and the reviewer has no idea what it
    is either."""
    with pytest.raises(ValidationError):
        validate_evidence_file(_upload("dmesg"))


def test_only_the_last_extension_counts():
    """That is what a server maps to a content type, so that is what can be acted on.

    ``notes.exe.txt`` is a text file to anything serving it. The reverse is not.
    """
    validate_evidence_file(_upload("notes.exe.txt"))
    with pytest.raises(ValidationError):
        validate_evidence_file(_upload("notes.txt.exe"))


def test_the_extension_check_is_case_insensitive():
    validate_evidence_file(_upload("RESULTS.LOG"))
    with pytest.raises(ValidationError):
        validate_evidence_file(_upload("PAYLOAD.HTML"))


def test_an_oversized_file_is_refused():
    with pytest.raises(ValidationError) as exc:
        validate_evidence_file(_upload("huge.log", size=max_evidence_bytes() + 1))

    assert "limited to" in str(exc.value)


def test_a_file_at_the_limit_is_accepted():
    validate_evidence_file(_upload("big.log", size=max_evidence_bytes()))


def test_the_limit_is_configurable(settings):
    settings.LUMINA_MAX_EVIDENCE_MB = 1

    validate_evidence_file(_upload("ok.log", size=1024 * 1024))
    with pytest.raises(ValidationError):
        validate_evidence_file(_upload("no.log", size=1024 * 1024 + 1))


# --- through the form ----------------------------------------------------------


@pytest.fixture
def payload(db):
    from django.contrib.auth.models import User

    from lumina.vendors.models import Vendor

    vendor = Vendor.objects.create(name="Dell Inc.", published=True)
    user = User.objects.create_user("attach-probe", password="pw")
    return user, {
        "kind": "system", "name": "PowerEdge R750", "vendor": vendor.slug,
        "claimed_validation_level": "community",
    }


@pytest.mark.django_db
def test_the_submit_form_refuses_a_hostile_attachment(client, payload):
    from lumina.hardware.models import Submission

    user, data = payload
    client.force_login(user)
    data["attachments"] = _upload("payload.html")

    resp = client.post(reverse("submit:start"), data)

    assert resp.status_code == 200, "expected the form to re-render with an error"
    assert not Submission.objects.exists()
    assert ".html files cannot be attached" in resp.content.decode()


@pytest.mark.django_db
def test_every_posted_file_is_checked_not_only_the_bound_one(client, payload):
    """The gap a plain field validator leaves, and the ordering matters.

    ``_attach_files`` stores everything in ``files.getlist("attachments")``, but a
    ``FileField`` cleans a single value, and ``MultiValueDict`` hands it the **last**
    posted file. So the bad file goes **first** here: with only the field validator in
    place, ``results.log`` is what gets cleaned, ``payload.html`` sails past unchecked
    and lands in the store.

    Written the other way round at first, and it passed with ``clean_attachments``
    deleted - the field validator happened to bind the hostile file because it was
    last. A test that cannot fail is worse than no test, so the order is the assertion
    here.
    """
    from lumina.hardware.models import Submission, TestResultAttachment

    user, data = payload
    client.force_login(user)

    resp = client.post(reverse("submit:start"), dict(
        data, attachments=[_upload("payload.html"), _upload("results.log")],
    ))

    assert resp.status_code == 200
    assert not Submission.objects.exists()
    assert not TestResultAttachment.objects.exists()


@pytest.mark.django_db
def test_a_good_attachment_still_goes_through(client, payload):
    from lumina.hardware.models import Submission

    user, data = payload
    client.force_login(user)
    data["attachments"] = _upload("results.log")

    resp = client.post(reverse("submit:start"), data)

    assert resp.status_code == 302
    assert Submission.objects.get().attachments.count() == 1


# --- how it is served ----------------------------------------------------------
#
# Asserted against the nginx template because that is where the rule lives and there is
# nothing else to assert it against: the file is rendered by Ansible onto a host this
# test suite never sees. Same approach as the CSS assertions in
# test_searchable_vendor_pickers.py, and the same limitation - it pins the directive,
# not the running server.

_NGINX_CONF = (
    Path(__file__).resolve().parents[3]
    / "ansible" / "roles" / "lumina" / "templates" / "lumina.nginx.conf.j2"
)


def _media_block() -> str:
    """The **public** media alias, not the internal evidence location.

    Matched on ``location /media/ {`` including the space, because a bare
    ``location /media/`` prefix also matches ``location /media/test-results/`` and finds
    that one first. Adding the internal block silently repointed three of the assertions
    below at it, where two kept passing on the wrong block and only the third failed.
    """
    conf = _NGINX_CONF.read_text()
    start = conf.index("location /media/ {")
    return conf[start:conf.index("location /", start + 1)]


def test_the_conf_template_is_where_we_think_it_is():
    """So the assertions below cannot silently pass on a file that moved."""
    assert _NGINX_CONF.is_file(), _NGINX_CONF


def test_uploaded_files_are_downloaded_never_rendered():
    """The defense that does not depend on the allowlist being complete."""
    assert 'Content-Disposition "attachment"' in _media_block()


def test_browsers_are_told_not_to_sniff_the_type():
    assert 'X-Content-Type-Options "nosniff"' in _media_block()


def test_uploaded_files_cannot_run_script_if_reached_anyway():
    assert "Content-Security-Policy" in _media_block()
    assert "default-src 'none'" in _media_block()


def test_the_headers_apply_to_error_responses_too():
    """Without ``always``, nginx drops add_header on a 4xx/5xx, which is exactly the
    response a probe for a nonexistent path gets."""
    block = _media_block()
    for line in block.splitlines():
        if "add_header" in line:
            assert line.rstrip().endswith("always;"), line


def test_static_assets_are_not_forced_to_download():
    """Sanity that the rule is scoped to uploads. Forcing this on /static/ would make
    the site's own CSS and JS download instead of load."""
    conf = _NGINX_CONF.read_text()
    static_block = conf[conf.index("location /static/"):conf.index("location /media/")]

    assert "Content-Disposition" not in static_block


# --- who may read one ----------------------------------------------------------
#
# Uploading was always behind ``@login_required``. *Reading* was not behind anything:
# the file was served straight off the public ``/media/`` alias by nginx, so anyone
# holding the URL could fetch it forever, with no revocation and with ``expires 7d``
# inviting caches to keep their own copies. Nothing was enumerable, because the path
# carries the submission's UUID4, but unguessable is not protected.
#
# Submission evidence is reviewer material rather than catalog content, so it now goes
# through ``submit:attachment``, which authorizes first.


@pytest.fixture
def attached(client, payload):
    """One submission with one evidence file, created through the real form."""
    from lumina.hardware.models import TestResultAttachment

    user, data = payload
    client.force_login(user)
    client.post(reverse("submit:start"), dict(
        data, attachments=SimpleUploadedFile("results.log", b"it works here"),
    ))
    client.logout()
    return user, TestResultAttachment.objects.get()


@pytest.fixture
def a_reviewer():
    from django.contrib.auth.models import Group, User

    user = User.objects.create_user("evidence-rev", password="pw")
    group, _ = Group.objects.get_or_create(name="reviewer")
    user.groups.add(group)
    return user


def _body(response) -> bytes:
    if response.streaming:
        return b"".join(response.streaming_content)
    return response.content


@pytest.mark.django_db
def test_the_submitter_can_read_their_own_attachment(client, attached):
    submitter, attachment = attached
    client.force_login(submitter)

    resp = client.get(reverse("submit:attachment", args=[attachment.pk]))

    assert resp.status_code == 200
    assert _body(resp) == b"it works here"


@pytest.mark.django_db
def test_a_reviewer_can_read_it(client, attached, a_reviewer):
    _, attachment = attached
    client.force_login(a_reviewer)

    resp = client.get(reverse("submit:attachment", args=[attachment.pk]))

    assert resp.status_code == 200
    assert _body(resp) == b"it works here"


@pytest.mark.django_db
def test_anybody_else_gets_a_404(client, attached):
    """404 rather than 403: which submissions exist is not public, so "not yours" has to
    be indistinguishable from "no such thing"."""
    from django.contrib.auth.models import User

    _, attachment = attached
    client.force_login(User.objects.create_user("nosy", password="pw"))

    resp = client.get(reverse("submit:attachment", args=[attachment.pk]))

    assert resp.status_code == 404


@pytest.mark.django_db
def test_anonymous_is_sent_to_log_in(client, attached):
    _, attachment = attached

    resp = client.get(reverse("submit:attachment", args=[attachment.pk]))

    assert resp.status_code == 302
    assert "/oidc/" in resp["Location"] or "login" in resp["Location"]


@pytest.mark.django_db
def test_it_downloads_rather_than_renders(client, attached):
    submitter, attachment = attached
    client.force_login(submitter)

    resp = client.get(reverse("submit:attachment", args=[attachment.pk]))

    assert "attachment" in resp["Content-Disposition"]


@pytest.mark.django_db
def test_nginx_does_the_sending_when_configured(client, attached, settings):
    """``X-Accel-Redirect`` keeps a 25 MB download out of a gunicorn worker.

    The handoff is opt-in through a setting because nothing but nginx understands the
    header: with it unset the view streams the file itself, which is what the dev server
    and this suite do, and a deployment that forgot the setting serves real bytes rather
    than an empty body.
    """
    settings.LUMINA_INTERNAL_MEDIA_LOCATION = "/media/"
    submitter, attachment = attached
    client.force_login(submitter)

    resp = client.get(reverse("submit:attachment", args=[attachment.pk]))

    assert resp.status_code == 200
    assert resp["X-Accel-Redirect"] == f"/media/{attachment.file.name}"
    assert resp.content == b"", "the body must come from nginx, not Django"
    # Left to nginx, which reads it off the extension. Django's default would otherwise
    # pin every download to text/html.
    assert not resp.has_header("Content-Type")


@pytest.mark.django_db
def test_the_handoff_target_is_the_internal_location(settings):
    """The setting and the vhost have to name the same prefix, or every download 404s."""
    block = _NGINX_CONF.read_text()

    assert "location /media/test-results/" in block
    env = (
        _NGINX_CONF.parent / "lumina.env.j2"
    ).read_text()
    assert "LUMINA_INTERNAL_MEDIA_LOCATION=/media/" in env


def test_the_evidence_location_is_internal_only():
    """``internal`` is the whole protection: nginx refuses the path to any client asking
    for it directly, however it learned the URL."""
    conf = _NGINX_CONF.read_text()
    start = conf.index("location /media/test-results/")
    block = conf[start:conf.index("}", start)]

    assert "internal;" in block


def test_the_evidence_location_precedes_the_public_alias():
    """nginx picks the longest matching prefix, so ordering does not strictly decide
    this - but a reader has to see the exception before the rule, and reordering them
    while adding a regex location would change which one wins."""
    conf = _NGINX_CONF.read_text()

    assert conf.index("location /media/test-results/") < conf.index("location /media/ ")


@pytest.mark.django_db
def test_the_reviewer_page_links_through_the_authorizing_view(
    client, attached, a_reviewer
):
    """A stale ``{{ a.file.url }}`` would now point at a path nginx refuses, so the link
    would 404 for everyone including reviewers."""
    _, attachment = attached
    client.force_login(a_reviewer)

    body = client.get(
        reverse("review:detail", args=[attachment.submission.pk])
    ).content.decode()

    assert reverse("submit:attachment", args=[attachment.pk]) in body
    assert f"/media/{attachment.file.name}" not in body
