from django.urls import path
from django.utils.translation import gettext_lazy as _
from wagtail import hooks
from wagtail.admin.filters import WagtailFilterSet
from wagtail.snippets.models import register_snippet
from wagtail.snippets.views.snippets import SnippetViewSet

from .models import AgentStationDataFile
from .views import issue_pairing_code, revoke_device
from .viewsets import agent_device_viewset


@hooks.register("register_admin_urls")
def urlconf_adl_agent_plugin():
    return [
        path(
            "adl-agent-plugin/devices/<int:pk>/issue-pairing-code/",
            issue_pairing_code,
            name="agent_device_issue_pairing_code",
        ),
        path(
            "adl-agent-plugin/devices/<int:pk>/revoke/",
            revoke_device,
            name="agent_device_revoke",
        ),
    ]


@hooks.register("register_admin_viewset")
def register_agent_device_viewset():
    return agent_device_viewset


class AgentStationDataFileFilterSet(WagtailFilterSet):
    class Meta:
        model = AgentStationDataFile
        fields = ["station_link", "status"]


class AgentStationDataFileViewSet(SnippetViewSet):
    """What each machine has sent, and what ADL made of it.

    The listing exists for one question above all: which files failed, and
    why (story 20). So the status and the first line of the error are columns
    rather than something to open each row for, and the status is a filter --
    an operator looking at a country wants its failures, not its thousands of
    quiet successes.
    """

    model = AgentStationDataFile
    icon = "doc-full"
    menu_label = _("Agent Station Data Files")
    list_per_page = 50
    inspect_view_enabled = True
    list_display = [
        "file_name", "station_link", "status", "error_summary",
        "received_at", "processed_at", "values_saved",
    ]
    search_fields = ["file_name"]
    filterset_class = AgentStationDataFileFilterSet


register_snippet(AgentStationDataFileViewSet)
