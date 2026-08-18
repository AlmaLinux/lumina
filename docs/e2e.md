# End-to-end manual test plan

These steps exercise the whole system against the dev compose stack. Run
them after any substantial change.

## Setup

```bash
cp .env.example .env
docker compose up -d db valkey keycloak mailpit
pip install -e '.[dev]'
python manage.py migrate
python manage.py createsuperuser  # bootstrap admin before Keycloak is wired
python manage.py runserver
```

In a second terminal, seed some taxonomy data via the Django admin at
http://localhost:8000/admin/:

- Nothing, for hardware. Architecture is the only hardware facet left and it is filled in from
  the run's own kernel report, so the submission form asks for no taxonomy at all and hides the
  Categories card. Network and Management were retired for failing the same test: a facet set on
  some listings and blank on others makes an empty filter result read as "no such hardware".
- For software, add the Category taxonomy with a few approved values, which is what the software
  submission form's picker offers.
- Add a Vendor (Dell, verified=True) with a VendorMembership for your user
  (role=submitter) so you can exercise the vendor-validated path.

Configure Keycloak per [keycloak.md](keycloak.md) or point at an existing
Keycloak instance, then restart the runserver with the updated `.env`.

## Test cases

1. **Community-validated submission.** Log in as a plain user (no vendor
   membership), submit a system. Verify:
   - The listing appears pending in `/review/`, not on the public catalog.
   - An email to the review-notify list lands in Mailpit
     (http://localhost:8025).
   - Approving in `/review/` publishes it at community-validated level.

2. **A vendor's submission is still only a declaration.** Log in as a user with a
   submitter membership in a verified Vendor. Select "on behalf of" and claim vendor
   level. Approve in `/review/`. The listing publishes at **community**, and its
   AlmaLinux compatibility row reads "Declared, not yet validated". The vendor badge
   comes from an approved suite run, never from this form
   (`Submission.MANUAL_CEILING`). The `on_behalf_of` attribution is still recorded.

3. **A reviewer cannot promote a declaration.** On a pending submission, confirm the
   final-level control offers community only, with no dropdown. Then post a crafted
   request asking for more and confirm it is clamped rather than honored:

   ```bash
   curl -sS -X POST -b cookies.txt -d 'final_level=almalinux' \
     http://localhost:8000/review/<pk>/approve/
   ```

   The listing must come out community-validated. This is the check that matters: a
   submission with no vendor membership, no staff flag, and no uploaded evidence used to
   reach the top tier this way.

4. **This form cannot touch an existing listing.** There is no re-validation box, and
   posting a `listing_slug` field is ignored. Confirm that submitting the same hardware
   as a second plain user creates a **second pending listing** rather than adding an
   attestation to the first, and that the first listing's compatibility rows are
   untouched. Deduplicating the two is a reviewer's job now; adding evidence to an
   existing listing means running the suite against it. See
   `NoRevalidationThroughThisFormTests` for what the old box allowed.

5. **Proposed new category value.** On the submit form, type a new value
   (e.g. "riscv64") in the Architecture propose field. Verify that after
   approval in `/review/`, it shows up in the public filter panel.

6. **Filter UX.** Hit `/hardware/systems/?vendor=dell&architecture=x86_64`.
   The URL should pre-check the filters. Change a checkbox - HTMX should
   update just the results region without a full reload. Confirm the URL
   updates (`hx-push-url`) so the result is shareable.

7. **Public JSON API.** `curl http://localhost:8000/api/v1/systems/` and
   verify only published listings appear. Confirm
   `/api/v1/categories/` returns only approved values.

8. **API token auth.** Create an `ApiToken` for your user via the Django
   admin, copy the raw value from `ApiToken.issue` (use `manage.py shell`
   for now - UI lands later). Hit `/api/v1/submissions/` with
   `Authorization: Bearer <token>`. Verify a read-only token gets 403
   and a submit-scoped token gets 501 (scaffolded).

9. **Audit log.** After any approve/reject action, check
   `/admin/audit/auditlogentry/` - there should be an immutable row
   attributing the action to you with the X-Forwarded-For'd IP.
