"""A vendor's quiet-after window, and the index the station list reads.

The index is what keeps ``last_received_for_connections`` one grouped scan
per device rather than a walk of the ledger: it is asked on every sync, which
is every cycle, for every machine in the fleet.
"""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('adl_agent_plugin', '0009_alter_agentdevice_pinned_version'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentconnection',
            name='stale_after_minutes',
            field=models.PositiveIntegerField(blank=True, help_text="How long one of this vendor's stations may send nothing before the agent's station list marks it quiet. Raise it for a vendor that writes one file a day; leave it empty to use this instance's default.", null=True, validators=[django.core.validators.MinValueValidator(1)], verbose_name='Quiet After (minutes)'),
        ),
        migrations.AddIndex(
            model_name='agentstationdatafile',
            index=models.Index(fields=['station_link', 'received_at'], name='idx_agentfile_link_received'),
        ),
    ]
