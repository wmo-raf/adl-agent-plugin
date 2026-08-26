from django.urls import path
from django.utils.translation import gettext_lazy as _
from wagtail import hooks
from wagtail.admin.filters import WagtailFilterSet
from wagtail.snippets.models import register_snippet
from wagtail.snippets.views.snippets import SnippetViewSet

from .bulk_actions import ReprocessBulkAction
from .models import AgentStationDataFile
from .views import issue_pairing_code, revoke_device
from .viewsets import (
    AgentSettingsViewSetGroup,
    agent_device_viewset,
    agent_release_viewset,
)


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


# The two viewsets are still registered individually -- that is what builds
# their URLs; ``ViewSetGroup.on_register`` only puts the menu item up. Each
# one carries ``add_to_admin_menu = False``, so the single entry point is the
# group's Settings > ADL Agent submenu.
@hooks.register("register_admin_viewset")
def register_agent_viewsets():
    return [
        agent_device_viewset,
        agent_release_viewset,
        AgentSettingsViewSetGroup(),
    ]


# Offering re-processing on the file listing (story 21). A bulk action rather
# than a per-row button because the case it exists for is a decoder fix, which
# never applies to one file: an operator filters the listing down to a
# station's failures and asks for all of them at once.
#
# Registered by handing the class over rather than by decorating a function:
# this hook collects classes, not callables that return one.
hooks.register("register_bulk_action", ReprocessBulkAction)


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

    It is also where a fix gets applied: the re-process bulk action is offered
    on the rows ticked here (story 21). Whether a file's bytes are still
    staged decides which route that takes, so it is a column too.
    """

    model = AgentStationDataFile
    icon = "doc-full"
    menu_label = _("Agent Station Data Files")
    list_per_page = 50
    inspect_view_enabled = True
    list_display = [
        "file_name", "station_link", "status", "error_summary",
        "bytes_state", "received_at", "processed_at", "values_saved",
    ]
    search_fields = ["file_name"]
    filterset_class = AgentStationDataFileFilterSet


register_snippet(AgentStationDataFileViewSet)
