"""
The re-process action, as an operator reaches it.

A decoder fix does not apply to one file; it applies to every file that
decoder has already read wrongly, which may be a country's whole quarter. So
the action is a bulk one: it is offered on the file listing, where the status
filter has already narrowed things down to the failures, and one press covers
whatever was ticked.

What the action itself does is only to carry a set of rows to
``reprocessing.reprocess`` and report back what became of them. The decision
of which route each file takes is not made here, and is deliberately not
offered to the operator: see that module.
"""

from django.shortcuts import get_list_or_404
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from wagtail.snippets.bulk_actions.snippet_bulk_action import SnippetBulkAction
from wagtail.snippets.permissions import get_permission_name

from .models import AgentStationDataFile
from .reprocessing import reprocess


class ReprocessBulkAction(SnippetBulkAction):
    display_name = _("Re-process")
    action_type = "reprocess"
    aria_label = _("Re-process selected files")
    template_name = "adl_agent_plugin/bulk_actions/confirm_reprocess.html"
    action_priority = 10

    #: Only the ledger. ``SnippetBulkAction`` offers an action on every
    #: registered snippet by default, and "re-process" means nothing to any
    #: other model on the instance.
    models = [AgentStationDataFile]

    @classmethod
    def get_queryset(cls, model, object_ids):
        """The ticked rows, carrying the station link each one belongs to.

        A decoder fix is ticked a hundred rows at a time, and every one of
        them is about to be asked which station link it is on. The connection
        behind that link is polymorphic and is read separately, in one query,
        by ``reprocessing.reprocess``.
        """
        return get_list_or_404(
            model.objects.select_related("station_link"), pk__in=object_ids,
        )

    def check_perm(self, data_file):
        """Re-processing is a write, so seeing the listing is not enough.

        It resets a file ADL has already accounted for, or asks a machine in
        the field for bytes again. Checked once per request rather than per
        row, as Wagtail's own snippet actions do: the permission is on the
        model, not on the object.
        """
        if getattr(self, "_can_reprocess", None) is None:
            self._can_reprocess = self.request.user.has_perm(
                get_permission_name("change", self.model)
            )

        return self._can_reprocess

    @classmethod
    def execute_action(cls, objects, user=None, **kwargs):
        # The two numbers a bulk action may return are named for parents and
        # children, which this action has neither of. They are the two routes
        # a re-process takes, and the success message below is the only thing
        # that reads them.
        outcome = reprocess(objects)

        return len(outcome.redecoded), len(outcome.reoffered)

    def get_success_message(self, num_redecoded, num_reoffered):
        parts = []

        if num_redecoded:
            parts.append(ngettext(
                "%(count)d file is being decoded again.",
                "%(count)d files are being decoded again.",
                num_redecoded,
            ) % {"count": num_redecoded})

        if num_reoffered:
            parts.append(ngettext(
                "%(count)d file has been asked for again, and arrives on its "
                "machine's next cycle.",
                "%(count)d files have been asked for again, and arrive on "
                "their machines' next cycles.",
                num_reoffered,
            ) % {"count": num_reoffered})

        return " ".join(parts) or _("Nothing was re-processed.")
