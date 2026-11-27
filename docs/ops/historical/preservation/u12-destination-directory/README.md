# Package 2 — U12 original-HEAD preservation

Original worktree: /home/ai/etemad-dev/u12-destination-directory  
Original HEAD: fd75ee14fb6cc79811bece5247acae1b15780d4b  
Package 1 pinned audit: 4bda003428f982daa88ba20cfd539d7e44111f53

This PR preserves work found in a local worktree. It does not claim acceptance.

Package 1 A1 found seven files with distinct local implementation within the searched scope: no direct publish.destinations table, candidate directory implementation, component or route found in pinned main; 277 of 294 nonblank locally added lines were absent in corresponding files. Equivalent behavior elsewhere not conclusively excluded. The archived migration shares its parent with canonical 014_durable_workflow and MUST NOT enter the executable migration chain.

The five tracked changes are retained as an exact patch against this commit's parent, the original worktree HEAD. The two untracked files are byte-identical text archives. No unrelated changes are included. These three payloads already exist in [draft PR #133](https://github.com/sameranism/ETEMAD-pre/pull/133) based on ec1fc388. This branch fulfills Package 2's distinct original-HEAD ancestry and naming requirement, reusing verified Git blobs rather than creating another working copy. Do not merge both overlapping preservation PRs; retain both until the human selects the preferred review lineage. No implementation, activation, migration, deployment, runtime or acceptance change.

| Source path | Original local blob | Owning or adjacent task |
|---|---|---|
| backend/app/dependencies.py | f969575681bdd11a83cab2f2c7d8389fcb4643f3 | P23/S06-002; shared wiring S01-025/P01 |
| backend/modules/publishing/api.py | 217a039a9b7a6fb551115685d13451a70e635f52 | P23/S06-002 |
| backend/modules/publishing/destinations.py | b73b3354db6156ebffae0c475bf7f196ec9667ab | P23/S06-002 |
| backend/tests/test_migrations_postgresql.py | f26e5d71eb4ecb8359222c6999e54cb92bf948c2 | P23/S06-002; adjacent P25/S06-014/015 |
| frontend/src/features/publishing/PublishingCenter.tsx | 7d98a75955a060479afd8e58553432c254019205 | P27/S08-017 |
| backend/migrations/central/014_destination_directory.py | a08f255918f573e8c15633231dca21fa49732417 | P23/S06-002; adjacent P25/S06-014 |
| frontend/src/features/publishing/DestinationDirectory.tsx | d20fbacd9f24b3a8abd2abf3ba6a5f9e90c0afa0 | P27/S08-017 |

## Archive verification

- tracked-local.patch.txt: 415696fad37ac04ad944293ef03b931d7ba2ec7a
- 014_destination_directory.py.txt: a08f255918f573e8c15633231dca21fa49732417
- DestinationDirectory.tsx.txt: d20fbacd9f24b3a8abd2abf3ba6a5f9e90c0afa0

Read-only source comparison confirmed all seven local hashes unchanged and the existing tracked patch hash exact. Pattern-based screening found no credential indicators in the seven source files; this is not an exhaustive security guarantee. Original worktree retained on HOLD. No test or CI pass claimed for this new head. No automatic reviewer requests. Rollback or consolidation of the archive PRs requires separate human approval.