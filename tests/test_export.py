"""Tests for the pending.nix renderer."""

from prpkgs.export import _escape, render_pending_nix
from prpkgs.models import PendingPackage


def test_escape_plain():
    assert _escape("1.2.3") == "1.2.3"


def test_escape_quotes_and_backslash():
    assert _escape('a "b" c\\d') == 'a \\"b\\" c\\\\d'


def test_escape_antiquotation():
    # a version like `${recoll.version}` must not render as a live Nix
    # interpolation of an unbound variable.
    assert _escape("${recoll.version}") == "\\${recoll.version}"


def _pkg(**kw):
    base = dict(
        pr_number=1,
        name="foo",
        author="someone",
        pr_url="",
        pr_title="",
        pr_created_at="",
        attr_path="foo",
        head_rev="deadbeef",
        nar_hash="sha256-AAA=",
    )
    base.update(kw)
    return PendingPackage(**base)


def test_render_escapes_antiquotation_version():
    pkg = _pkg(version="${recoll.version}")
    rendered, _ = render_pending_nix([pkg])
    # the opener is escaped, so Nix reads it as a literal string
    assert 'version = "\\${recoll.version}";' in rendered
    assert 'version = "${recoll.version}";' not in rendered
