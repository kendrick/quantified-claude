"""
Tests for the skills inventory scan.

The personal skills under ~/.claude/skills are symlinks into a shared store
(~/.agents/skills), so the inventory scan has to follow symlinked directories or
it misses every personal skill — which is exactly what a plain rglob did,
silently dropping humanizer and friends out of the MOC.
"""

from skill_usage import collect_inventory


def _skill(dir_path, name, description="d"):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n", encoding="utf-8"
    )


def test_inventory_follows_symlinked_skill_directories(tmp_path):
    # The real skill lives in a store; the skills dir holds a symlink to it,
    # mirroring ~/.claude/skills/humanizer -> ../../.agents/skills/humanizer.
    real = tmp_path / "store" / "humanizer"
    _skill(real, "humanizer")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "humanizer").symlink_to(real)

    inventory = collect_inventory([skills_dir])

    assert "humanizer" in inventory
    assert inventory["humanizer"]["description"] == "d"


def test_inventory_reads_a_plain_unlinked_skill_dir(tmp_path):
    # Regression guard: the non-symlinked case (plugin skills) must still work.
    skills_dir = tmp_path / "skills"
    _skill(skills_dir / "impeccable", "impeccable")

    inventory = collect_inventory([skills_dir])

    assert "impeccable" in inventory
