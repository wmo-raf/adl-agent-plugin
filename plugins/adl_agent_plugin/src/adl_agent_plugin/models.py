from adl.core.models import DataParameter, NetworkConnection, StationLink, Unit
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import F
from django.db.models.signals import post_delete, post_save
from django.utils import timezone as dj_timezone
from django.utils.translation import gettext_lazy as _
from modelcluster.fields import ParentalKey
from timezone_field import TimeZoneField
from wagtail.admin.panels import FieldPanel, InlinePanel, MultiFieldPanel
from wagtail.models import Orderable

from .credentials import (
    PAIRING_CODE_TTL,
    ExpiredPairingCode,
    InvalidPairingCode,
    generate_device_token,
    generate_pairing_code,
    hash_device_token,
    normalize_pairing_code,
)
from .panels import AgentDeviceIdentityPanel
from .validators import validate_start_date


class AgentDeviceQuerySet(models.QuerySet):
    def active(self):
        return self.filter(revoked_at__isnull=True, token_hash__isnull=False)


class AgentDevice(models.Model):
    """One installed ADL Agent -- that is, one machine in one country.

    A device is an identity in its own right, not a stand-in for a user
    account (decision #259). Nobody's password ever reaches the Windows box:
    an administrator creates the device here, ADL mints a pairing code, a
    technician types that code into the installer, and the agent trades it
    for a long-lived token. The token is what every later call presents.

    One device will go on to serve several ``AgentConnection`` rows (a
    country server often hosts two vendors' folders), which is why identity
    lives on its own model rather than on the connection. Those connections
    arrive in a later slice; what exists here is the identity itself.

    The three lifecycle verbs an administrator has are all methods below:
    :meth:`issue_pairing_code` (also used for rotation),
    :meth:`redeem_pairing_code` (what the agent calls, via the pair
    endpoint) and :meth:`revoke`.
    """

    STATUS_PAIRED = "paired"
    STATUS_AWAITING_PAIRING = "awaiting_pairing"
    STATUS_REVOKED = "revoked"
    STATUS_UNPAIRED = "unpaired"

    STATUS_LABELS = {
        STATUS_PAIRED: _("Paired"),
        STATUS_AWAITING_PAIRING: _("Awaiting pairing"),
        STATUS_REVOKED: _("Revoked"),
        STATUS_UNPAIRED: _("Not paired"),
    }

    #: How each status reads in Wagtail's help-block vocabulary. Kept beside
    #: the statuses themselves so the admin template asks rather than
    #: re-deriving the ladder from raw status strings.
    STATUS_TONES = {
        STATUS_PAIRED: "help-info",
        STATUS_AWAITING_PAIRING: "help-warning",
        STATUS_REVOKED: "help-critical",
        STATUS_UNPAIRED: "help-warning",
    }

    name = models.CharField(
        max_length=255,
        unique=True,
        verbose_name=_("Name"),
        help_text=_(
            "How this machine is known to you -- typically the server's "
            "hostname or the office it sits in."
        ),
    )
    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
        help_text=_("Anything worth remembering about this machine."),
    )

    #: One loop per machine scans every folder it has been given, so the
    #: cadence is the device's, not the connection's (decision #260). Local
    #: scans are cheap, so staggering connections would buy nothing.
    check_interval_minutes = models.PositiveIntegerField(
        default=5,
        # A floor only. How often a site can afford to scan and upload is a
        # question about that site's link and disks, not one this model is
        # entitled to answer, so no ceiling is invented here.
        validators=[MinValueValidator(1)],
        verbose_name=_("Check interval (minutes)"),
        help_text=_(
            "How often this machine scans its folders and offers what it "
            "finds to ADL."
        ),
    )

    #: Monotonic counter over everything this device is configured with --
    #: its connections, its station links, and their variable mappings. The
    #: agent caches its configuration and re-reads it when this number moves,
    #: which is also what makes the shared tier's last-write-wins rule
    #: workable: nobody is told "conflict", the loser simply sees the version
    #: move and re-reads (decision #266). Bumped by the signal handlers at the
    #: foot of this module, never by hand.
    config_version = models.PositiveBigIntegerField(
        default=1, editable=False, verbose_name=_("Configuration version"),
    )

    # Nullable rather than blank-as-empty-string so that the unique
    # constraints below hold: any number of devices may have no pairing code
    # and no token at once, but no two may share one.
    pairing_code = models.CharField(
        max_length=9,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Pairing code"),
    )
    pairing_code_expires_at = models.DateTimeField(
        null=True, blank=True, editable=False,
        verbose_name=_("Pairing code expires at"),
    )
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Device token hash"),
    )
    paired_at = models.DateTimeField(
        null=True, blank=True, editable=False, verbose_name=_("Paired at"),
    )
    last_seen_at = models.DateTimeField(
        null=True, blank=True, editable=False, verbose_name=_("Last seen at"),
    )
    revoked_at = models.DateTimeField(
        null=True, blank=True, editable=False, verbose_name=_("Revoked at"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    objects = AgentDeviceQuerySet.as_manager()

    panels = [
        MultiFieldPanel([
            FieldPanel("name"),
            FieldPanel("description"),
            FieldPanel("check_interval_minutes"),
        ], heading=_("Device")),
        AgentDeviceIdentityPanel(heading=_("Identity")),
    ]

    class Meta:
        verbose_name = _("Agent Device")
        verbose_name_plural = _("Agent Devices")
        ordering = ["name"]

    def __str__(self):
        return self.name

    # ---------- state ----------

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_paired(self):
        return self.token_hash is not None and not self.is_revoked

    @property
    def pairing_code_is_valid(self):
        if not self.pairing_code or not self.pairing_code_expires_at:
            return False
        return self.pairing_code_expires_at > dj_timezone.now()

    @property
    def status(self):
        # Order matters, and it is the order of what is true *now*. A
        # working token wins over an outstanding rotation code, because the
        # machine is still shipping data. A live code wins over a past
        # revocation, because issuing one is an administrator readmitting
        # the machine -- reporting "Revoked" beside a code an administrator
        # has just handed out would describe neither.
        if self.is_paired:
            return self.STATUS_PAIRED
        if self.pairing_code_is_valid:
            return self.STATUS_AWAITING_PAIRING
        if self.is_revoked:
            return self.STATUS_REVOKED
        return self.STATUS_UNPAIRED

    @property
    def status_label(self):
        return self.STATUS_LABELS[self.status]

    @property
    def status_tone(self):
        return self.STATUS_TONES[self.status]

    # ``request.user`` is set to the device on agent endpoints, so it has to
    # answer the two questions Django asks of any user-shaped object. This is
    # not an authorization statement: what a device may do is decided by
    # ``IsAgentDevice``, and no core view accepts device authentication at all.
    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    # ---------- lifecycle ----------

    def save(self, *args, **kwargs):
        # A device with neither code nor token could never be paired, and an
        # administrator who has just created one wants the code now -- so a
        # brand new device is born holding one.
        if self._state.adding and not self.pairing_code and not self.token_hash:
            self._assign_pairing_code()
        super().save(*args, **kwargs)

    def _assign_pairing_code(self):
        """Put a fresh, unused code and its expiry on the instance."""
        for _attempt in range(10):
            code = generate_pairing_code()
            if not AgentDevice.objects.filter(pairing_code=code).exists():
                break
        else:  # pragma: no cover - 2**39 codes; ten collisions cannot happen
            raise RuntimeError("Could not generate an unused pairing code.")

        self.pairing_code = code
        self.pairing_code_expires_at = dj_timezone.now() + PAIRING_CODE_TTL

    def issue_pairing_code(self):
        """Issue a fresh pairing code and return it.

        This is both first-time enrollment and rotation. The device's current
        token deliberately keeps working: an administrator issuing a code for
        a machine that is still shipping data should not create a data gap
        between the click and the technician getting round to typing the code
        in. The old token dies the moment the new code is redeemed, in
        :meth:`redeem_pairing_code`.
        """
        self._assign_pairing_code()
        self.save(update_fields=["pairing_code", "pairing_code_expires_at",
                                 "updated_at"])
        return self.pairing_code

    def revoke(self):
        """Cut this machine off, now.

        Everything it could authenticate with goes: the token, so calls in
        flight start failing with 401, and any outstanding pairing code, so a
        compromised machine cannot simply re-enroll itself. Getting the device
        back means an administrator issuing a new code.
        """
        self.revoked_at = dj_timezone.now()
        self.token_hash = None
        self.pairing_code = None
        self.pairing_code_expires_at = None
        self.save(update_fields=["revoked_at", "token_hash", "pairing_code",
                                 "pairing_code_expires_at", "updated_at"])

    @classmethod
    @transaction.atomic
    def redeem_pairing_code(cls, raw_code):
        """Trade a pairing code for a device token.

        Returns ``(device, token)`` with the token in the clear -- the only
        time it is ever readable. Raises :class:`InvalidPairingCode` or
        :class:`ExpiredPairingCode` otherwise.

        The whole exchange is one transaction over a locked row, which is
        what makes the two guarantees in decision #259 true: the code is
        single-use (a successful redemption clears it, so a replay finds
        nothing), and rotation atomically replaces the token (the new digest
        overwrites the old one, and the old token stops authenticating the
        instant this commits).
        """
        code = normalize_pairing_code(raw_code)
        if not code:
            raise InvalidPairingCode()

        try:
            device = cls.objects.select_for_update().get(pairing_code=code)
        except cls.DoesNotExist:
            raise InvalidPairingCode()

        if not device.pairing_code_is_valid:
            # The dead code stays on the row. It is inert -- every later
            # attempt fails the same check -- and keeping it lets the admin
            # say "expired" rather than "there was never a code". It is
            # overwritten by the next issue, and destroyed by a revoke.
            raise ExpiredPairingCode()

        token = generate_device_token()
        now = dj_timezone.now()

        device.token_hash = hash_device_token(token)
        device.paired_at = now
        device.last_seen_at = now
        # Re-pairing a revoked device is how a machine comes back: the
        # administrator's act of issuing a code is the decision to readmit it.
        device.revoked_at = None
        device.pairing_code = None
        device.pairing_code_expires_at = None
        device.save(update_fields=["token_hash", "paired_at", "last_seen_at",
                                   "revoked_at", "pairing_code",
                                   "pairing_code_expires_at", "updated_at"])

        return device, token

    @classmethod
    def authenticate_token(cls, token):
        """Return the live device presenting ``token``, or ``None``.

        Revoked devices have no token digest at all, so they fall out of the
        lookup rather than needing a separate check.
        """
        if not token:
            return None

        try:
            return cls.objects.active().get(token_hash=hash_device_token(token))
        except cls.DoesNotExist:
            return None

    def current_config_version(self):
        """This device's configuration version, read from the database.

        Always re-read rather than trusted from the instance: the counter is
        moved by ``UPDATE`` statements from signal handlers, which can (and
        during a config write, do) fire after the instance in hand was
        loaded. Every response that carries a version calls this, so an
        agent is never handed a number that was already out of date when it
        was written.
        """
        current = AgentDevice.objects.filter(pk=self.pk).values_list(
            "config_version", flat=True
        ).first()

        if current is not None:
            self.config_version = current

        return self.config_version

    @classmethod
    def bump_config_version_for(cls, device_id):
        """Record that something in a device's configuration changed.

        Takes an id rather than an instance because the signal handlers at
        the foot of this module have only an id to hand -- and a single
        targeted UPDATE for the same reason :meth:`touch_last_seen` is one:
        it runs beside whatever else is writing the row, and must not carry
        a stale instance back to the database.
        """
        cls.objects.filter(pk=device_id).update(
            config_version=F("config_version") + 1
        )

    def bump_config_version(self):
        """Record that something in this device's configuration changed."""
        self.bump_config_version_for(self.pk)

    def touch_last_seen(self):
        """Record that this device just called in.

        A single targeted UPDATE, not a model save: this runs on every
        authenticated request and must not race with whatever else is
        writing the row.
        """
        now = dj_timezone.now()
        AgentDevice.objects.filter(pk=self.pk).update(last_seen_at=now)
        self.last_seen_at = now


class ReadOnlyConfigFields(Exception):
    """An agent tried to write configuration that is not its to write.

    Carries the offending names in two lists because they are two different
    mistakes: ``read_only`` is a field that exists but belongs to the admin
    tier, ``unknown`` is a field that does not exist at all -- most often a
    typo, which is worth saying out loud rather than silently dropping.
    """

    def __init__(self, read_only=(), unknown=()):
        self.read_only = list(read_only)
        self.unknown = list(unknown)
        super().__init__(str(self))

    @property
    def fields(self):
        return self.read_only + self.unknown

    def __str__(self):
        parts = []
        if self.read_only:
            parts.append(
                "%s is managed in the ADL admin and cannot be set from the app"
                % ", ".join(self.read_only)
            )
        if self.unknown:
            parts.append("%s is not a station link setting" % ", ".join(self.unknown))
        return "; ".join(parts) or "No configuration was written."


class AgentConnection(NetworkConnection):
    """One vendor's data on one machine.

    A country server often hosts two vendors' software writing into two
    different folder trees; each gets its own connection, and all of them
    belong to the one :class:`AgentDevice` installed on that machine
    (decision #259) -- which is why the device is a field here rather than
    the other way round.

    Everything on this model is admin-only tier: which machine sends the
    data, and what the data means. The machine's own knowledge -- where the
    files sit and how they are named -- lives on the station links below and
    is writable from the app (decision #260).
    """

    station_link_model_string_label = "adl_agent_plugin.AgentStationLink"

    #: The agent inverts ADL's usual direction of travel: there is no host to
    #: dial and no credential to present outbound, because the country server
    #: pushes to us. Declaring that keeps layers 4 and 5 of the ingestion
    #: diagnostic reporting NOT_APPLICABLE instead of inventing a verdict
    #: about a network call ADL never makes. Agent liveness is reported
    #: separately, from heartbeats.
    has_external_source = False

    device = models.ForeignKey(
        AgentDevice,
        # A device with connections is not deletable, and deliberately so:
        # deleting one would take a country's station links -- and their
        # folder configuration -- with it. Revoke cuts a machine off; delete
        # is for a device that was never wired up.
        on_delete=models.PROTECT,
        related_name="connections",
        verbose_name=_("Agent Device"),
        help_text=_("The machine that sends this connection's files."),
    )

    panels = NetworkConnection.panels + [
        FieldPanel("device"),
        InlinePanel(
            "variable_mappings",
            label=_("Variable Mapping"),
            heading=_("Variable Mappings"),
            help_text=_(
                "How this vendor's file columns map onto ADL parameters. "
                "Serves every station on this connection unless a station "
                "overrides a parameter."
            ),
        ),
    ]

    class Meta:
        verbose_name = _("Agent Connection")
        verbose_name_plural = _("Agent Connections")


class AgentConnectionVariableMapping(Orderable):
    """The connection-wide half of Pattern C.

    Vendor software writes the same column names for every station it
    serves, so the mapping is stated once here; a station that disagrees
    overrides the parameter on its own link.
    """

    network_connection = ParentalKey(
        AgentConnection, on_delete=models.CASCADE, related_name="variable_mappings"
    )
    adl_parameter = models.ForeignKey(
        DataParameter, on_delete=models.CASCADE, verbose_name=_("ADL Parameter")
    )
    file_variable_name = models.CharField(
        max_length=255, verbose_name=_("File Variable Name")
    )
    file_variable_unit = models.ForeignKey(
        Unit, on_delete=models.CASCADE, verbose_name=_("File Variable Unit")
    )

    panels = [
        FieldPanel("adl_parameter"),
        FieldPanel("file_variable_name"),
        FieldPanel("file_variable_unit"),
    ]

    class Meta(Orderable.Meta):
        verbose_name = _("Agent Connection Variable Mapping")
        verbose_name_plural = _("Agent Connection Variable Mappings")

    def __str__(self):
        return f"{self.file_variable_name} -> {self.adl_parameter}"

    @property
    def source_parameter_name(self):
        """The key this variable's value arrives under, once decoded."""
        return self.file_variable_name

    @property
    def source_parameter_unit(self):
        """The unit the file states this variable in."""
        return self.file_variable_unit


class AgentListingStrategy(models.TextChoices):
    """How the agent finds a station's files in its folder (decision #267)."""

    ENUMERATE = "enumerate", _(
        "Enumerate — scan the folder and match files by pattern"
    )
    DIRECT_FETCH = "direct_fetch", _(
        "Direct Fetch — construct expected filenames, never scan"
    )


class AgentStationLink(StationLink):
    """One ADL station, bound to one folder on one machine.

    The field split here is the whole point of the model. What the data
    *means* -- which station it is, which parameters, from when -- is
    admin-only tier and never writable from the machine. Where the files sit
    and how they are named is the app's tier: the person looking at the real
    files configures how they are found, through
    ``PATCH api/agent/v1/station-links/<id>/config`` (decision #260).

    ``APP_EDITABLE_FIELDS`` is that tier, stated once. The sync response
    renders it, the config endpoint accepts exactly it, and the tests read
    both against this one list -- so the two cannot drift apart.
    """

    extra_list_display = ["local_folder_path", "file_pattern", "listing_strategy"]

    DATE_GRANULARITY_CHOICES = [
        ("year", _("Year")),
        ("month", _("Month")),
        ("day", _("Day")),
        ("hour", _("Hour")),
    ]

    MONTH_FORMAT_CHOICES = [
        ("m", _("Month, 2 digits with leading zeros. '01' to '12'")),
        ("n", _("Month without leading zeros. '1' to '12'")),
        ("M", _("Month, textual, 3 letters. 'Jan'")),
        ("b", _("Month, textual, 3 letters, lowercase. 'jan'")),
        ("F", _("Month, textual, full. 'January'")),
        ("f", _("Month, textual, full, lowercase. 'january'")),
    ]

    #: The app-editable tier, in the order it reads on the wire and in the
    #: admin. Adding a field here makes it writable from the machine; that
    #: is the whole decision, so make it deliberately.
    APP_EDITABLE_FIELDS = (
        "local_folder_path",
        "file_pattern",
        "dir_structured_by_date",
        "date_granularity",
        "month_dir_format",
        "listing_strategy",
        "direct_fetch_prefix",
        "direct_fetch_interval_minutes",
        "direct_fetch_datetime_format",
        "direct_fetch_datetime_timezone",
        "direct_fetch_file_extension",
        "stability_window_seconds",
    )

    # ---------- app tier: where the files sit ----------

    local_folder_path = models.CharField(
        max_length=500,
        verbose_name=_("Local Folder Path"),
        help_text=_(
            "Folder on the machine where the vendor software writes this "
            "station's files, e.g. C:\\VendorData\\Station1"
        ),
    )
    file_pattern = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("File Pattern"),
        help_text=_(
            "Glob the filenames must match, e.g. 'Station1_*.dat'. Not used "
            "by Direct Fetch, which builds the names instead."
        ),
    )

    dir_structured_by_date = models.BooleanField(
        default=False,
        verbose_name=_("Folder structured by date?"),
        help_text=_(
            "Check if the files sit under dated sub-folders of the path "
            "above, in the form [YYYY]/[MM]/[DD]/[HH]."
        ),
    )
    date_granularity = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        choices=DATE_GRANULARITY_CHOICES,
        verbose_name=_("Date Granularity"),
        help_text=_("How far down the dated folder tree the files sit."),
    )
    month_dir_format = models.CharField(
        max_length=5,
        blank=True,
        null=True,
        choices=MONTH_FORMAT_CHOICES,
        default="m",
        verbose_name=_("Month folder format"),
    )

    # ---------- app tier: how they are found ----------

    listing_strategy = models.CharField(
        max_length=40,
        choices=AgentListingStrategy.choices,
        default=AgentListingStrategy.ENUMERATE,
        verbose_name=_("File Listing Strategy"),
        help_text=_(
            "How the agent finds this station's files. Enumerate suits "
            "almost every folder. Direct Fetch is for folders so large that "
            "listing them is itself the problem, and needs filenames the "
            "agent can construct."
        ),
    )
    direct_fetch_prefix = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("File Prefix"),
        help_text=_(
            "Everything in the filename before the datetime. For "
            "'STATION_001_20260219122000.txt' that is 'STATION_001_'."
        ),
    )
    direct_fetch_interval_minutes = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name=_("File Interval (minutes)"),
        help_text=_("Minutes between consecutive files, e.g. 10."),
    )
    direct_fetch_datetime_format = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name=_("File Datetime Format"),
        help_text=_(
            "How the datetime is written in the filename, e.g. "
            "yyyyMMddHHmmss for '20260219122000'."
        ),
    )
    direct_fetch_datetime_timezone = TimeZoneField(
        default="UTC",
        verbose_name=_("File Datetime Timezone"),
        help_text=_(
            "Timezone of the datetime in the filename. Check with the "
            "vendor whether it is local time or UTC."
        ),
    )
    direct_fetch_file_extension = models.CharField(
        max_length=10,
        blank=True,
        null=True,
        default=".txt",
        verbose_name=_("File Extension"),
        help_text=_("Extension including the dot, e.g. '.txt'."),
    )

    stability_window_seconds = models.PositiveIntegerField(
        default=60,
        verbose_name=_("Stability Window (seconds)"),
        help_text=_(
            "A file written more recently than this is left alone until the "
            "next cycle, so a file still being written is never shipped "
            "half-finished."
        ),
    )

    # ---------- admin tier ----------

    start_date = models.DateTimeField(
        blank=True,
        null=True,
        validators=[validate_start_date],
        verbose_name=_("Collection Start Date"),
        help_text=_(
            "Collection never starts before this date. On a fresh install it "
            "is the start of the backfill; moving it forward makes the agent "
            "stop offering anything older."
        ),
    )

    panels = StationLink.panels + [
        MultiFieldPanel([
            FieldPanel("local_folder_path"),
            FieldPanel("file_pattern"),
        ], heading=_("Local Folder")),
        MultiFieldPanel([
            FieldPanel("dir_structured_by_date"),
            FieldPanel("date_granularity"),
            FieldPanel("month_dir_format"),
        ], heading=_("Folder Structure")),
        MultiFieldPanel([
            FieldPanel("listing_strategy"),
            FieldPanel("stability_window_seconds"),
        ], heading=_("File Listing Strategy")),
        MultiFieldPanel([
            FieldPanel("direct_fetch_prefix"),
            FieldPanel("direct_fetch_datetime_format"),
            FieldPanel("direct_fetch_interval_minutes"),
            FieldPanel("direct_fetch_datetime_timezone"),
            FieldPanel("direct_fetch_file_extension"),
        ], heading=_("Direct Fetch")),
        MultiFieldPanel([
            FieldPanel("start_date"),
        ], heading=_("Data Collection")),
        InlinePanel(
            "variable_mappings",
            label=_("Variable Mapping"),
            heading=_("Variable Mappings"),
            help_text=_(
                "Only for a station whose files disagree with the mapping "
                "set on the connection. A parameter set here replaces the "
                "connection's; the rest still apply."
            ),
        ),
    ] + StationLink.aggregation_panels

    class Meta:
        verbose_name = _("Agent Station Link")
        verbose_name_plural = _("Agent Station Links")

    def __str__(self):
        return f"{self.network_connection} - {self.station}"

    def clean(self):
        """Check the folder story hangs together.

        Pure, and deliberately so: the ingestion diagnostic re-runs
        ``full_clean()`` over stored rows outside the request cycle, and
        the real filesystem this describes is on another continent.
        """
        super().clean()

        errors = {}

        if self.listing_strategy == AgentListingStrategy.ENUMERATE:
            if not self.file_pattern:
                errors["file_pattern"] = _(
                    "A file pattern is required when the agent scans the folder."
                )

        if self.listing_strategy == AgentListingStrategy.DIRECT_FETCH:
            if not self.direct_fetch_prefix:
                errors["direct_fetch_prefix"] = _(
                    "A file prefix is required for Direct Fetch — it is how the "
                    "agent builds the filename."
                )
            if not self.direct_fetch_interval_minutes:
                errors["direct_fetch_interval_minutes"] = _(
                    "A file interval is required for Direct Fetch."
                )
            if not self.direct_fetch_datetime_format:
                errors["direct_fetch_datetime_format"] = _(
                    "A datetime format is required for Direct Fetch."
                )

        if self.dir_structured_by_date and not self.date_granularity:
            errors["date_granularity"] = _(
                "Say how far down the dated folder tree the files sit."
            )

        if errors:
            raise ValidationError(errors)

    @classmethod
    def for_device(cls, device):
        """Every station link this device is responsible for.

        One machine serves several connections, so "what is mine" is a
        question about the device, and it is answered here rather than in
        whatever view happens to ask -- which is also what makes it hard for
        an endpoint to reach another machine's links by accident.
        """
        return cls.objects.filter(network_connection__in=device.connections.all())

    def get_variable_mappings(self):
        """This station's effective mappings (authoring guide, Pattern C).

        The connection's list is the default; anything the station states
        for the same parameter replaces it, parameter by parameter.
        """
        connection_mappings = self.network_connection.variable_mappings.all() or []
        station_mappings = self.variable_mappings.all() or []

        resolved = {m.adl_parameter_id: m for m in connection_mappings}
        resolved.update({m.adl_parameter_id: m for m in station_mappings})

        return list(resolved.values())

    def get_first_collection_date(self):
        """The floor core puts under this station's ingestion window."""
        return self.start_date

    def get_manifest_watermark(self):
        """The oldest file this station is worth offering ADL.

        A **floor**, never a high-water mark: a file backfilled into the
        folder weeks late must still reach ADL, so this cannot become
        "the newest thing we have seen". Today it is the collection start
        date; when the file ledger lands it will still be a floor, only a
        better-informed one.
        """
        return self.start_date

    def app_config(self):
        """The app-editable tier, as it goes on the wire.

        Values are rendered to JSON-safe primitives here rather than in a
        serializer, so that the wire form of a field lives beside its
        definition.
        """
        config = {}

        for name in self.APP_EDITABLE_FIELDS:
            value = getattr(self, name)
            if value is None or isinstance(value, (bool, int, float, str)):
                config[name] = value
            else:
                # Timezones, chiefly: their string form is their name.
                config[name] = str(value)

        return config

    def apply_app_config(self, data):
        """Write the app-editable tier from ``data``.

        Last write wins, by construction: whatever arrives is applied over
        what is stored, with no version to present and no conflict to
        report (decision #266). What the caller does not name is left
        alone.

        Raises :class:`ReadOnlyConfigFields` if the write reaches outside
        the tier, and ``ValidationError`` if what it writes does not hang
        together. Either way nothing is saved -- a write is all or nothing,
        so a machine is never left half-configured.
        """
        read_only, unknown = [], []
        for name in data:
            if name in self.APP_EDITABLE_FIELDS:
                continue
            if name in {f.name for f in self._meta.get_fields()}:
                read_only.append(name)
            else:
                unknown.append(name)

        if read_only or unknown:
            raise ReadOnlyConfigFields(read_only=read_only, unknown=unknown)

        for name, value in data.items():
            setattr(self, name, value)

        # Only this tier is validated. The admin tier is not being written,
        # and a row an administrator has left half-configured (a station link
        # created before its start date was decided, say) must not make the
        # machine's own settings unwritable.
        self.full_clean(exclude=[
            field.name
            for field in self._meta.get_fields()
            if getattr(field, "concrete", False)
            and field.name not in self.APP_EDITABLE_FIELDS
        ])

        self.save(update_fields=[*data, "modified_at"])


class AgentStationLinkVariableMapping(Orderable):
    """The per-station half of Pattern C -- the one awkward station."""

    station_link = ParentalKey(
        AgentStationLink, on_delete=models.CASCADE, related_name="variable_mappings"
    )
    adl_parameter = models.ForeignKey(
        DataParameter, on_delete=models.CASCADE, verbose_name=_("ADL Parameter")
    )
    file_variable_name = models.CharField(
        max_length=255, verbose_name=_("File Variable Name")
    )
    file_variable_unit = models.ForeignKey(
        Unit, on_delete=models.CASCADE, verbose_name=_("File Variable Unit")
    )

    panels = [
        FieldPanel("adl_parameter"),
        FieldPanel("file_variable_name"),
        FieldPanel("file_variable_unit"),
    ]

    class Meta(Orderable.Meta):
        verbose_name = _("Agent Station Link Variable Mapping")
        verbose_name_plural = _("Agent Station Link Variable Mappings")

    def __str__(self):
        return f"{self.file_variable_name} -> {self.adl_parameter}"

    @property
    def source_parameter_name(self):
        """The key this variable's value arrives under, once decoded."""
        return self.file_variable_name

    @property
    def source_parameter_unit(self):
        """The unit the file states this variable in."""
        return self.file_variable_unit


# ---------------------------------------------------------------------------
# Keeping config_version honest
#
# The agent asks "has anything changed?" by comparing one number, so every
# way a configuration can change has to move it -- an admin editing a folder
# path, an inline mapping saved with its parent, a station link deleted, the
# app writing its own tier. Signals catch all of them, including the ones
# that never go through a method of ours.
#
# Each handler answers one question: whose device is this row's? A row whose
# owner has already gone (a cascade tearing down a connection, say) has
# nobody left to tell, and says so with None.
# ---------------------------------------------------------------------------

def _device_of_connection(instance):
    return instance.device_id


def _device_of_connection_pk(connection_pk):
    return AgentConnection.objects.filter(pk=connection_pk).values_list(
        "device_id", flat=True
    ).first()


def _device_of_connection_relation(instance):
    """For anything that names its connection: a station link, a mapping."""
    return _device_of_connection_pk(instance.network_connection_id)


def _device_of_station_link_mapping(instance):
    connection_pk = AgentStationLink.objects.filter(
        pk=instance.station_link_id
    ).values_list("network_connection_id", flat=True).first()

    if connection_pk is None:
        return None

    return _device_of_connection_pk(connection_pk)


#: Which config models move a device's version, and how each finds its device.
CONFIG_OWNERS = {
    AgentConnection: _device_of_connection,
    AgentStationLink: _device_of_connection_relation,
    AgentConnectionVariableMapping: _device_of_connection_relation,
    AgentStationLinkVariableMapping: _device_of_station_link_mapping,
}


def _bump_owning_device(sender, instance, **kwargs):
    device_id = CONFIG_OWNERS[sender](instance)

    if device_id is not None:
        AgentDevice.bump_config_version_for(device_id)


def _bump_device_itself(sender, instance, created, update_fields=None, **kwargs):
    # The device row carries exactly one piece of configuration -- its check
    # interval -- and the credential writes beside it (a rotation, a revoke)
    # change nothing an agent caches. Those name their fields, so they are
    # told apart from an administrator saving the edit form, which names
    # none.
    if created:
        return

    if update_fields is None or "check_interval_minutes" in update_fields:
        instance.bump_config_version()


for _model in CONFIG_OWNERS:
    post_save.connect(
        _bump_owning_device, sender=_model,
        dispatch_uid=f"adl_agent_config_version_save_{_model.__name__}",
    )
    post_delete.connect(
        _bump_owning_device, sender=_model,
        dispatch_uid=f"adl_agent_config_version_delete_{_model.__name__}",
    )

post_save.connect(
    _bump_device_itself, sender=AgentDevice,
    dispatch_uid="adl_agent_config_version_device",
)
