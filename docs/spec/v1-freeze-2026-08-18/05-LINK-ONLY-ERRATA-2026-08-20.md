# V1 Link-Only Errata — 2026-08-20

This product-owner decision supersedes only the earlier V1 file-upload and
object-storage requirements. It does not expand the frozen business scope.

- Growth OS V1 does not accept, store, serve, back up, or restore uploaded
  files.
- A Task's `ContentAssetVersion` stores an external URL as the deliverable;
  changing the URL creates a new version and follows the existing review flow.
- Publication proof records an external publication URL and/or platform content
  ID. It does not require an uploaded screenshot or file.
- COS, other media/object-storage backends, media-volume backups, upload-size
  gates, and storage privacy probes are not V1 deployment requirements.
- PostgreSQL remains the authoritative data store. Its encrypted off-host
  backup, restore rehearsal, RPO no greater than one hour, and RTO no greater
  than four hours remain required.
