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
                "status_label": device.status_label,
                "status_tone": device.status_tone,
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


class AgentDeviceLivenessPanel(Panel):
    """What the machine last said about itself, on its own page.

    The fleet listing answers "which machines are in trouble"; this answers
    the next question, which is "what is wrong with this one". So it shows
    the reading behind the state -- when the last heartbeat and the last
    completed cycle were, what the machine is running, how skewed its clock
    is, what its disks have left -- and the per-station scan counts, which
    are the difference between "this country is quiet" and "this country is
    fine except for Garissa".

    Read-only by construction: nothing here is ADL's to set. It is what a
    machine reported, or the absence of a machine having reported.
    """

    class BoundPanel(Panel.BoundPanel):
        template_name = "adl_agent_plugin/panels/device_liveness.html"

        def is_shown(self):
            return self.instance is not None and self.instance.pk is not None

        def get_context_data(self, parent_context=None):
            context = super().get_context_data(parent_context)
            from .heartbeat import read_details

            device = self.instance

            context.update({
                "device": device,
                "liveness": device.liveness,
                **read_details(device.heartbeat_details),
                # Newest first and bounded: the whole history is a listing's
                # job, and an edit form is not a listing.
                "transitions": device.state_transitions.all()[:10],
            })

            return context
