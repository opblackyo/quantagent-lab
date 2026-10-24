"""Create an audited review snapshot without Git history. Never publishes."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from audit_public import audit


def main():
    root = Path(__file__).resolve().parents[1]
    result = audit(root)
    if result["candidate_findings"]:
        print(json.dumps(result, indent=2))
        raise SystemExit("Candidate audit failed; no snapshot created.")
    names = subprocess.check_output(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]).decode().split("\0")
    names = sorted({name for name in names if name and (root / name).is_file()})
    destination = root / "artifacts" / ("release-review-" + uuid4().hex[:8])
    destination.mkdir(parents=True)
    manifest = []
    for name in names:
        source = root / name
        # Never follow a candidate symlink outside the repository or into private artifacts.
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise SystemExit("Symlink/outside source requires manual review.")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest.append({"file": name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    archive = destination.with_suffix(".zip")
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        for item in manifest:
            bundle.write(destination / item["file"], arcname="quantagent-lab/" + item["file"])
    receipt = {"purpose": "review-only; licensing/history/publication approval pending", "files": manifest,
               "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    destination.with_suffix(".manifest.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"review_directory": destination.relative_to(root).as_posix(),
                      "archive": archive.relative_to(root).as_posix(), "files": len(manifest),
                      "archive_sha256": receipt["archive_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
