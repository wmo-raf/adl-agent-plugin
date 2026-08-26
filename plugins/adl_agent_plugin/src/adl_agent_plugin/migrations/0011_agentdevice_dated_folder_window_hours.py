"""How far back a cycle walks a station's dated sub-folders.

Per device rather than per station: what a machine can afford to enumerate
every cycle is a question about its disks and its share. The default of two
days is what the agent assumed before this field existed, so an instance
that migrates and changes nothing keeps the behaviour it had.
"""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('adl_agent_plugin', '0010_agentconnection_stale_after_minutes_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='agentdevice',
            name='dated_folder_window_hours',
            field=models.PositiveIntegerField(default=48, help_text='For stations whose files sit under dated sub-folders: how far back each cycle walks the tree. Two days suits a vendor that files by day or hour. Anything older is picked up by the daily reconciliation.', validators=[django.core.validators.MinValueValidator(0)], verbose_name='Dated folder window (hours)'),
        ),
    ]
