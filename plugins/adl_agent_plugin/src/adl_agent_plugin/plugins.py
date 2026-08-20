from adl.core.registries import Plugin
from django.urls import include, path


class AdlAgentPlugin(Plugin):
    """Server side of the ADL Agent.

    The agent inverts ADL's usual direction of travel: instead of ADL
    dialing out to a country server, the country server pushes its files
    outbound over HTTPS. This plugin is what those pushes arrive at.

    So far it owns device identity -- who is allowed to push at all --
    configuration (which folders on which machine hold which station's
    files, read by the agent from ``sync`` and written back, in the part
    that is the machine's to decide, through the station link config
    endpoint), and the file ledger: what each machine has been asked for,
    what has arrived, and where those bytes are staged.

    The ``get_station_data`` drain that turns staged files into observations
    arrives in a later slice; until then the plugin registers so that its API
    and admin are installed, files are received and remembered, and ingestion
    yields nothing.
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
