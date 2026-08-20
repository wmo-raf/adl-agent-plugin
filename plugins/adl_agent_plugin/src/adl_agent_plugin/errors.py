"""
The one way this API says no.

Every refusal an agent can meet -- a manifest too long, a file whose bytes
do not match its hash, a station link that is not this device's -- is raised
as one of these and rendered by a single helper in
:mod:`adl_agent_plugin.api_views`. That is what keeps the error envelope
(decision #266) the same shape wherever it comes from: a ``code`` an agent
switches on, a ``detail`` a technician reads, and whatever else that
particular refusal owes the caller.
"""

from rest_framework import status


class AgentRequestRejected(Exception):
    """A request ADL will not act on, and why.

    ``extra`` carries whatever the agent needs to do something about it --
    the limit it exceeded, the offending entries, the hash that was
    expected. It is merged into the response body beside ``code`` and
    ``detail``.
    """

    def __init__(self, code, detail, status_code=status.HTTP_400_BAD_REQUEST,
                 **extra):
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.extra = extra
        super().__init__(detail)
