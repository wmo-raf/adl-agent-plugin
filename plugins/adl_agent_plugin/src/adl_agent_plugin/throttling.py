import os

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle

from .constants import (
    AGENT_PAIR_THROTTLE_RATE_ENV_VAR,
    AGENT_PAIR_THROTTLE_SCOPE,
    DEFAULT_AGENT_PAIR_THROTTLE_RATE,
)


class AgentPairThrottle(AnonRateThrottle):
    """Per-client limit on the unauthenticated pair endpoint.

    The rate is carried here rather than in DRF's
    ``DEFAULT_THROTTLE_RATES``: ADL builds that setting *after* it runs each
    plugin's settings hook, so a scope registered from a plugin is silently
    overwritten. Owning the rate means the plugin is throttled the moment it
    is installed, with nothing for an operator to remember to configure.

    Deployments that enroll unusually many machines at once can raise it
    with the ``ADL_AGENT_PAIR_THROTTLE_RATE`` environment variable, in DRF's
    own ``<n>/<period>`` notation.
    """

    scope = AGENT_PAIR_THROTTLE_SCOPE

    def get_rate(self):
        override = getattr(settings, "ADL_AGENT_PAIR_THROTTLE_RATE", None)

        return override or os.environ.get(
            AGENT_PAIR_THROTTLE_RATE_ENV_VAR, DEFAULT_AGENT_PAIR_THROTTLE_RATE
        )
