# Final local publication preparation

Date: 2026-10-24 (Asia/Taipei). Target: v0.1.0-alpha. No publication is authorized or performed. This supersedes earlier release-preparation status.

## Observed verification

- Roots were discovered from uv.lock and frontend/package-lock.json, not assumed. A fresh archive was extracted outside the original private repository.
- uv sync --locked and npm ci completed. Original 47 tests passed (49.71s). A new exact-approved-identity regression control was added; the final Python suite passed **48 tests (20.45s)**, with 44 upstream pandas/calendar deprecation warnings.
- Frontend typecheck and production build passed. Chromium installation completed; **4 Playwright E2E tests passed (1.1m)**. No real provider calls were used.
- MIT was explicitly selected with attribution to QuantAgent Lab contributors; root LICENSE and package metadata agree. This does not authorize publication.
- pip-licenses 5.5.5 audited 58 installed Python dependencies; license-checker-rseidelsohn 5.0.1 audited 169 installed npm dependencies. The original broader lockfile inventory includes optional platforms/market packages. Sanitized metadata is in FINAL_DEPENDENCY_LICENSES.json. Raw reports, paths and third-party contacts remain outside the snapshot.
- Running pip-licenses in an unrelated uvx environment or using its external-interpreter path parser on a path containing spaces can give an empty report. The accepted run used uv run --with pip-licenses with project dependencies and asserted expected core package names and count.
- The npm tool labels the project root UNLICENSED because it is marked private; the root is excluded from the third-party count. It has an owner-approved MIT LICENSE and manifest license. No third-party dependency was UNKNOWN/UNLICENSED in this installed scan. LGPL/MPL/CC-BY notices and source-only scope remain in THIRD_PARTY_NOTICES.md; this is not a blanket legal certification.

## Media and privacy

ExifTool 13.59 was obtained from a fixed official source commit and used with -all= -overwrite_original on all four PNGs. FFmpeg remuxed the 45.24-second video with -map_metadata -1 -c copy. Compressed image/video content was verified unchanged. An encoder identifier is technical container metadata, not an account. Screenshots and five contact sheets (one sampled frame per second) were visually inspected with no private identity or credential observed. This is scene sampling, not a claim that every decoded frame was manually read. Final owner media review is still required.

A broad installed-tree scan found 18 items confined to NumPy test vectors or generated Next preview/server-action artifacts. Those environments/builds are excluded from the source snapshot and are not represented as clean publishable files. Final secret/privacy checks apply to the exact clean source files and new history, not a directory containing dependencies or runtime outputs.

## New history and approval

Use only the exact owner-confirmed GitHub Settings noreply and confirmed public username for the new initial commit. No private email or unrelated account is approved. Audit the new history with the explicit approved identity options; they permit that exact pair only. The added regression test ensures another identity is still flagged.

The full publication checklist is private evidence, outside the public source files. It binds the reviewed manifest/commit, MIT selection and confirmed identity without publishing private emails. The final Owner-publication checkbox remains empty. No remote is added and no create/push/release command is executed. Hosted CI has not run. The original private working files and .git are checked against a before/after byte-hash baseline.
