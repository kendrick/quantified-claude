"""
Tests for the SKILL.md frontmatter parser.

Skill descriptions are routinely written as YAML block scalars — `description: |`
(literal) or `description: >` (folded) with the text on the following indented
lines. The naive key:value split kept only the `|`/`>` marker and dropped the
text, so those skills rendered with empty or single-character descriptions in the
MOC. We don't preserve literal newlines (the MOC collapses whitespace anyway), so
both block styles fold to a single spaced string.
"""

from skill_usage import parse_frontmatter


def test_folded_block_scalar_description():
    text = (
        "---\n"
        "name: slalom-pptx\n"
        "description: >\n"
        "  Create Slalom-branded decks using the\n"
        "  official template.\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter(text)
    assert fm["name"] == "slalom-pptx"
    assert fm["description"] == "Create Slalom-branded decks using the official template."


def test_literal_block_scalar_description():
    text = (
        "---\n"
        "name: humanizer\n"
        "description: |\n"
        "  Remove signs of AI-generated writing.\n"
        "  Use when editing or reviewing text.\n"
        "---\n"
    )
    fm = parse_frontmatter(text)
    assert fm["description"] == "Remove signs of AI-generated writing. Use when editing or reviewing text."


def test_block_scalar_does_not_leak_into_following_keys():
    # A colon inside the block body must not be mistaken for a new key, and the
    # real key after the block must still parse.
    text = (
        "---\n"
        "name: x\n"
        "description: |\n"
        "  Trigger when: the user mentions a deck.\n"
        "license: Proprietary\n"
        "---\n"
    )
    fm = parse_frontmatter(text)
    assert fm["description"] == "Trigger when: the user mentions a deck."
    assert fm["license"] == "Proprietary"


def test_plain_quoted_value_still_parses():
    text = '---\nname: z\ndescription: "hello world"\n---\n'
    fm = parse_frontmatter(text)
    assert fm["name"] == "z"
    assert fm["description"] == "hello world"
