"""User-facing launcher errors."""


class TechSaraError(RuntimeError):
    """A safe, actionable failure that may be printed without a traceback."""


class PrerequisiteError(TechSaraError):
    """A host prerequisite must be installed or enabled by the user."""


class UnsafeOverrideError(TechSaraError):
    """A requested profile or model would exceed a conservative budget."""


class OfflineError(TechSaraError):
    """A required cached artifact is unavailable in offline mode."""
