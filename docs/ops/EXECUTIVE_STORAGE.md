# P19 / S05-008 / S05-009 — private report storage

The central database still belongs to the existing three-database architecture.
Migration 015 replaces the empty plaintext executive table with the
`executive.private_reports` index. It refuses to discard existing plaintext
records. A populated legacy table needs a separately verified encrypted import.
The normal runtime role must not own or have privileges on this schema/table.

Provision the index owner as the separate migration/administration role.
Grant a dedicated non-superuser runtime login only CONNECT to central, USAGE on
executive, and SELECT/INSERT on executive.private_reports. Do not grant UPDATE,
DELETE, TRUNCATE, CREATE, membership in the ordinary role, or public/other
business-table access. The ordinary runtime must not be a member of this role.
No credentials are embedded in SQL migrations.

Runtime configuration (in the protected deployment secret store, not Git):

- `ETEMAD_EXECUTIVE_DB_URL`: dedicated login, same central database.
- `ETEMAD_EXECUTIVE_OBJECT_ROOT`: separate 0700 directory for private staging,
  not a child or parent of the ordinary object root. Use a scoped private
  object-storage bucket/adapter for production.
- `ETEMAD_SECRET_EXECUTIVE_REPORTS_KEY`: base64-encoded random 32-byte key.

Only AES-256-GCM ciphertext and ciphertext checksums are stored; each object
uses a fresh nonce and authenticated subject/report/slot identity. The public
index and public object root never contain executive report payloads. Storage
configuration is validated against actual database privileges before activation
and before each private operation. Missing or invalid private configuration
leaves private operations unavailable; other newsroom functionality continues.
Private reads still require explicit user-level EIC grants and current evidence
authorization. This is not a separate public retrieval source.

Back up the executive index and encrypted objects together in a protected
location; keep key backups separately in a secret store. Test restoration
and decryption before declaring production-ready. The v1 format uses one
configured key; key rotation requires an authorized re-encryption migration
with authenticated object verification, backup and rollback. Never overwrite or
delete the current key before verifying all restored/re-encrypted objects.
A crash before committing an index row can leave an encrypted orphan; retention
cleanup and bucket policy must be validated during the release workflow.
