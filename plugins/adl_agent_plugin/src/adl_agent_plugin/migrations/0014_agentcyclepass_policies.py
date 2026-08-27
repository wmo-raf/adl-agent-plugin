"""Put compression and retention on the cycle-pass hypertable.

A row per station per unit pass is a lot of rows -- some twenty-nine thousand
a day for a country running two hundred station links on a ten-minute
interval, around a gigabyte a year raw. They are also exactly the repetitive
small-integer columns TimescaleDB's columnar compression eats, which takes
that to well under a tenth of it.

The numbers are settings and not constants (see
:mod:`adl_agent_plugin.cycles`), so what this migration applies is whatever
the instance is configured with on the day it runs. A change afterwards is
picked up by the nightly task that re-applies them, which is the same call
made here -- there is one implementation of "put the policies on", and this
is a caller of it rather than a copy.
"""

from django.db import migrations


def apply_policies(apps, schema_editor):
    # Imported inside the function, not at module scope: this reaches the real
    # model to read its table name, and a migration module is imported while
    # the app registry is still being built.
    from adl_agent_plugin.cycles import apply_policies as put_them_on

    put_them_on()


def drop_policies(apps, schema_editor):
    """Take them off again, so the migration is reversible.

    Reversing this does not decompress anything -- unmigrating past the table
    itself drops it, and there is nothing here worth an expensive rewrite on
    the way to that.
    """
    from adl_agent_plugin.models import AgentCyclePass

    table = AgentCyclePass._meta.db_table

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"SELECT remove_compression_policy('{table}', if_exists => true)"
        )
        cursor.execute(
            f"SELECT remove_retention_policy('{table}', if_exists => true)"
        )


class Migration(migrations.Migration):

    dependencies = [
        ('adl_agent_plugin', '0013_agentdevice_log_level_agentcyclepass'),
    ]

    operations = [
        migrations.RunPython(apply_policies, drop_policies),
    ]
