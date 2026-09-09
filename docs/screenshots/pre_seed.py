"""
The one row docs/screenshots/fixture.json cannot create for itself.

AgentConnection.device is a non-null foreign key, and the fixture format
names foreign keys rather than creating their targets — so the device has to
exist before the fixture is applied. The capture harness runs this file
inside the web container immediately before `seed_docs_demo`.

The device is left in its just-created, awaiting-pairing state here.
docs/screenshots/seed.py pairs it afterwards, through the model's own
redeem_pairing_code(), so the demo shows a device that genuinely holds a
token rather than one with the flags set by hand.
"""

from adl_agent_plugin.models import AgentDevice

DEVICE_NAME = "Nairobi Office PC"

device, created = AgentDevice.objects.get_or_create(
    name=DEVICE_NAME,
    defaults={
        "description": "Windows machine in the forecasting office that the "
                       "vendor software writes station files to.",
        "check_interval_minutes": 5,
    },
)

print(f"[docs-pre-seed] device {device.pk} '{device.name}' "
      f"({'created' if created else 'existing'}), status {device.status}")
