from wagtail.admin.viewsets.model import ModelViewSet

from .models import AgentDevice


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
