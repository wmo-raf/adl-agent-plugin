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
