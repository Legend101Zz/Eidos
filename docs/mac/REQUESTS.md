# Requests from the local Mac session

## For the cloud session

1. **Review and merge b7d9ed1** (sealed jobs/assessor could read `$HOME` on macOS). Consider whether any receipt
   recorded before it under `macos:seatbelt-sealed` should be annotated (the `no-credential-read` control did not
   cover the home directory).
2. **Temporal download fallback**: on 2026-11-08 the GitHub browser-download URL for the pinned server returned 504 for
   hours while `gh release download` (API asset endpoint) worked. Consider a fallback in `services._download` to
   `https://api.github.com/repos/temporalio/temporal/releases/assets/<id>` with `Accept: application/octet-stream`
   (digest still pinned), or document `EIDOS_TEMPORAL_ARCHIVE_CACHE` in INSTALL_AND_OPERATE.
3. `lab init` prints its "next" command without shell quoting (`EIDOS_HOME=/Volumes/Mrigesh SSD/... lab start`).

## For the owner

(none yet)
