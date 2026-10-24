> Superseded for this candidate: see [final local verification](FINAL_PUBLICATION_VERIFICATION.md). MIT and exact public noreply are now owner-confirmed; publication remains unapproved.

# Public release review

Review date: 2026-10-24 (Asia/Taipei). Target: `v0.1.0-alpha`. **Publication is blocked pending the decisions below. Nothing has been pushed or published.**

## Findings and actions

1. Full pre-preparation Git history: Gitleaks 8.30.0 scanned all four reachable commits, exit 0, no secret findings. This is a pattern scan, not proof no unknown secret exists.
2. Privacy review found personal machine/profile locations and account-plan/usage context in local configuration and development documentation, plus identifying author/committer metadata in the original history. Values are intentionally omitted. Current public files are being sanitized; deletion does not clean earlier commits.
3. Local agent config and the developer usage helper are removed from tracking but preserved locally. Original development notes are retained only in ignored private artifacts. No credentials were read or copied into the public candidate.
4. The existing repository has no remote configured at this checkpoint. No repository was created and no account was queried for publication. A destination must be confirmed before upload; recheck for an existing repository to avoid duplication.
5. Real downloaded data, SQLite, logs and result exports stay outside publication. Public media uses synthetic Mock data only. The attached third-party illustration is not reused.
6. Root privacy rules, ignore rules, a redacted candidate/history audit and synthetic negative controls now protect future preparation. CI has read-only contents permissions, does not persist checkout credentials and uses no paid keys.

## File disposition

| Eligible after final checks | Private / excluded |
| --- | --- |
| Application and test source; dependency manifests/locks | Credentials, `.env` files except blank `.env.example` |
| Generic project skills and AGENTS.md | Machine-specific agent configuration and developer account helpers |
| Sanitized docs and third-party notices | Original private development notes and personal account metadata |
| Generated synthetic fixture and manifest | Real provider CSV/cache, databases/WAL, raw responses and exports |
| Reviewed synthetic screenshots/recording | Browser traces, local scanner logs/reports and unreviewed artifacts |

## History plan — approval required

Keep the original local repository/history private. Recommended review route: produce a clean file snapshot without `.git`, audit it, and after owner approval initialize publication history with an approved nonpersonal identity. Existing local history remains untouched. Alternatively, approve a separately reviewed filter/rewrite procedure. Neither history strategy has been executed for publication.

The snapshot tool creates a **review artifact**, not a pushable release. Do not push current branches/tags or use mirror/force operations to publish this development repository. Re-audit exact publication history after the approved identity is selected. If any real credential is later found, revoke/rotate it before history cleanup; [GitHub guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

## Licensing and remaining gates

See `LICENSE_PROPOSAL.md`, `THIRD_PARTY_NOTICES.md` and `DEPENDENCY_LICENSES.json`. MIT is proposed, not granted. Approve license/attribution, history/identity and destination, finish exact-candidate verification, then request publication confirmation. Hosted CI and a working private vulnerability channel cannot be marked complete before a repository exists.

Public Release conditions are **not yet fully met**. Real-model experiments are not required for an honest Mock/Replay alpha, but privacy, licensing, reproducibility and explicit publication approval are required.

## Observed candidate verification

- An isolated source snapshot installed with `uv sync --locked` and `npm ci`; no preexisting private data/database or paid key was required. Python tests passed in both the working repository and isolated environment. The final working tree has **47 passing tests**, including four publication negative controls; the isolated environment previously passed 46 before the fourth control was added.
- Isolated frontend type generation/checking and production build passed. Browser E2E passed **4 tests (1.3m)**. Its initial run exposed a 15-second UI assertion firing while the asynchronous batch was still RUNNING; the test now waits on the actual run status before inspecting comparison UI, with an explicit bounded job deadline.
- Candidate snapshot audit: **80 files, zero findings**. Gitleaks 8.30.0 directory scan: exit 0, no findings. Local Markdown file/image links resolve. The review snapshot has no `.git`, private cache or SQLite files.
- Four screenshots and a **45.24-second WebM** were generated against an empty synthetic-only instance with API capability disabled. Screenshots and a recording contact sheet were visually reviewed; PNGs have no text/EXIF metadata, and the video reports an encoder tag only (no artist/author/title/comment). Media remains subject to final owner review.
- Dependency metadata inventory covers 70 Python and 240 npm entries. Transitive LGPL/MPL/CC-BY entries are disclosed in third-party notices; no dependency binaries or installed environments are in the source snapshot.
- Hosted CI has not run. No repository, remote, tag, release or public deployment was created.

## Privacy of the publication destination

A public GitHub repository necessarily shows its publishing owner/organization. If the owner's personal GitHub identity must stay private, an explicitly approved separate project/organization identity is required. Do not silently choose a connected personal account. A license attribution and publication commit identity also require approval before upload.

## Suggested GitHub metadata

- Name: `quantagent-lab` (confirm availability and existing repository first).
- Description: “Multi-agent quantitative research platform with inspectable agent decisions, deterministic risk controls, backtesting and interactive visualization.”
- Topics: `quantitative-finance`, `multi-agent`, `langgraph`, `backtesting`, `fastapi`, `nextjs`, `typescript`, `research`, `visualization`.
- About website: leave blank until an approved public demo exists. Loopback URLs are local only.
- Release: use the reviewed draft notes and attach only sanitized, approved files.
