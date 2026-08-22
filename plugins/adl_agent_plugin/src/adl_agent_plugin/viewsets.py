from wagtail.admin.viewsets.model import ModelViewSet

from .models import AgentDevice, AgentRelease


class AgentDeviceViewSet(ModelViewSet):
    """The fleet: every machine, what it is running, and how it is doing.

    Two listings would be one too many, so this is both the CRUD an
    administrator enrolls a machine from and the fleet view an operator scans
    down when a country goes quiet (decision #264). The columns are chosen
    for the second job -- version, liveness, when it was last heard from, how
    far its clock has drifted, and the version it is pinned to -- because the
    first job is done once per machine and the second is done every morning.

    Credentials are absent from the form on purpose: nothing here is
    typeable. The device's identity state, the two actions that change it,
    and the detail behind its liveness are rendered by
    ``AgentDeviceIdentityPanel`` and ``AgentDeviceLivenessPanel`` inside the
    edit form (see ``AgentDevice.panels``).
    """

    model = AgentDevice
    icon = "desktop"
    menu_label = "Agent Devices"
    menu_order = 500
    add_to_admin_menu = True
    copy_view_enabled = False
    inspect_view_enabled = False
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
    menu_order = 510
    add_to_admin_menu = True
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
