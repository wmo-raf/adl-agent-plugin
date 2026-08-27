"""When ADL last took delivery of a file from a machine.

The second of the two facts the liveness ladder reads about work, and the
one a completed scan cycle cannot supply: a machine pushing a backlog is
working hard and will not finish a cycle for hours, so reading cycles alone
called it stuck (wmo-raf/adl#303).

Denormalised from ``AgentStationDataFile.received_at``, which remains the
record. It is on the device because ``adl_agent_plugin.health`` may not
query -- it runs on rendering paths and inside a source check that is
guaranteed to perform no I/O.

Null on every existing row, and correctly so: nothing has been stamped yet.
A device that is genuinely working stamps it on its next upload, and one
that is not keeps the verdict it already had.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('adl_agent_plugin', '0011_agentdevice_dated_folder_window_hours'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentdevice',
            name='last_file_received_at',
            field=models.DateTimeField(blank=True, editable=False, null=True, verbose_name='Last file received at'),
        ),
    ]
