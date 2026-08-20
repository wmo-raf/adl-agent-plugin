"""Names shared between the plugin's Django settings hook and its views.

Kept import-free on purpose: ``config/settings/settings.py`` runs while ADL
is still assembling its settings module, before Django or DRF are usable,
so anything it reads has to be safe to import at that moment.
"""

#: DRF throttle scope for the unauthenticated pair endpoint.
AGENT_PAIR_THROTTLE_SCOPE = "agent_pair"

#: Attempts per client IP per hour. Generous for a whole office enrolling
#: machines in one afternoon, and worthless against a code space of 2**39.
DEFAULT_AGENT_PAIR_THROTTLE_RATE = "30/hour"

#: Deployments that pair unusually many machines at once can raise the rate.
AGENT_PAIR_THROTTLE_RATE_ENV_VAR = "ADL_AGENT_PAIR_THROTTLE_RATE"
