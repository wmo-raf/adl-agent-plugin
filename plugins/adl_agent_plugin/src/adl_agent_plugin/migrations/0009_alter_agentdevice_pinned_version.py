"""Catch the migration up with ``pinned_version``'s help text.

Pre-existing drift, not this branch's change: the field's wording moved when
the update feed landed and no migration was written for it. Kept apart from
the change beside it so it can be read -- and reverted -- on its own.
"""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('adl_agent_plugin', '0008_agentrelease_agentreleaseartifact'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agentdevice',
            name='pinned_version',
            field=models.CharField(blank=True, default='', help_text='Hold this machine on one agent version instead of letting it follow the update feed. Leave empty to keep it current.', max_length=100, validators=[django.core.validators.RegexValidator('^\\d+\\.\\d+\\.\\d+$', message='An agent version is three numbers separated by dots, such as 1.2.0.')], verbose_name='Pinned version'),
        ),
    ]
