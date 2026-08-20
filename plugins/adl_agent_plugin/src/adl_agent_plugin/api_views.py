import tempfile

from django.core.exceptions import ValidationError
from django.core.files import File
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import uploads
from .authentication import AgentDeviceAuthentication, IsAgentDevice
from .credentials import PairingError
from .entries import parse_entry
from .errors import AgentRequestRejected
from .manifest import diff_against_ledger, read_manifest
from .models import (
    AgentDevice,
    AgentStationDataFile,
    AgentStationLink,
    ReadOnlyConfigFields,
)
from .serialization import (
    config_write_payload,
    device_summary,
    manifest_payload,
    sync_payload,
    upload_payload,
)
from .tasks import nudge
from .throttling import AgentPairThrottle


def error(code, detail, status_code=status.HTTP_400_BAD_REQUEST, **extra):
    """The one error envelope this API answers with (decision #266).

    ``code`` is the stable string an agent switches on; ``detail`` is the
    sentence a technician reads.
    """
    return Response({"code": code, "detail": detail, **extra}, status=status_code)


def rejection(exc):
    """The same envelope, for a refusal raised from deeper in the plugin."""
    return error(exc.code, exc.detail, exc.status_code, **exc.extra)


class AgentAPIView(APIView):
    """Base for every endpoint a paired agent calls.

    Authentication is listed per view rather than project-wide on purpose --
    a device token is a credential here and nowhere else (decision #259).
    Stating it once, here, is what keeps a later endpoint from being added
    without it.
    """

    authentication_classes = [AgentDeviceAuthentication]
    permission_classes = [IsAgentDevice]

    @property
    def device(self):
        return self.request.user

    def find_station_link(self, station_link_id):
        """This device's station link with that id, or ``None``.

        Scoped through the device's own connections, so a lookup cannot
        return another machine's link however its id was come by.
        """
        return (
            AgentStationLink.for_device(self.device)
            .filter(pk=station_link_id)
            .first()
        )

    def no_such_station_link(self):
        # Deliberately the same answer as a link that does not exist: a device
        # has no business learning the ids of other machines' work.
        return error(
            "not_found",
            _("No station link with that id is configured for this device."),
            status.HTTP_404_NOT_FOUND,
        )


class AgentPairView(APIView):
    """``POST api/agent/v1/pair`` -- trade a pairing code for a device token.

    The only endpoint in this plugin that answers without a credential,
    because it is where credentials come from. Everything that makes that
    safe lives elsewhere and is asserted by the tests: the code is
    single-use and expires in 72 hours (the model), and the endpoint is
    rate-limited per client (the throttle below).
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AgentPairThrottle]

    def post(self, request):
        payload = request.data if isinstance(request.data, dict) else {}

        try:
            device, token = AgentDevice.redeem_pairing_code(
                payload.get("pairing_code")
            )
        except PairingError as exc:
            # "Not recognised" and "expired" are told apart on purpose. The
            # technician standing at the machine needs to know whether to
            # re-type the code or ask for a new one, and with the code space
            # and the rate limit above, confirming that some code once
            # existed buys an attacker nothing.
            return error(exc.code, str(exc))

        return Response(
            {
                "token": token,
                "device": device_summary(device),
            },
            status=status.HTTP_200_OK,
        )


class AgentDeviceMeView(AgentAPIView):
    """``GET api/agent/v1/me`` -- what ADL believes about the caller.

    What a freshly paired agent calls to confirm its token works, and what an
    operator curls to watch a revocation take effect.
    """

    def get(self, request):
        return Response(device_summary(self.device), status=status.HTTP_200_OK)


class AgentSyncView(AgentAPIView):
    """``GET api/agent/v1/sync`` -- the device's whole world, in one call.

    Called at the top of every cycle. One round trip has to leave the agent
    knowing which folders to scan, how to scan them, how far back to look,
    and whether any of that changed since last time -- these machines sit on
    links where a second round trip is not free (decision #266).
    """

    def get(self, request):
        return Response(sync_payload(self.device), status=status.HTTP_200_OK)


class AgentStationLinkConfigView(AgentAPIView):
    """``PATCH api/agent/v1/station-links/<id>/config`` -- the app's tier.

    The person looking at the real files says where they are and how they
    are named; what the data means stays with HQ. Writes are last-write-wins
    and never answer 409: the response carries the configuration that now
    stands and the version it stands at, and an agent whose cached version
    has moved simply re-reads (decision #266).
    """

    def patch(self, request, pk):
        station_link = self.find_station_link(pk)

        if station_link is None:
            return self.no_such_station_link()

        if not isinstance(request.data, dict):
            return error(
                "invalid_body",
                _("Send an object of station link settings to change."),
            )

        try:
            station_link.apply_app_config(request.data)
        except ReadOnlyConfigFields as exc:
            return error("read_only_fields", str(exc), fields=exc.fields)
        except ValidationError as exc:
            return error(
                "invalid_config",
                _("The settings sent do not describe a folder the agent can "
                  "read."),
                errors=exc.message_dict,
            )

        return Response(
            config_write_payload(station_link, self.device),
            status=status.HTTP_200_OK,
        )


class AgentManifestView(AgentAPIView):
    """``POST api/agent/v1/manifest`` -- propose files, be told which to send.

    One call per cycle for the whole machine, however many stations it
    serves: these links are slow and a round trip per station would dominate
    the cycle (decision #266).

    Nothing here writes. The ledger only ever learns about a file from the
    upload endpoint, so a cycle that dies between the two leaves ADL
    believing exactly what it believed before -- and the next manifest offers
    the same files again.
    """

    parser_classes = [JSONParser]

    def post(self, request):
        try:
            entries = read_manifest(request.data)
        except AgentRequestRejected as exc:
            return rejection(exc)

        diff = diff_against_ledger(self.device, entries)

        return Response(
            manifest_payload(self.device, diff), status=status.HTTP_200_OK,
        )


class AgentFileUploadView(AgentAPIView):
    """``POST api/agent/v1/files`` -- one file, with the entry that promised it.

    One file per request, so that a failure costs one file and the next
    manifest heals it (decision #266). The bytes are checked against the
    entry before anything is stored: ADL would rather hold nothing than hold
    a file its ledger describes wrongly, because everything downstream --
    dedup, re-processing, "what have we got for this station" -- trusts the
    ledger to be true.
    """

    parser_classes = [MultiPartParser]

    def post(self, request):
        # A multipart body is a QueryDict of repeated keys; the entry is
        # single-valued, so flatten it to the plain mapping ``parse_entry``
        # reads and let that refuse anything else.
        payload = request.data
        if hasattr(payload, "dict"):
            payload = payload.dict()

        try:
            entry = parse_entry(payload)
        except AgentRequestRejected as exc:
            return rejection(exc)

        station_link = self.find_station_link(entry.station_link_id)

        if station_link is None:
            return self.no_such_station_link()

        if not station_link.enabled:
            # The manifest never asked for this, so an agent reaching here is
            # working from a configuration ADL has since changed. Refused
            # rather than quietly kept: a switched-off station should not
            # accumulate files nobody will process.
            return error(
                "station_link_disabled",
                _("This station is switched off in ADL and is not taking "
                  "files."),
                status.HTTP_409_CONFLICT,
            )

        # A real file on disk, not fifty megabytes of request in memory, and
        # gone the moment storage has taken its own copy.
        with tempfile.NamedTemporaryFile(suffix=f"-{entry.name}") as staged:
            try:
                uploads.receive(
                    request.FILES.get("file"),
                    request.data.get("encoding"),
                    entry,
                    staged,
                )
            except AgentRequestRejected as exc:
                return rejection(exc)

            data_file = AgentStationDataFile.record_upload(
                station_link, entry, File(staged),
            )

        # The machine has just told ADL there is work; waiting for the next
        # scheduled pass to notice would throw away the latency that push
        # delivery exists for. Coalesced per connection, so a cycle uploading
        # forty files still asks once (see :mod:`adl_agent_plugin.tasks`).
        nudge(station_link.network_connection)

        return Response(
            upload_payload(data_file, self.device),
            status=status.HTTP_201_CREATED,
        )
