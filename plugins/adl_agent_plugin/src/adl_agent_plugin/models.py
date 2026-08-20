from django.db import models, transaction
from django.utils import timezone as dj_timezone
from django.utils.translation import gettext_lazy as _
from wagtail.admin.panels import FieldPanel, MultiFieldPanel

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

    def touch_last_seen(self):
        """Record that this device just called in.

        A single targeted UPDATE, not a model save: this runs on every
        authenticated request and must not race with whatever else is
        writing the row.
        """
        now = dj_timezone.now()
        AgentDevice.objects.filter(pk=self.pk).update(last_seen_at=now)
        self.last_seen_at = now
