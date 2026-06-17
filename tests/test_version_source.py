"""Guard the single authoritative version source (#114).

``pyproject.toml`` is the only hand-maintained version literal; release-please
bumps it on release and the build embeds it in the installed dist metadata.
``src/caw/__init__.py`` derives ``__version__`` from that metadata rather than
duplicating it. These tests fail if the sources ever disagree, catching drift
before it ships.
"""

import importlib.metadata
import tomllib
from pathlib import Path

import caw

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        value = tomllib.load(fh)["project"]["version"]
    assert isinstance(value, str)
    return value


def test_dunder_version_matches_installed_metadata() -> None:
    assert caw.__version__ == importlib.metadata.version("caw")


def test_installed_metadata_matches_the_pyproject_literal() -> None:
    assert importlib.metadata.version("caw") == _pyproject_version()


def test_init_does_not_hardcode_a_version_literal() -> None:
    # The sole literal lives in pyproject.toml; __init__ must derive, not copy it.
    init_src = (Path(caw.__file__).parent / "__init__.py").read_text(encoding="utf-8")
    assert _pyproject_version() not in init_src
