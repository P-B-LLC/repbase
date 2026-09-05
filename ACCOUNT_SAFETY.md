# Account safety rollout

## Implemented

- Account deletion and individual post deletion enqueue avatar/original/feed-photo cleanup in the same database transaction as deletion.
- Storage deletion happens after commit; rolling back leaves files intact.
- Failed storage operations remain in `PendingMediaDeletion` for retry. Surviving references are checked before deletion.
- The account token is invalidated by the existing user cascade.
- Regression coverage includes API deletion/token invalidation, post deletion, rollback, storage failure/retry, and shared references.

## Deployment requirements

1. Apply migration `0048_pendingmediadeletion` with `python manage.py migrate` in the deployment environment.
2. Run `python manage.py retry_media_deletions` from the deployment's scheduled-job runner regularly, and alert on failures or a growing pending queue. Immediate deletion is attempted at commit; this job handles storage outages or process interruption.
3. Confirm the production storage identity can delete files and that any CDN cache policy also honors media deletion. The queue uses Django's configured default storage, matching these image fields.
4. Test deletion through the beta API and verify original and derived files are gone from the actual storage/CDN.

Previously orphaned files cannot be attributed from already-deleted database rows. They are not automatically erased by this migration. Inventory them against live references and the deployed retention policy before a separately approved cleanup. No existing user media or production database was changed during implementation.

## Validation on Windows

- Full backend suite: 231 tests passed, including five new account-safety tests.
- Existing photo-test file handles now close during test cleanup; the full suite exits successfully on Windows.
- Migration drift check passed.
- Pagination warnings identified by the audit remain outside this account-safety batch.

This batch does not resolve the separate route-retry, editor-save, pagination, release configuration, or device-QA audit items.
