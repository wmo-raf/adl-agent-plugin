from django.urls import reverse
from django.utils.http import urlencode
from wagtail.admin.panels import Panel


class AgentDeviceIdentityPanel(Panel):
    """The device's credential state, shown inside its edit form.

    None of what this panel shows is editable -- a pairing code and a token
    are minted by ADL, never typed by an administrator -- so it renders a
    read-out plus links to the two actions that change it. The pairing code
    itself is shown here in full: it exists to be relayed to whoever is
    standing at the machine, and an administrator who cannot re-read it ends
    up rotating the device just to see a code again.
    """

    class BoundPanel(Panel.BoundPanel):
        template_name = "adl_agent_plugin/panels/device_identity.html"

        def is_shown(self):
            # A device that has not been saved yet has no credentials to
            # report, and the actions below need a pk to address.
            return self.instance is not None and self.instance.pk is not None

        def get_context_data(self, parent_context=None):
            context = super().get_context_data(parent_context)
            device = self.instance

            context.update({
                "device": device,
                "status_label": device.status_label,
                "status_tone": device.status_tone,
                "pairing_code": (
                    device.pairing_code if device.pairing_code_is_valid else None
                ),
                "pairing_code_expires_at": device.pairing_code_expires_at,
                "issue_code_url": reverse(
                    "agent_device_issue_pairing_code", args=[device.pk]
                ),
                "revoke_url": reverse("agent_device_revoke", args=[device.pk]),
            })

            return context


class AgentRecentCyclesPanel(Panel):
    """The last few collection passes, on whatever page you were already on.

    The "Agent Cycles" listing answers the fleet-wide questions; this answers
    the one an operator has while looking at a particular machine or a
    particular station, which is "and what has it actually been doing". Before
    these rows existed ADL could not answer it at all -- it held one cycle's
    worth and overwrote it every five minutes (wmo-raf/adl#307).

    Newest first and bounded, exactly as the liveness panel bounds its state
    transitions: an edit form is not a listing, and the link at the foot of it
    goes to the one that is -- already filtered to what you were looking at.

    One panel for both pages, told which column it is filtering on. Two
    near-identical panels is how the device's and the station's idea of
    "recent" quietly come to differ.
    """

    #: Enough to see a pattern -- half a dozen ten-minute cycles is an hour --
    #: and few enough not to turn an edit form into a log file.
    MOST = 10

    def __init__(self, lookup, **kwargs):
        super().__init__(**kwargs)
        self.lookup = lookup

    def clone_kwargs(self):
        return {**super().clone_kwargs(), "lookup": self.lookup}

    class BoundPanel(Panel.BoundPanel):
        template_name = "adl_agent_plugin/panels/recent_cycles.html"

        def is_shown(self):
            return self.instance is not None and self.instance.pk is not None

        def get_context_data(self, parent_context=None):
            context = super().get_context_data(parent_context)
            from .models import AgentCyclePass

            lookup = self.panel.lookup
            listing = reverse(
                "wagtailsnippets_adl_agent_plugin_agentcyclepass:list"
            )

            context.update({
                "passes": AgentCyclePass.objects.filter(
                    **{lookup: self.instance}
                ).select_related("station_link__station")[:self.panel.MOST],
                # The same filter the rows were read under, so that "see all"
                # arrives at what the reader was already looking at rather
                # than at every pass this instance has ever stored.
                "listing_url": "%s?%s" % (
                    listing, urlencode({lookup: self.instance.pk}),
                ),
                # A device's passes name their station; a station's do not
                # need to.
                "show_station": lookup != "station_link",
            })

            return context
