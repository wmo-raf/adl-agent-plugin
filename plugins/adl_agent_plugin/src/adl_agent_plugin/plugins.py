from adl.core.registries import Plugin
from django.urls import include, path


class AdlAgentPlugin(Plugin):
    """Server side of the ADL Agent.

    The agent inverts ADL's usual direction of travel: instead of ADL
    dialing out to a country server, the country server pushes its files
    outbound over HTTPS. This plugin is what those pushes arrive at.

    So far it owns device identity -- who is allowed to push at all -- and
    configuration: which folders on which machine hold which station's
    files, read by the agent from ``sync`` and written back, in the part
    that is the machine's to decide, through the station link config
    endpoint.

    The staging store and the ``get_station_data`` drain that turns pushed
    files into observations arrive in later slices; until then the plugin
    registers so that its API and admin are installed, and ingestion yields
    nothing.
    """

    type = "adl_agent_plugin"
    label = "ADL Agent Plugin"

    def get_urls(self):
        # Core mounts every plugin's urls under "plugins/", so the agent's
        # versioned surface is served at /plugins/api/agent/v1/.
        return [
            path(
                "api/agent/v1/",
                include("adl_agent_plugin.urls", namespace="adl_agent"),
            ),
        ]

    def get_station_data(self, station_link, start_date=None, end_date=None):
        return []
