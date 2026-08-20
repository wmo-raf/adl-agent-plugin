import os

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle

#: Attempts per client IP per hour. Generous for a whole office enrolling
#: machines in one afternoon, and worthless against a code space of 2**39.
DEFAULT_PAIR_THROTTLE_RATE = "30/hour"

#: Deployments that pair unusually many machines at once can raise the rate.
#: Read from the environment for operators and from Django settings for
#: tests; same name either way.
PAIR_THROTTLE_RATE_SETTING = "ADL_AGENT_PAIR_THROTTLE_RATE"


class AgentPairThrottle(AnonRateThrottle):
    """Per-client limit on the unauthenticated pair endpoint.

    The rate is carried here rather than in DRF's
    ``DEFAULT_THROTTLE_RATES``: ADL builds that setting *after* it runs each
    plugin's settings hook, so a scope registered from a plugin is silently
    overwritten. Owning the rate means the plugin is throttled the moment it
    is installed, with nothing for an operator to remember to configure.

    Raise it with the ``ADL_AGENT_PAIR_THROTTLE_RATE`` environment variable,
    in DRF's own ``<n>/<period>`` notation.
    """

    scope = "agent_pair"

    def get_rate(self):
        override = getattr(settings, PAIR_THROTTLE_RATE_SETTING, None)

        return override or os.environ.get(
            PAIR_THROTTLE_RATE_SETTING, DEFAULT_PAIR_THROTTLE_RATE
        )
