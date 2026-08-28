"""Project-wide error hierarchy. Every raised error in jarvis derives from JarvisError."""


class JarvisError(Exception):
    """Base. Every raised error in the project derives from this."""

    exit_code: int = 1


class UserError(JarvisError):
    """Bad arguments, bad manifest."""

    exit_code = 1


class IntegrityError(JarvisError):
    """HALT conditions."""

    exit_code = 2


class GateNotMetError(JarvisError):
    """A gate returned a negative."""

    exit_code = 3


class ConfigError(UserError):
    """Configuration is missing, malformed, or violates a frozen decision."""


class IdError(UserError):
    """An identifier is malformed or cannot be generated as requested."""


class HashingError(UserError):
    """A value cannot be canonically serialised or hashed."""


class DirtyTreeError(IntegrityError):
    """The working tree has uncommitted changes where a clean tree is required."""


class ProvenanceError(IntegrityError):
    """Data provenance cannot be established or is inconsistent."""


class DataCoverageError(IntegrityError):
    """Required data coverage is missing for the requested range."""


class VaultViolation(IntegrityError):
    """An operation would violate the vault's write-once/single-reader rules."""


class LookaheadError(IntegrityError):
    """A computation would use information not yet available at decision time."""


class LifecycleError(IntegrityError):
    """An entity was used outside its declared lifecycle state."""


class SessionError(IntegrityError):
    """A trading session boundary or definition is invalid."""


class AmbiguousTimeError(IntegrityError):
    """A local time cannot be unambiguously resolved to a UTC instant (DST fold/gap)."""
