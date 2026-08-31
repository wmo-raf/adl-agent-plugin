from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.timesince import timesince
from django.utils.translation import gettext_lazy as _
from wagtail.admin.views import generic
from wagtail.admin.viewsets import ViewSetGroup
from wagtail.admin.viewsets.model import ModelViewSet

from .models import AgentDevice, AgentRelease


def uptime_display(seconds):
    """A service uptime a person can read, or ``None`` for one not reported.

    ``timesince`` rather than a hand-rolled formatter: it is what the rest of
    the admin speaks ("2 days, 3 hours"), it localizes itself, and it rounds
    the way a person reading a diagnostic wants -- two units, largest first.
    Below its one-minute floor the seconds are shown as they are, because "0
    minutes" reads as a machine that never said anything.
    """
    if not seconds:
        return None
    if seconds < 60:
        return _("%(seconds)s seconds") % {"seconds": int(seconds)}
    now = timezone.now()
    return timesince(now - timedelta(seconds=seconds), now)


class AgentDeviceInspectView(generic.InspectView):
    """What the machine last said about itself, on a page of its own.

    The fleet listing answers "which machines are in trouble"; this answers
    the next question, which is "what is wrong with this one". So it shows
    the reading behind the state -- when the last heartbeat and the last
    completed cycle were, what the machine is running, how skewed its clock
    is, what its disks have left -- the per-station scan counts, which are
    the difference between "this country is quiet" and "this country is fine
    except for Garissa", and the machine's recent collection passes.

    A page rather than panels on the edit form (where all of this used to
    live), because reading a machine is not editing it: an operator arrives
    here from the device listing's *Inspect* button or from a connection
    row's *Device info* button, and nothing on the page is a form field.

    Read-only by construction: nothing here is ADL's to set. It is what a
    machine reported, or the absence of a machine having reported.
    """

    page_title = _("Device info")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .heartbeat import read_details
        from .models import AgentCyclePass, AgentStationLink
        from .panels import AgentRecentCyclesPanel

        device = self.object
        details = read_details(device.heartbeat_details)

        # The heartbeat names stations by station link id, which is the one
        # spelling both sides can agree on -- but an operator reads names.
        # One bounded query for the whole page; a link the machine reported
        # that ADL no longer has falls back to its number in the template.
        link_ids = [
            entry.get("station_link_id")
            for entry in details["links"]
            if entry.get("station_link_id") is not None
        ]
        station_names = {
            link.pk: str(link.station)
            for link in AgentStationLink.objects.filter(
                pk__in=link_ids
            ).select_related("station")
        }
        details["links"] = [
            {**entry, "station": station_names.get(entry.get("station_link_id"))}
            for entry in details["links"]
        ]

        context.update({
            "device": device,
            "liveness": device.liveness,
            **details,
            "uptime_display": uptime_display(details["uptime_seconds"]),
            # Newest first and bounded, here as everywhere: the whole
            # history is a listing's job.
            "transitions": device.state_transitions.all()[:10],
            # The same rows, template and bound the station link's
            # recent-cycles panel renders, so the two pages cannot come to
            # differ about what "recent" means.
            "passes": AgentCyclePass.objects.filter(
                device=device
            ).select_related("station_link__station")[:AgentRecentCyclesPanel.MOST],
            "listing_url": "%s?%s" % (
                reverse("wagtailsnippets_adl_agent_plugin_agentcyclepass:list"),
                urlencode({"device": device.pk}),
            ),
            "show_station": True,
        })

        return context


class AgentDeviceViewSet(ModelViewSet):
    """The fleet: every machine, what it is running, and how it is doing.

    Two listings would be one too many, so this is both the CRUD an
    administrator enrolls a machine from and the fleet view an operator scans
    down when a country goes quiet (decision #264). The columns are chosen
    for the second job -- version, liveness, when it was last heard from, how
    far its clock has drifted, and the version it is pinned to -- because the
    first job is done once per machine and the second is done every morning.

    Credentials are absent from the form on purpose: nothing here is
    typeable. The device's identity state and the two actions that change it
    are rendered by ``AgentDeviceIdentityPanel`` inside the edit form (see
    ``AgentDevice.panels``); the detail behind its liveness is the inspect
    page, ``AgentDeviceInspectView``.
    """

    model = AgentDevice
    icon = "desktop"
    menu_label = "Agent Devices"
    # Reached through Settings > ADL Agent rather than off the top-level menu:
    # enrolling a machine and pinning it to a release are administration, and
    # they sit next to the other things an administrator configures once.
    add_to_admin_menu = False
    copy_view_enabled = False
    inspect_view_enabled = True
    inspect_view_class = AgentDeviceInspectView
    inspect_template_name = "adl_agent_plugin/device_inspect.html"
    #: Never rendered -- the template above draws the whole page -- but the
    #: base view builds a context entry per field named here, so naming one
    #: keeps it from walking every column on the model.
    inspect_view_fields = ["name"]
    list_display = [
        "name",
        "agent_version",
        "liveness_label",
        "last_heartbeat_at",
        # Both halves of "last seen": the heartbeat is the signal the ladder
        # is counted in, and last_seen_at is every other call the machine
        # makes -- a machine mid-upload but between heartbeats is not silent,
        # and an operator scanning the fleet should be able to see that.
        "last_seen_at",
        "clock_skew_display",
        "pinned_version",
        "status_label",
    ]
    search_fields = ["name", "description", "agent_version"]
    #: The stored state, not the computed one -- a filter has to be a query.
    #: The two agree to within one sweep, which is a minute.
    list_filter = ["liveness_state"]


agent_device_viewset = AgentDeviceViewSet("agent_devices")


class AgentReleaseViewSet(ModelViewSet):
    """What this instance can offer its fleet, and what it is offering now.

    An operator reads two things off this listing: which release the machines
    are on their way to, and whether anything is waiting for a decision. So
    the state and the source are columns -- a release mirrored from upstream
    arrives staged, and staying staged is a choice somebody has to make
    rather than a thing that happens by neglect.

    The packages are edited inside a release rather than beside it: a version
    with no package for a tier is a fleet half of which never updates, and
    that is easier to notice when the two live on one screen.
    """

    model = AgentRelease
    icon = "download"
    menu_label = "Agent Releases"
    add_to_admin_menu = False
    copy_view_enabled = False
    inspect_view_enabled = False
    list_display = [
        "version",
        "published_label",
        "source",
        "released_at",
        "created_at",
    ]
    search_fields = ["version", "notes"]
    list_filter = ["is_published", "source"]


agent_release_viewset = AgentReleaseViewSet("agent_releases")


class AgentSettingsViewSetGroup(ViewSetGroup):
    """The plugin's two administrative listings, folded into Settings.

    The submenu order is the order of ``items`` -- ``ViewSetGroup`` numbers
    them itself and ignores each viewset's own ``menu_order`` -- so devices
    come before releases, which is the order they are set up in.
    """

    menu_label = _("ADL Agent")
    menu_icon = "desktop"
    add_to_settings_menu = True
    # 910 rather than the 900 the wis2box and FTP groups both use: sharing a
    # value leaves the three ordered by tie-break, and this one belongs after
    # them.
    menu_order = 910

    items = [
        agent_device_viewset,
        agent_release_viewset,
    ]
