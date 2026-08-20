from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import AgentDeviceAuthentication, IsAgentDevice
from .credentials import PairingError
from .models import AgentDevice
from .throttling import AgentPairThrottle


def device_summary(device):
    """What an agent is told about itself. Never includes a credential."""
    return {
        "id": device.pk,
        "name": device.name,
        "paired_at": device.paired_at,
        "last_seen_at": device.last_seen_at,
    }


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
            return Response(
                {"code": exc.code, "detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "token": token,
                "device": device_summary(device),
            },
            status=status.HTTP_200_OK,
        )


class AgentDeviceMeView(APIView):
    """``GET api/agent/v1/me`` -- what ADL believes about the caller.

    The first authenticated endpoint, and for now the only one: it is what
    a freshly paired agent calls to confirm its token works, and what an
    operator curls to watch a revocation take effect. The real work --
    sync, manifest, files -- lands beside it in later slices.
    """

    authentication_classes = [AgentDeviceAuthentication]
    permission_classes = [IsAgentDevice]

    def get(self, request):
        return Response(device_summary(request.user), status=status.HTTP_200_OK)
