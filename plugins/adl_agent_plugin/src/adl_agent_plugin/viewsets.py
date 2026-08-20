from wagtail.admin.viewsets.model import ModelViewSet

from .models import AgentDevice


class AgentDeviceViewSet(ModelViewSet):
    """Admin CRUD for agent devices.

    Credentials are absent from the form on purpose -- nothing here is
    typeable. The device's identity state, and the two actions that change
    it, are rendered by ``AgentDeviceIdentityPanel`` inside the edit form
    (see ``AgentDevice.panels``).
    """

    model = AgentDevice
    icon = "desktop"
    menu_label = "Agent Devices"
    menu_order = 500
    add_to_admin_menu = True
    copy_view_enabled = False
    inspect_view_enabled = False
    list_display = ["name", "status_label", "last_seen_at", "created_at"]
    search_fields = ["name", "description"]


agent_device_viewset = AgentDeviceViewSet("agent_devices")
