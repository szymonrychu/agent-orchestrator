"""slugify and generated_slug: DNS-1123 slugs derived from free-text names."""

from __future__ import annotations

import re

import pytest

from agents_orchestrator.spec import SLUG_MAX, generated_slug, slugify, validate_slug


@pytest.mark.parametrize(
    ("text", "max_len", "expected"),
    [
        ("Hello World", SLUG_MAX, "hello-world"),
        ("  --a__b//c--  ", SLUG_MAX, "a-b-c"),
        ("UPPER123", SLUG_MAX, "upper123"),
        ("a   b", SLUG_MAX, "a-b"),
        ("abcdef-ghij", 7, "abcdef"),
        ("abc-def", 4, "abc"),
        ("!!!", SLUG_MAX, ""),
        ("", SLUG_MAX, ""),
        ("x" * 100, SLUG_MAX, "x" * SLUG_MAX),
    ],
)
def test_slugify_dns_1123_form(text: str, max_len: int, expected: str) -> None:
    assert slugify(text, max_len) == expected


def test_generated_slug_shape_and_length() -> None:
    slug = generated_slug("My Preset")
    assert re.fullmatch(r"my-preset-[a-z]{4}", slug)
    validate_slug(slug)
    assert generated_slug("name", rand=lambda: "abcd") == "name-abcd"

    long = generated_slug("a" * 80)
    assert len(long) == SLUG_MAX
    assert re.fullmatch(r"a{35}-[a-z]{4}", long)
    validate_slug(long)

    cut = generated_slug("word " * 20, rand=lambda: "wxyz")
    assert len(cut) <= SLUG_MAX
    assert cut.endswith("-wxyz")
    assert "--" not in cut
    validate_slug(cut)


@pytest.mark.parametrize("name", ["orchestrator", "Orchestrator Prod", "orchestrator-x"])
def test_generated_slug_avoids_reserved_prefix(name: str) -> None:
    slug = generated_slug(name, rand=lambda: "abcd")
    assert slug.startswith("p-orchestrator")
    assert len(slug) <= SLUG_MAX
    validate_slug(slug)


def test_generated_slug_reserved_prefix_is_recut() -> None:
    slug = generated_slug("orchestrator-" + "z" * 60, rand=lambda: "abcd")
    assert slug.startswith("p-orchestrator-z")
    assert len(slug) == SLUG_MAX
    validate_slug(slug)


def test_generated_slug_fallback_base() -> None:
    assert generated_slug("!!! ???", rand=lambda: "abcd") == "cluster-abcd"
    assert generated_slug("", rand=lambda: "abcd") == "cluster-abcd"
    validate_slug(generated_slug("   "))
