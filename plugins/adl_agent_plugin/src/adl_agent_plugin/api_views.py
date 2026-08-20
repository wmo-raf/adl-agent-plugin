from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import AgentDeviceAuthentication, IsAgentDevice
from .credentials import PairingError
from .models import AgentDevice, AgentStationLink, ReadOnlyConfigFields
from .serialization import config_write_payload, device_summary, sync_payload
from .throttling import AgentPairThrottle


def error(code, detail, status_code=status.HTTP_400_BAD_REQUEST, **extra):
    """The one error envelope this API answers with (decision #266).

    ``code`` is the stable string an agent switches on; ``detail`` is the
    sentence a technician reads.
    """
    return Response({"code": code, "detail": detail, **extra}, status=status_code)


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
            # Deliberately the same answer as a link that does not exist: a
            # device has no business learning the ids of other machines' work.
            return error(
                "not_found",
                _("No station link with that id is configured for this device."),
                status.HTTP_404_NOT_FOUND,
            )

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

    def find_station_link(self, pk):
        """This device's station link with that id, or ``None``.

        Scoped through the device's own connections, so the lookup cannot
        return another machine's link however the id was come by.
        """
        return AgentStationLink.for_device(self.device).filter(pk=pk).first()
