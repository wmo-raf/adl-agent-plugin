from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from wagtail.admin import messages
from wagtail.permission_policies import ModelPermissionPolicy

from .models import AgentDevice

#: Both actions mint or destroy a credential, so both are gated on the
#: permission to change a device -- read access to the listing is not enough.
DEVICE_ADMIN_PERMISSIONS = ["change"]


def _get_device_for_admin(request, pk):
    device = get_object_or_404(AgentDevice, pk=pk)

    policy = ModelPermissionPolicy(AgentDevice)
    if not policy.user_has_any_permission(request.user, DEVICE_ADMIN_PERMISSIONS):
        raise PermissionDenied

    return device


def _device_edit_url(device):
    return reverse("agent_devices:edit", args=[device.pk])


def _render_confirm(request, device, context):
    base = {
        "breadcrumbs_items": [
            {"url": reverse("wagtailadmin_home"), "label": _("Home")},
            {"url": reverse("agent_devices:index"), "label": _("Agent Devices")},
            {"url": _device_edit_url(device), "label": device.name},
            {"url": None, "label": context["action_label"]},
        ],
        "header_icon": "desktop",
        "device": device,
        "cancel_url": _device_edit_url(device),
    }
    base.update(context)

    return render(request, "adl_agent_plugin/device_action_confirm.html", base)


@require_http_methods(["GET", "POST"])
def issue_pairing_code(request, pk):
    """Mint a pairing code for a device -- enrollment, and also rotation.

    Confirmed rather than one-click because on an already-paired machine
    this is the first half of a token rotation, and the person clicking
    should know they have started one.
    """
    device = _get_device_for_admin(request, pk)

    if request.method == "POST":
        code = device.issue_pairing_code()
        messages.success(
            request,
            _("New pairing code for %(device)s: %(code)s") % {
                "device": device.name, "code": code,
            },
        )
        return redirect(_device_edit_url(device))

    if device.is_paired:
        confirm_message = _(
            "This device is paired and working. Issuing a new pairing code "
            "starts a token rotation."
        )
        consequences = [
            _("Its current token keeps working until the new code is used, "
              "so data keeps flowing in the meantime."),
            _("The moment the new code is used, the current token stops "
              "working -- any other machine still holding it is cut off."),
            _("Any pairing code issued earlier and not yet used is replaced."),
        ]
    else:
        confirm_message = _(
            "Issue a pairing code for this device. It is valid for 72 hours "
            "and can be used once."
        )
        consequences = [
            _("Any pairing code issued earlier and not yet used is replaced."),
        ]

    return _render_confirm(request, device, {
        "header_title": _("Issue pairing code — %s") % device.name,
        "action_label": _("Issue pairing code"),
        "action_url": request.path,
        "confirm_message": confirm_message,
        "consequences": consequences,
        "is_destructive": False,
    })


@require_http_methods(["GET", "POST"])
def revoke_device(request, pk):
    """Cut a machine off from ADL."""
    device = _get_device_for_admin(request, pk)

    if request.method == "POST":
        device.revoke()
        messages.success(
            request,
            _("%(device)s has been revoked.") % {"device": device.name},
        )
        return redirect(_device_edit_url(device))

    return _render_confirm(request, device, {
        "header_title": _("Revoke device — %s") % device.name,
        "action_label": _("Revoke device"),
        "action_url": request.path,
        "confirm_message": _(
            "Revoke this device. It stops being able to reach ADL "
            "immediately."
        ),
        "consequences": [
            _("Its token is destroyed; every call it makes answers 401 and "
              "the agent tells the technician to re-pair."),
            _("Any unused pairing code is destroyed too, so the machine "
              "cannot re-enroll itself."),
            _("Letting it back in means issuing a new pairing code."),
        ],
        "is_destructive": True,
    })
