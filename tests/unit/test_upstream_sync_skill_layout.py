"""Verify sync-upstream skill structure without running the skill."""

import stat
from pathlib import Path


def test_skill_file_exists():
    """SKILL.md must exist at the expected path."""
    repo_root = Path(__file__).parents[2]
    skill_path = repo_root / ".agents" / "skills" / "sync-upstream" / "SKILL.md"
    assert skill_path.exists(), f"Skill not found at {skill_path}"


def test_skill_has_required_sections():
    """Skill must contain all required operational sections."""
    repo_root = Path(__file__).parents[2]
    skill_path = repo_root / ".agents" / "skills" / "sync-upstream" / "SKILL.md"
    content = skill_path.read_text()

    # Section names mirror what the SKILL.md actually publishes (Step N headings).
    required_sections = [
        "Step 1: Preflight",
        "Step 2: Fetch and compare",
        "Step 3: Isolated worktree merge",
        "Step 4: Merge classification",
        "Step 5: Push",
        "Step 6: sync-PR",
        "Step 7: Audit issue",
        "Step 8: Cleanup",
        "Exit codes",
        "Dry-run",
        "up_to_date",
        "auto_merged",
        "stopped_blocker",
        "preflight_failed",
        "skipped_locked",
        "worktree remove",
        "gh issue create",
        "gh pr create",
    ]
    missing = [s for s in required_sections if s not in content]
    assert not missing, f"Missing required sections: {missing}"


def test_skill_does_not_leak_github_token():
    """Skill text must not contain the literal token string."""
    repo_root = Path(__file__).parents[2]
    skill_path = repo_root / ".agents" / "skills" / "sync-upstream" / "SKILL.md"
    content = skill_path.read_text()

    token_literal = "ghs_"
    assert token_literal not in content, "SKILL.md must not contain token literals"


def test_scripts_are_executable():
    """sync_upstream.sh and setup_upstream_remote.sh must be executable."""
    repo_root = Path(__file__).parents[2]
    for script_name in ["sync_upstream.sh", "setup_upstream_remote.sh"]:
        path = repo_root / "scripts" / script_name
        assert path.exists(), f"Script not found: {script_name}"
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR, f"Script not executable: {script_name}"


def test_launchd_template_is_not_executable():
    """launchd.example.plist must NOT be executable."""
    repo_root = Path(__file__).parents[2]
    path = repo_root / "scripts" / "launchd.example.plist"
    assert path.exists(), f"Template not found: {path}"
    mode = path.stat().st_mode
    assert not (mode & stat.S_IXUSR), "launchd template must not be executable"
