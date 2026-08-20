from adl.core.registries import plugin_registry
from django.apps import AppConfig


class AdlAgentPluginConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = "adl_agent_plugin"

    def ready(self):
        from .plugins import AdlAgentPlugin

        plugin_registry.register(AdlAgentPlugin())
