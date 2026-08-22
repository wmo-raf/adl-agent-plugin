def setup(settings):
    """
    This function is called after adl has setup its own Django settings file but
    before Django starts. Read and modify provided settings object as appropriate
    just like you would in a normal Django settings file. E.g.:

    settings.INSTALLED_APPS += ["some_custom_plugin_dep"]

    Note for this plugin: the pair endpoint's rate limit deliberately does NOT
    live here. ADL assembles ``REST_FRAMEWORK`` after running this hook, so a
    throttle scope registered here would be overwritten before Django starts.
    ``AgentPairThrottle`` carries its own rate instead.
    """
    import os

    # Where this instance mirrors agent releases from, and whether it does.
    #
    # Both are environment-configurable because they are deployment
    # decisions, not code ones: an instance behind a locked-down egress
    # policy turns the mirror off and its operator uploads releases in the
    # admin instead, and an organisation running its own release host points
    # the index somewhere else. Empty means "the default in ``mirror``",
    # which is where the agent's own build publishes.
    settings.ADL_AGENT_RELEASE_INDEX_URL = os.getenv(
        "ADL_AGENT_RELEASE_INDEX_URL", "",
    )
    settings.ADL_AGENT_RELEASE_MIRROR_ENABLED = os.getenv(
        "ADL_AGENT_RELEASE_MIRROR_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")
