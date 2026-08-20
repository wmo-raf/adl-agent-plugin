"""Shared arrangement for the agent plugin's HTTP-seam tests."""

from django.core.cache import cache
from django.urls import reverse

from adl_agent_plugin.throttling import AgentPairThrottle

PAIR_URL = reverse("plugins:adl_agent:pair")
ME_URL = reverse("plugins:adl_agent:device_me")


def clear_pair_throttle(ident="127.0.0.1"):
    """Forget what the pair endpoint has seen from ``ident``.

    ADL's cache is Redis, shared and long-lived, so the throttle history
    outlives a test run and would otherwise leak between tests. Deleting the
    one key this scope uses is surgical where ``cache.clear()`` would not be.
    """
    key = AgentPairThrottle.cache_format % {
        "scope": AgentPairThrottle.scope, "ident": ident,
    }
    cache.delete(key)


def configured_pair_attempts():
    """How many pair attempts the throttle allows per window."""
    return AgentPairThrottle().num_requests


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}
