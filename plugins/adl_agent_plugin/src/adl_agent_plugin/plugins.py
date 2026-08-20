from adl.core.registries import Plugin
from adl_ftp_plugin.processing import add_values_saved
from django.urls import include, path

from .drain import drain_station_link, resolve_connection_decoder
from .models import AgentStationDataFile


class AdlAgentPlugin(Plugin):
    """Server side of the ADL Agent.

    The agent inverts ADL's usual direction of travel: instead of ADL
    dialing out to a country server, the country server pushes its files
    outbound over HTTPS. This plugin is what those pushes arrive at.

    It owns device identity -- who is allowed to push at all --
    configuration (which folders on which machine hold which station's
    files, read by the agent from ``sync`` and written back, in the part
    that is the machine's to decide, through the station link config
    endpoint), the file ledger (what each machine has been asked for, what
    has arrived, and where those bytes are staged), and the drain that turns
    those staged files into observations.

    The drain is an ordinary ``get_station_data``: core resolves the window,
    holds the per-station lock, chunks and upserts the records, and writes the
    activity log, exactly as it does for a plugin that dialed out for them.
    The only thing unusual about this plugin is where the bytes came from.
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
        """Every record staged for this station and not yet turned into one.

        The window core resolves is not what selects the files -- the ledger
        is, by status -- but it still applies to what comes out of them: a
        record older than the station's collection start date, or older than
        what ADL already holds, is core's to reject as it would be from any
        other source.
        """
        logger = self.get_logger()

        decoder = resolve_connection_decoder(
            station_link.network_connection, task_logger=logger,
        )

        if decoder is None:
            return

        # Read once and held: this is both what the run reports it had to work
        # with and what it goes on to drain, and asking twice would let the two
        # disagree.
        waiting = list(AgentStationDataFile.waiting_for(station_link))

        # Duck-typed sources-count handover: core stores this on the run's
        # activity log so "looked, found nothing" (0) stays distinguishable
        # from "never looked" (None). For a push-based plugin the source items
        # are the files that arrived and are still waiting -- counted here,
        # before any of them is decoded, so a decode bug cannot read as the
        # machine having sent nothing.
        station_link.adl_sources_count = len(waiting)

        yield from drain_station_link(
            station_link, waiting, decoder, task_logger=logger,
        )

    def after_save_records(self, station_link, station_records, saved_records, qc_fail_results=None):
        """Per-file bookkeeping for the drain.

        Core calls this after each chunk it upserts; the count goes to
        whichever file's counting window is open (see the shared pipeline in
        ``adl_ftp_plugin.processing``), so the number stamped on a file is the
        observation values that actually reached the database -- not the
        records its decoder produced.
        """
        add_values_saved(station_link, len(saved_records))
