"""Tests for the PR-title parser."""

from prpkgs.parser import parse_pr_title


def _tuples(title):
    return [(p.attr_path, p.name, p.version) for p in parse_pr_title(title)]


def test_single_package():
    assert _tuples("foo: init at 1.0.0") == [("foo", "foo", "1.0.0")]


def test_namespaced_attr():
    assert _tuples("python3Packages.foo: init at 1.0.0") == [
        ("python3Packages.foo", "foo", "1.0.0"),
    ]


def test_multiple_packages_shared_version():
    out = _tuples("foo, bar: init at 1.0.0")
    assert ("foo", "foo", "1.0.0") in out
    assert ("bar", "bar", "1.0.0") in out


def test_multiple_packages_braced():
    out = _tuples("{foo, bar}: init at 1.0.0")
    assert ("foo", "foo", "1.0.0") in out
    assert ("bar", "bar", "1.0.0") in out


def test_multiple_packages_distinct_versions():
    out = _tuples("foo: init at 1.0.0, bar: init at 2.0.0")
    assert ("foo", "foo", "1.0.0") in out
    assert ("bar", "bar", "2.0.0") in out


def test_dashed_name():
    assert _tuples("tetro-tui: init at 0.1.0") == [("tetro-tui", "tetro-tui", "0.1.0")]


def test_fallback_to_prefix_when_no_init():
    out = _tuples("firefox: 100.0 -> 101.0")
    assert out == [("firefox", "firefox", None)]


def test_fallback_to_whole_title_when_unparseable():
    out = parse_pr_title("Add Pslab python lib")
    assert len(out) == 1
    assert out[0].version is None
    assert out[0].attr_path == ""
