from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, permissions
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from .models import AgentDevice

AUTH_KEYWORD = "Bearer"


class AgentDeviceAuthentication(BaseAuthentication):
    """Authenticate an ``Authorization: Bearer <device token>`` header.

    This class is the authorization boundary decision #259 asks for, and it
    is a boundary by omission: it is never added to the project's
    ``DEFAULT_AUTHENTICATION_CLASSES``, only listed explicitly on this
    plugin's own views. A device token presented to the core API or the
    Wagtail admin is therefore not a weak credential there -- it is not a
    credential there at all.

    On success ``request.user`` and ``request.auth`` are both the
    :class:`~adl_agent_plugin.models.AgentDevice`.
    """

    keyword = AUTH_KEYWORD

    def authenticate(self, request):
        header = get_authorization_header(request).split()

        if not header or header[0].lower() != self.keyword.lower().encode():
            # Not our scheme. Returning None lets any other authenticator on
            # the view try; with none configured the request stays anonymous
            # and the permission class turns it into a 401.
            return None

        if len(header) == 1:
            raise exceptions.AuthenticationFailed(
                _("Invalid token header. No credentials provided.")
            )
        if len(header) > 2:
            raise exceptions.AuthenticationFailed(
                _("Invalid token header. Token string may not contain spaces.")
            )

        try:
            token = header[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed(
                _("Invalid token header. Token string contains invalid characters.")
            )

        device = AgentDevice.authenticate_token(token)

        if device is None:
            # Deliberately one message for "never existed" and "revoked".
            # The agent app treats any 401 the same way -- stop uploading and
            # tell the technician to re-pair -- so the distinction would only
            # ever tell a caller which tokens once existed.
            raise exceptions.AuthenticationFailed(
                _("Invalid or revoked device token.")
            )

        device.touch_last_seen()

        return device, device

    def authenticate_header(self, request):
        # Returning a challenge is what makes an unauthenticated call answer
        # 401 rather than 403, which is the status decision #259 promises the
        # agent app it can key "re-pair needed" on.
        return self.keyword


class IsAgentDevice(permissions.BasePermission):
    """Allow only a live agent device.

    Named explicitly rather than reusing ``IsAuthenticated`` so that adding
    a second authentication class to an agent view can never silently let a
    logged-in admin session through an endpoint meant for machines.
    """

    message = _("A valid agent device token is required.")

    def has_permission(self, request, view):
        device = getattr(request, "user", None)
        return isinstance(device, AgentDevice) and not device.is_revoked
