> Superseded for this candidate: see [final local verification](FINAL_PUBLICATION_VERIFICATION.md). MIT and exact public noreply are now owner-confirmed; publication remains unapproved.

# Public release checklist

## Candidate files

- [x] Candidate audit passes; Gitleaks scan passes with redacted output (rerun on the approved final candidate).
- [ ] All published Git history/metadata is reviewed independently of current files.
- [ ] No owner credentials, accounts, private email, machine/profile paths or account usage.
- [x] No private datasets, SQLite/WAL, logs, exports, agent config or raw provider responses in the review snapshot.
- [x] Screens/video use synthetic Mock data; screenshots/contact sheet and file metadata reviewed. Final owner media review remains.
- [x] English/Traditional Chinese README, architecture, methodology, roadmap and notices exist.
- [ ] License and nonpersonal copyright attribution explicitly approved; exact LICENSE added.

## Reproducibility and hosting

- [x] Fresh isolated candidate installation, backend tests, typecheck/build and browser smoke pass (evidence in release review).
- [x] Mock/Replay needs no key or preexisting private database.
- [x] CI uses read-only permissions, no paid keys and no credential-bearing PR logs configured.
- [ ] Hosted CI result verified after approved upload; configured does not mean executed.
- [ ] Private security reporting configured and tested at the actual repository.

## Publication

- [ ] Verify existing Git remote and owner-approved repository; do not create a duplicate.
- [ ] Select approved publication identity and history strategy; never directly push the current private history.
- [ ] Review exact clean snapshot and all destination refs/tags, not only working files.
- [ ] Owner confirms publication after reviewing evidence and unresolved risks.
- [ ] Only then push the approved candidate and create tag/release; inspect public files afterward.

GitHub source publication and public application deployment are separate. Never use `git push --mirror` for this private development repository. Existing history must not be destructively rewritten without a separate approval.
