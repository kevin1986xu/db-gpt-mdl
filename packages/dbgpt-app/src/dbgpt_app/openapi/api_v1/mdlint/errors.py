"""Error types for the DB-GPT/Wren MDL integration."""


class MdlError(Exception):
    """Base class for MDL integration failures."""


class MdlConfigError(MdlError):
    """The configured MDL manifest is missing or invalid."""


class MdlPolicyError(MdlError):
    """A query violates table, column, or row access policy."""


class MdlWrenUnavailableError(MdlError):
    """Wren is required but unavailable in the current Python environment."""
