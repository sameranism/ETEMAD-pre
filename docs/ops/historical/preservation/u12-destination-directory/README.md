# U12 destination directory preservation — review only

Baseline: `ec1fc38876f54e834091b90cf90698e708f853d0`. Original worktree HEAD: `fd75ee14fb6cc79811bece5247acae1b15780d4b`. Related owner tasks: P23/S06-002 (candidate directory), P27/S08-017 (publishing UI). P25/S06-013/014/015 are adjacent integration/test requirements, not new IDs or claims of completion.

This is a non-executable historical archive, not a feature delivery. The tracked patch is against the original HEAD, NOT current main. Two previously untracked files are captured verbatim as .txt files. Do not apply the patch or copy the migration into the active migration directory. Canonical 014_durable_workflow already has parent 013_daily_reports; the local 014_destination_directory would create a competing head. Any implementation requires a separately reviewed additive revision after the current head, compatible RBAC/contract checks, and separately authorized database execution. Do not run a downgrade. The local draft builds inactive candidates, owner-only registration, bounded directory listing, and a UI that does not enable dispatch. Canonical main has no direct matching table, file or named component in the inspected scope; semantic equivalence under a different architecture is not established.

Original local blob identities (Git blob SHA-1, not commit SHA):

- `backend/app/dependencies.py`: `f969575681bdd11a83cab2f2c7d8389fcb4643f3`
- `backend/modules/publishing/api.py`: `217a039a9b7a6fb551115685d13451a70e635f52`
- `backend/modules/publishing/destinations.py`: `b73b3354db6156ebffae0c475bf7f196ec9667ab`
- `backend/tests/test_migrations_postgresql.py`: `f26e5d71eb4ecb8359222c6999e54cb92bf948c2`
- `frontend/src/features/publishing/PublishingCenter.tsx`: `7d98a75955a060479afd8e58553432c254019205`
- `backend/migrations/central/014_destination_directory.py`: `a08f255918f573e8c15633231dca21fa49732417`
- `frontend/src/features/publishing/DestinationDirectory.tsx`: `d20fbacd9f24b3a8abd2abf3ba6a5f9e90c0afa0`

Archive blob identities:

- `tracked-local.patch.txt`: `415696fad37ac04ad944293ef03b931d7ba2ec7a` (16548 bytes)
- `014_destination_directory.py.txt`: `a08f255918f573e8c15633231dca21fa49732417` (1695 bytes)
- `DestinationDirectory.tsx.txt`: `d20fbacd9f24b3a8abd2abf3ba6a5f9e90c0afa0` (6624 bytes)

Read-only preflight S06-002 and S08-017 passed (12 and 14 required attachments). Local content and structural differences reviewed; a pattern-based secret scan found no matches in these archive payloads; fixture credential references are not secret values. No runtime tests or CI success claimed for this PR. Existing worktree stays untouched and remains on preservation HOLD. This is not a full Git or ignored-file backup. Rollback of this documentation, if approved, is by a protected revert, never deletion of the source worktree.
