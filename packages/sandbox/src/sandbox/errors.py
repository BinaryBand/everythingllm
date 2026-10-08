"""The sandbox runner's errors, which its op callers see as their text and the apps server
(sandbox.appsweb) turns into HTTP statuses by their type."""

import hostrpc


class SandboxError(hostrpc.RunnerError):
    """An error to show the agent: bad arguments, a missing file, the runner being down."""


class Busy(SandboxError):
    """A run holds the workspace, so a file operation fails at once rather than waiting."""


class NoSuchApp(SandboxError):
    """There's no app of that name in the workspace."""


class BadToken(SandboxError):
    """A write-back that isn't from the app's page."""


class StaleToken(SandboxError):
    """A write-back from a page of the app's that has been rendered again since."""
