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
    # decisions, not code ones: an organisation running its own release host
    # points the index somewhere else, and an instance behind a locked-down
    # egress policy leaves the mirror alone and uploads releases in the admin
    # instead. Empty means "the default in ``mirror``", which is where the
    # agent's own build publishes.
    settings.ADL_AGENT_RELEASE_INDEX_URL = os.getenv(
        "ADL_AGENT_RELEASE_INDEX_URL", "",
    )

    # Opt-in, deliberately. Mirroring gives this instance a standing nightly
    # outbound dependency on a host outside the country running it, and an
    # instance should not acquire one because somebody upgraded a plugin.
    settings.ADL_AGENT_RELEASE_MIRROR_ENABLED = os.getenv(
        "ADL_AGENT_RELEASE_MIRROR_ENABLED", "false",
    ).strip().lower() in ("1", "true", "yes", "on")

    # How long this instance keeps the collection history its machines send,
    # and when it turns the older part of it into columns. Deployment-wide
    # and read from the environment, like ADL_AGENT_CONCURRENT_UPLOADS and
    # ADL_AGENT_CYCLE_STUCK_MULTIPLIER: how much disk a country's diagnostic
    # history is worth is a decision about that country, not about any one
    # machine.
    #
    # Not defaulted here. ``adl_agent_plugin.cycles`` reads the environment
    # itself and falls back to its own numbers, so an unset variable and a
    # mistyped one behave the same way -- which is what stops an instance
    # failing to start over a diagnostic table.
    for name in (
        "ADL_AGENT_CYCLE_COMPRESS_AFTER_DAYS",
        "ADL_AGENT_CYCLE_RETENTION_DAYS",
    ):
        value = os.getenv(name)

        if value is not None:
            setattr(settings, name, value)
