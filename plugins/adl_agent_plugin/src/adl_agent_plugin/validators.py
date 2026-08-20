import datetime

from django.core.exceptions import ValidationError
from django.utils import timezone as dj_timezone
from django.utils.translation import gettext_lazy as _


def validate_start_date(value: datetime.datetime):
    """A collection start date names when history begins, so it is in the past.

    Kept identical in meaning to the FTP plugin's check of the same name: the
    two plugins share the field's semantics (core reads it through
    ``get_first_collection_date``), and an operator moving a station from FTP
    to the agent should meet the same rule.
    """
    if value is not None and value > dj_timezone.now():
        raise ValidationError(_("Start date should be in the past"))
