"""Synthetic negative controls for the publication privacy boundary."""
import importlib.util
import json
from pathlib import Path
import pytest
import subprocess


spec = importlib.util.spec_from_file_location("audit_public", Path(__file__).resolve().parents[2] / "tools/audit_public.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_sensitive_values_are_detected_but_never_returned():
    secret = "sk-" + "x" * 40
    machine = "C:" + "/Users/" + "private-fixture/project"
    email = "fixture-person" + "@example.test"
    results = audit.inspect_file("README.md", f"{secret}\n{machine}\n{email}".encode(), [email])
    assert {item["category"] for item in results} == {"credential_literal", "private_machine_path", "private_commit_identity_in_content"}
    serialized = json.dumps(results)
    assert all(value not in serialized for value in (secret, machine, email))


def test_private_files_cannot_pass_even_when_empty():
    for name in (".env", ".env.local", "artifacts/demo.json", "data/cache/private.csv", "auth.json", "research.sqlite3"):
        assert any(item["category"] == "private_file" for item in audit.inspect_file(name, b""))
    assert audit.inspect_file(".env.example", b"OPENAI_API_KEY=\n") == []


def test_directory_scans_hidden_files(tmp_path):
    (tmp_path / ".env").write_text("SAFE_LOOKING_CONTENT=", encoding="utf-8")
    result = audit.audit(tmp_path, directory=True)
    assert result["candidate_files_scanned"] == 1
    assert result["candidate_findings"][0]["file"] == ".env"


def test_missing_repository_metadata_cannot_report_a_zero_file_pass(tmp_path):
    with pytest.raises(ValueError, match="pristine source snapshot"):
        audit.audit(tmp_path)


def test_explicit_public_noreply_approval_does_not_allow_another_identity(tmp_path):
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    actor = ("Fixture Public", "123+fixture@users.noreply.github.com")
    def commit(email):
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=" + actor[0], "-c", "user.email=" + email,
                        "-c", "commit.gpgsign=false", "commit", "-m", "fixture"], check=True, capture_output=True)
    (tmp_path / "README.md").write_text("Public fixture\n", encoding="utf-8")
    commit(actor[1])
    assert audit.audit(tmp_path, history=True)["history_findings"]
    assert not audit.audit(tmp_path, history=True, approved_actor=actor)["history_findings"]
    (tmp_path / "README.md").write_text("Updated fixture\n", encoding="utf-8")
    commit("private-fixture@example.test")
    findings = audit.audit(tmp_path, history=True, approved_actor=actor)["history_findings"]
    assert len(findings) == 1 and findings[0]["category"] == "commit_identity_requires_owner_review"
    assert "private-fixture@example.test" not in json.dumps(findings)
    with pytest.raises(ValueError, match="GitHub noreply"):
        audit.audit(tmp_path, approved_actor=("Fixture", "private-fixture@example.test"))
