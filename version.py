# version.py — the single place the version lives.
#
# Every other component reads it from here. A version duplicated in the
# banner, in pyproject.toml and in a tag drifts apart silently, and the
# drift only surfaces when someone reports a bug against a build number
# that never existed.
#
# Semantic versioning: MAJOR.MINOR.PATCH.

# 1.2.0: MINOR. The MCP server joined the tree (`gitlab-cli mcp`,
# `gitlab-cli report`) and the error catalog, rename following, LFS and
# pending-deletion handling arrived. All additive: no command, flag or
# config key that 1.1.0 documented was removed or changed meaning.
#
# 1.1.0: MINOR, not PATCH and not MAJOR. Since 1.0.0 the remote audit
# gained an instance scope (a security-relevant restriction, but it only
# ever does LESS than before), the log and menu screens changed behaviour,
# a new user-namespace setting arrived off by default, and config.py was
# deleted — a module nothing imported, so no documented interface broke.
# Additive plus a narrowing of a dangerous default = MINOR.
__version__ = "1.2.0"

# Short form for the banner, which has a fixed-width box to fit.
__version_short__ = ".".join(__version__.split(".")[:2])
