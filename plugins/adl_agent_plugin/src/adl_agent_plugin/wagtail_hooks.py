import django_filters
from django.db.models import Q
from django.urls import path
from django.utils.translation import gettext_lazy as _
from wagtail import hooks
from wagtail.admin.filters import DateRangePickerWidget, WagtailFilterSet
from wagtail.snippets.models import register_snippet
from wagtail.snippets.views.snippets import SnippetViewSet

from .bulk_actions import ReprocessBulkAction
from .models import (
    AgentCyclePass,
    AgentCyclePassOutcome,
    AgentStationDataFile,
)
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


#: How each outcome is asked for in SQL. The property on the model reads the
#: same three columns in the same order; these are the query form of it,
#: written beside the listing that needs them so that a filter and the label
#: beside it cannot come to mean different things.
#:
#: ``failed`` is checked before ``uploaded`` for the reason the model checks
#: it first: a pass that sent nine files and lost one is a pass somebody needs
#: to look at, and calling it delivered would hide exactly the row worth
#: seeing.
CYCLE_PASS_OUTCOME_QUERIES = {
    AgentCyclePassOutcome.CUT_SHORT: Q(completed=False),
    AgentCyclePassOutcome.FAILED: Q(completed=True, failed__gt=0),
    AgentCyclePassOutcome.DELIVERED: (
        Q(completed=True, uploaded__gt=0)
        & (Q(failed=0) | Q(failed__isnull=True))
    ),
    AgentCyclePassOutcome.QUIET: (
        Q(completed=True)
        & (Q(failed=0) | Q(failed__isnull=True))
        & (Q(uploaded=0) | Q(uploaded__isnull=True))
    ),
}


class AgentCyclePassFilterSet(WagtailFilterSet):
    """The four questions anybody actually brings to this listing.

    *Which machine*, *which station*, *what kind of pass*, and *when* -- plus
    the outcome, which is the one that makes the fleet-wide question askable:
    "every failed pass this week, across every device" was not a question ADL
    could answer at all before these rows existed.
    """

    outcome = django_filters.ChoiceFilter(
        choices=AgentCyclePassOutcome.CHOICES,
        method="filter_outcome",
        label=_("Outcome"),
        empty_label=_("Any outcome"),
    )
    time = django_filters.DateFromToRangeFilter(
        label=_("Pass date"), widget=DateRangePickerWidget,
    )

    def filter_outcome(self, queryset, name, value):
        """Ask for a derived word as a query over the columns behind it."""
        query = CYCLE_PASS_OUTCOME_QUERIES.get(value)

        return queryset if query is None else queryset.filter(query)

    class Meta:
        model = AgentCyclePass
        fields = ["device", "station_link", "trigger", "outcome", "time"]


class AgentCyclePassViewSet(SnippetViewSet):
    """What every machine's collection has been doing, pass by pass.

    ADL used to hold one cycle's worth of this and overwrite it every five
    minutes. These are the rows behind that -- one per station per unit pass
    (wmo-raf/adl#307) -- and the listing exists for two questions the fleet
    view cannot answer: what has *this* station been doing for a fortnight,
    and which passes failed this week anywhere.

    "Did not arrive" is a column rather than something to open a row for,
    because it is the answer to the question that brings anybody here: a
    station that has gone quiet, and a folder that has filled up with files
    whose names no longer match.

    Read-only by construction. Nothing here is ADL's to set -- it is what a
    machine reported.
    """

    model = AgentCyclePass
    icon = "history"
    menu_label = _("Agent Cycles")
    list_per_page = 50
    inspect_view_enabled = True
    add_to_admin_menu = False
    copy_view_enabled = False
    list_display = [
        "time", "station_link", "outcome_label", "trigger",
        "scanned", "uploaded", "failed", "backlog", "missing_summary",
    ]
    search_fields = ["unit", "error"]
    filterset_class = AgentCyclePassFilterSet


register_snippet(AgentCyclePassViewSet)
