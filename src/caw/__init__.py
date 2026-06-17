"""caw: a local-first workflow kernel and CLI for composing agent CLI entrypoints."""

from importlib.metadata import version

# pyproject.toml is the single authoritative version source (release-please bumps it;
# the build embeds it in the installed dist metadata). __version__ derives from that
# metadata rather than duplicating the literal, so the two can never drift (#114).
__version__ = version("caw")
