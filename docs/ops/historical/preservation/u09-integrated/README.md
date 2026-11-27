# U09 integrated archive preservation — review only

Current canonical baseline `ec1fc38876f54e834091b90cf90698e708f853d0`; original worktree HEAD `3160cab17b1cddef45bbc0537b3f864fd0ab2edc`. Ownership: P26/S01-010 (archive), related P13/S04-005 and S04-007 (editorial behavior) and P27/S08-023 (newsroom tests). No original task IDs changed.

Ten commits are outside the direct canonical ancestry; git cherry reports ten plus signs. However nine single-file postimages exactly match historical canonical commit `dd7645dbb79ece59f240a3e8d879e42dc4436804`:

| Local commit | Path | Historical canonical content |
|---|---|---|
| 2da353d2e4b1 | backend/migrations/central/008_news_archive.py | exact blob |
| 8c9bc913f4ab | backend/core/archive.py | exact blob |
| fdccf7598e0f | backend/app/archive_api.py | exact blob |
| e93e09df288f | backend/app/api_router.py | exact blob |
| ebc0c507cd05 | backend/app/dependencies.py | exact blob |
| 6251c4f14fc0 | backend/app/pipeline.py | exact blob |
| 5bd03131d139 | backend/tests/test_migrations_postgresql.py | 91 added nonblank lines all found historically; exact blob not found |
| c6b487adba88 | frontend/src/features/archive/ArchiveSearch.tsx | exact blob |
| 3cdd0dbfb1bf | frontend/src/app/App.tsx | exact blob |
| 3160cab17b1c | frontend/tests/newsroom.spec.ts | exact blob |

This is strong evidence of squash-mirrored file content for nine, not proof that all commit semantics or deletions match. The historical test commit's nonidentical patch and both uncommitted changes are retained here, relative to their respective original bases. Current local changes add nine nonblank lines relative to local HEAD; all are found in the corresponding current main files. Large current-main differences (+7/-1278) cannot justify restoring an old source version. Unique implementation is not established; preserve and review.

Original local blob IDs:
- `backend/modules/editorial/api.py`: `59ce9fa89b62c67bc05635a99185f973903d77b1`
- `backend/tests/test_migrations_postgresql.py`: `b32aff3f118d76d5d2f9f43a3be819b685685432`

Archive blobs:
- `tracked-local.patch.txt`: `26b0f7205fdc8251046c9b17e8e82554338f85ca` (2102 bytes)
- `historical-test-commit.patch.txt`: `a4f0b4e184ea0b95c2ca31ffef65cd186c942a2d` (9918 bytes)

Read-only preflights S01-010, S04-005, S04-007, S08-023 passed (8, 8, 8, 10 attachments). Pattern-based secret screening of archive payloads found no matches; all added nonblank patch lines were checked against already committed canonical content. No application changes, no migration changes, no acceptance or runtime tests asserted. Original worktree and Git history remain on HOLD. Archive is not a complete backup or an executable patch. No rollback of any database; documentation revert only through a protected PR if later authorized.
