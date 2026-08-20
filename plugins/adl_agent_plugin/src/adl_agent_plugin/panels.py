from django.urls import reverse
from wagtail.admin.panels import Panel


class AgentDeviceIdentityPanel(Panel):
    """The device's credential state, shown inside its edit form.

    None of what this panel shows is editable -- a pairing code and a token
    are minted by ADL, never typed by an administrator -- so it renders a
    read-out plus links to the two actions that change it. The pairing code
    itself is shown here in full: it exists to be relayed to whoever is
    standing at the machine, and an administrator who cannot re-read it ends
    up rotating the device just to see a code again.
    """

    class BoundPanel(Panel.BoundPanel):
        template_name = "adl_agent_plugin/panels/device_identity.html"

        def is_shown(self):
            # A device that has not been saved yet has no credentials to
            # report, and the actions below need a pk to address.
            return self.instance is not None and self.instance.pk is not None

        def get_context_data(self, parent_context=None):
            context = super().get_context_data(parent_context)
            device = self.instance

            context.update({
                "device": device,
                "status": device.status,
                "status_label": device.status_label,
                "pairing_code": (
                    device.pairing_code if device.pairing_code_is_valid else None
                ),
                "pairing_code_expires_at": device.pairing_code_expires_at,
                "issue_code_url": reverse(
                    "agent_device_issue_pairing_code", args=[device.pk]
                ),
                "revoke_url": reverse("agent_device_revoke", args=[device.pk]),
            })

            return context
