"""Redacted publication checks; complement Gitleaks and manual media review.

Default: inspect tracked and non-ignored candidate files. --history additionally
checks every reachable Git tree and commit identity. --directory inspects a
standalone release snapshot without consulting its Git metadata.
Never print matched values, raw source lines, account names, or email addresses.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path


PATTERNS = {
    "private_machine_path": re.compile(r"(?i)(?:[A-Z]:[\\/](?:Users|CodexProfiles|github|Program Files)[\\/]|/(?:Users|home)/[\w.-]+/)"),
    "credential_literal": re.compile(r"(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"),
}
PRIVATE_ROOTS = (".git/", ".codex/", "artifacts/", "data/cache/", ".playwright-mcp/", ".venv/", "frontend/node_modules/", "frontend/.next/")
PRIVATE_NAMES = {"auth.json", "credentials.json", "tools/check_codex_usage.py"}
PRIVATE_SUFFIXES = (".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx", ".log")
PUBLIC_ACTOR = ("QuantAgent Lab", "contributors@quantagent.invalid")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


def inspect_file(name, content, identities=()):
    findings = []
    parts = Path(name).parts
    if (name.startswith(PRIVATE_ROOTS) or name in PRIVATE_NAMES
            or Path(name).name in PRIVATE_NAMES or name.endswith(PRIVATE_SUFFIXES)
            or any(part.startswith(".env") and part != ".env.example" for part in parts)):
        findings.append({"file": name, "category": "private_file"})
    if name.endswith(".csv") and not name.startswith("data/sample/"):
        findings.append({"file": name, "category": "unapproved_dataset"})
    if len(content) > 5 * 1024 * 1024:
        findings.append({"file": name, "category": "large_file_requires_review"})
    if b"\0" in content:
        return findings  # Images/video require visual review; do not claim text scan covers them.
    for number, line in enumerate(content.decode("utf-8", errors="replace").splitlines(), 1):
        for category, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append({"file": name, "line": number, "category": category})
        if any(value in line for value in identities):
            findings.append({"file": name, "line": number, "category": "private_commit_identity_in_content"})
    return findings


def audit(root, history=False, directory=False, approved_actor=None):
    findings = []
    identities = set()
    commits = []
    public_actors = {PUBLIC_ACTOR}
    if approved_actor is not None:
        name, email = approved_actor
        if not name.strip() or not re.fullmatch(r"[^@\s]+@users\.noreply\.github\.com", email):
            raise ValueError("Explicit approval requires a nonempty name and exact GitHub noreply address")
        public_actors.add((name, email))
    if directory:
        paths = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    else:
        try:
            git_root = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        except subprocess.CalledProcessError:
            raise ValueError("Use --directory for a pristine source snapshot without Git metadata") from None
        if git_root != root.resolve():
            raise ValueError("Refusing inherited parent Git metadata; use --directory for a pristine source snapshot")
        paths = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
        # Derive private identifiers locally; never embed them in public configuration.
        records = git(root, "log", "--all", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce").decode().splitlines()
        for record in records:
            commit, author, email, committer, committer_email = record.split("\0")
            private = any(actor not in public_actors for actor in ((author, email), (committer, committer_email)))
            commits.append((commit, private))
            for actor in ((author, email), (committer, committer_email)):
                if actor not in public_actors:
                    identities.update(value for value in actor if len(value) >= 6)
    scanned = 0
    for name in sorted(set(filter(None, paths))):
        path = root / name
        if path.is_file():
            findings.extend(inspect_file(name, path.read_bytes(), identities))
            scanned += 1
    history_findings = []
    if history:
        seen = set()
        for commit, private in commits:
            if private:
                history_findings.append({"commit": commit, "category": "commit_identity_requires_owner_review"})
            for entry in git(root, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
                if not entry:
                    continue
                meta, raw_name = entry.split(b"\t", 1)
                _, kind, oid = meta.split()
                name = raw_name.decode()
                if kind != b"blob" or (oid, name) in seen:
                    continue
                seen.add((oid, name))
                content = git(root, "cat-file", "blob", oid.decode())
                for finding in inspect_file(name, content, identities):
                    history_findings.append({"commit": commit, **finding})
    return {"candidate_files_scanned": scanned, "candidate_findings": findings,
            "history_commits_scanned": len(commits) if history else 0,
            "history_findings": history_findings,
            "limitations": "Pattern checks only; run Gitleaks and visually review media. Scan a clean approved publication history separately."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--approved-name", help="Owner-confirmed public commit name, used only for this audit")
    parser.add_argument("--approved-noreply", help="Exact owner-confirmed GitHub Settings noreply; never inferred")
    args = parser.parse_args()
    if args.history and args.directory:
        parser.error("--history requires the working repository")
    if bool(args.approved_name) != bool(args.approved_noreply):
        parser.error("Both approved identity fields are required")
    try:
        result = audit(args.directory.resolve() if args.directory else Path(__file__).resolve().parents[1],
                       history=args.history, directory=bool(args.directory),
                       approved_actor=(args.approved_name, args.approved_noreply) if args.approved_name else None)
    except ValueError as exc:
        print(str(exc))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return int(bool(result["candidate_findings"] or result["history_findings"]))


if __name__ == "__main__":
    raise SystemExit(main())
