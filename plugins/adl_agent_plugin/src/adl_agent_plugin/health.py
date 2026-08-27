"""
What a machine's heartbeats say about it.

Every other plugin answers "is the source alive?" by dialing the source. The
agent cannot be asked anything -- there is no inbound path to a country
server, which is the whole reason this plugin exists -- so liveness is
inverted with everything else: the machine says so itself, every few minutes,
and ADL's job is to notice when it stops (decision #264).

Three distinct faults hide behind "no data arrived", and telling them apart
is what HQ has never been able to do with reverse tunnels:

- **offline** -- the machine, its link, or the service is down. Nothing is
  arriving and nothing will until someone goes and looks.
- **cycle stuck** -- the machine is up and heartbeating, and nothing it does
  is reaching ADL: no scan cycle finished and no file arrived. The service
  lives; its work does not.
- **clock skewed** -- everything is running, and the file-picking window runs
  on the device's own file times, so a wrong clock quietly loses data. Never
  an outage; always worth saying.

Nothing here touches the database or the network. Every function takes a
device (or a connection) already in hand and reads fields already on it, so
this module is safe to call from a rendering path, from the health checklist,
and from a test that never saved a row. That is also why ``models`` imports
*this* and never the other way round.

It is also why the device carries ``last_file_received_at`` at all. The
record of what ADL holds lives in ``AgentStationDataFile``, and asking it
would be one query -- which this module may not make. So the upload path
stamps the device as it stores each file, and the fact arrives here on the
row like every other.
"""

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone as dj_timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

#: How often a paired machine is expected to heartbeat. Its own fixed
#: cadence, deliberately not the scan interval: a loop isolated from the scan
#: cycle is what separates "machine dead" from "machine up but cycle wedged"
#: (decision #264). Handed to the agent in every sync response, so changing
#: it here changes the fleet without anything being reinstalled.
DEFAULT_HEARTBEAT_INTERVAL_MINUTES = 5

#: Missed heartbeats before a machine is called degraded, and before it is
#: called offline. Two and three rather than one and two because a single
#: missed beat is a dropped packet on a link that drops packets for a living.
DEFAULT_DEGRADED_AFTER_MISSED = 2
DEFAULT_OFFLINE_AFTER_MISSED = 3

#: A machine heartbeating but not finishing scan cycles is stuck after this
#: many of its own check intervals. Two, so that one slow cycle -- a folder
#: with a year of backlog in it -- is not an alarm.
DEFAULT_CYCLE_STUCK_MULTIPLIER = 2

#: Clock difference worth telling an operator about. Five minutes is well
#: past anything NTP or a lazy RTC explains, and well short of the hour a
#: wrong timezone would produce.
DEFAULT_CLOCK_SKEW_ADVISORY_SECONDS = 5 * 60

#: How long a station may go without ADL receiving a file from it before the
#: agent's own window calls it quiet. Six hours rather than a multiple of the
#: check interval, because the check interval is how often the machine
#: *scans* and says nothing about how often the vendor *writes*: a station
#: whose logger produces one file a day would be permanently quiet against a
#: ten-minute scan cadence.
#:
#: Six is the number that catches a logger which stopped at breakfast on the
#: same morning. It is the wrong number for a vendor that legitimately writes
#: once a day, which is why it is a floor a connection may raise -- see
#: ``AgentConnection.stale_after_minutes``. A deployment whose slowest vendor
#: is slower than every fast one is better served raising it there than here.
DEFAULT_STATION_STALE_AFTER_MINUTES = 6 * 60

#: How often a station stops trusting the cheap scan path and offers its whole
#: folder back to its collection start date (wmo-raf/adl#280). Daily, which is
#: what every agent in the field assumed while nothing served this number.
#:
#: Deployment-wide rather than per machine because what a sweep spends is
#: manifest traffic on the link between a country and ADL, not anything on the
#: machine's own disks -- so a deployment on a satellite link wants its whole
#: fleet slower, not one server of it.
DEFAULT_RECONCILIATION_INTERVAL_HOURS = 24

#: What the wire carries for a deployment that has switched sweeps off. The
#: agent reads zero and anything below it alike, so the number handed out is
#: normalised to the one the contract names.
NO_RECONCILIATION = 0

#: How often the standing layer-5 verdict is refreshed when nothing has
#: changed. Core stops trusting a probe result after fifteen minutes, so a
#: third of that leaves two chances to miss a sweep before the connection's
#: source layer falls back to "not recently checked".
SOURCE_EVIDENCE_REFRESH_SECONDS = 5 * 60

#: Every threshold above is settable per deployment, from Django settings for
#: tests and from the environment for operators -- the same names either way,
#: exactly as the pair throttle does it.
HEARTBEAT_INTERVAL_SETTING = "ADL_AGENT_HEARTBEAT_INTERVAL_MINUTES"
DEGRADED_AFTER_MISSED_SETTING = "ADL_AGENT_DEGRADED_AFTER_MISSED"
OFFLINE_AFTER_MISSED_SETTING = "ADL_AGENT_OFFLINE_AFTER_MISSED"
CYCLE_STUCK_MULTIPLIER_SETTING = "ADL_AGENT_CYCLE_STUCK_MULTIPLIER"
CLOCK_SKEW_ADVISORY_SETTING = "ADL_AGENT_CLOCK_SKEW_ADVISORY_SECONDS"
STATION_STALE_AFTER_SETTING = "ADL_AGENT_STATION_STALE_AFTER_MINUTES"
RECONCILIATION_INTERVAL_SETTING = "ADL_AGENT_RECONCILIATION_INTERVAL_HOURS"


class LivenessState:
    """What ADL currently believes about one machine.

    A closed vocabulary, and a single answer per device: these are the states
    a transition is recorded between, so two of them being true at once would
    make "when did it change" unanswerable. The ladder that picks one is in
    :func:`liveness_of` -- silence outranks stuckness, because a machine that
    is not talking cannot be observed to be cycling.

    Clock skew is deliberately *not* here. It is a finding about a machine
    that is otherwise fine, and folding it in would cost the state its
    meaning: an operator reading "skewed" would not know whether data is
    arriving.
    """

    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    CYCLE_STUCK = "cycle_stuck"
    #: Never paired, or revoked: there is no machine to be alive or dead.
    UNKNOWN = "unknown"

    LABELS = {
        ONLINE: _lazy("Online"),
        DEGRADED: _lazy("Degraded"),
        OFFLINE: _lazy("Offline"),
        CYCLE_STUCK: _lazy("Cycle stuck"),
        UNKNOWN: _lazy("Unknown"),
    }

    #: How each state reads in Wagtail's help-block vocabulary, kept beside
    #: the states so no template re-derives the ladder from raw strings.
    TONES = {
        ONLINE: "help-info",
        DEGRADED: "help-warning",
        OFFLINE: "help-critical",
        CYCLE_STUCK: "help-critical",
        UNKNOWN: "help-warning",
    }

    #: Built from LABELS rather than re-listed, so a state can never be
    #: storable without a label or labelled without being storable.
    CHOICES = list(LABELS.items())

    #: Everything that is not a machine doing its job. Read by the source
    #: check, so that adding a state cannot leave it silently reported OK.
    FAULTS = frozenset({DEGRADED, OFFLINE, CYCLE_STUCK, UNKNOWN})


def _threshold(name, default, minimum=1):
    """One configured number, or the default if it is missing or nonsense.

    A deployment that mistypes a threshold gets ADL's default and keeps its
    fleet monitoring, rather than an instance that will not start.

    ``minimum=None`` for a threshold whose vocabulary runs below one, where
    only an unreadable value is nonsense and every integer means something.
    """
    raw = getattr(settings, name, None)

    if raw is None:
        raw = os.environ.get(name)

    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default

    if minimum is None:
        return value

    return value if value >= minimum else default


def heartbeat_interval_minutes():
    return _threshold(HEARTBEAT_INTERVAL_SETTING,
                      DEFAULT_HEARTBEAT_INTERVAL_MINUTES)


def degraded_after_missed():
    return _threshold(DEGRADED_AFTER_MISSED_SETTING,
                      DEFAULT_DEGRADED_AFTER_MISSED)


def offline_after_missed():
    return _threshold(OFFLINE_AFTER_MISSED_SETTING,
                      DEFAULT_OFFLINE_AFTER_MISSED)


def cycle_stuck_multiplier():
    return _threshold(CYCLE_STUCK_MULTIPLIER_SETTING,
                      DEFAULT_CYCLE_STUCK_MULTIPLIER)


def clock_skew_advisory_seconds():
    return _threshold(CLOCK_SKEW_ADVISORY_SETTING,
                      DEFAULT_CLOCK_SKEW_ADVISORY_SECONDS)


def station_stale_after_minutes():
    """The deployment-wide floor a connection's own window may raise."""
    return _threshold(STATION_STALE_AFTER_SETTING,
                      DEFAULT_STATION_STALE_AFTER_MINUTES)


def reconciliation_interval_hours():
    """How often an agent offers a station's whole folder, or 0 for never.

    The one threshold with no floor: zero and below are not mistyped numbers
    here but a deployment saying the sweep costs more than it is worth, and
    falling back to daily would be ADL overruling that silently on a link
    that cannot afford it. Anything unreadable is still nonsense and still
    takes the default.

    Below zero is normalised up to zero on the way out. The agent treats the
    two alike, and a fleet reading its own configuration back should see the
    number the contract names rather than whatever an operator typed.
    """
    hours = _threshold(RECONCILIATION_INTERVAL_SETTING,
                       DEFAULT_RECONCILIATION_INTERVAL_HOURS, minimum=None)

    return max(hours, NO_RECONCILIATION)


@dataclass(frozen=True)
class Liveness:
    """One machine's state, and the sentence that explains it.

    The ages and counts behind the verdict are not fields here: they exist
    only to be spoken, and they are spoken in ``message`` -- written once, in
    this module, rather than three times at the device page, the health
    checklist and the probe result. Anything wanting the raw timestamps has
    the device row, which is where they live.
    """

    state: str
    #: Device clock minus ADL's, signed: positive means the machine is ahead.
    clock_skew_seconds: Optional[int]
    #: Whether that skew is large enough to be worth an operator's attention.
    skew_is_advisory: bool
    message: str

    @property
    def label(self):
        return LivenessState.LABELS[self.state]

    @property
    def tone(self):
        return LivenessState.TONES[self.state]

    @property
    def is_fault(self):
        return self.state in LivenessState.FAULTS

    @property
    def skew_note(self):
        """The clock sentence, or ``""`` when there is nothing to say.

        Separate from ``message`` because it is advisory in the health
        module's exact sense: shown wherever the state is shown, and never
        allowed to become the verdict.
        """
        if not self.skew_is_advisory:
            return ""

        seconds = self.clock_skew_seconds
        direction = _("ahead of") if seconds > 0 else _("behind")

        return _("Its clock is %(amount)s %(direction)s ADL's. File windows "
                 "are computed from the machine's own file times, so a "
                 "skewed clock quietly loses observations.") % {
            "amount": humanize_seconds(abs(seconds)),
            "direction": direction,
        }


def humanize_seconds(seconds):
    """A duration as a technician would say it -- coarse on purpose."""
    seconds = int(seconds)

    if seconds < 90:
        return _("%(count)s second(s)") % {"count": seconds}

    minutes = seconds // 60
    if minutes < 90:
        return _("%(count)s minute(s)") % {"count": minutes}

    hours = minutes // 60
    if hours < 48:
        return _("%(count)s hour(s)") % {"count": hours}

    return _("%(count)s day(s)") % {"count": hours // 24}


def _age(now, moment):
    if moment is None:
        return None
    return max(now - moment, timedelta(0))


def liveness_of(device, now=None):
    """What ``device``'s heartbeats say about it right now.

    Pure: it reads the snapshot the heartbeat endpoint left on the row and
    the clock, and touches nothing else. ``now`` is injectable because that is
    what lets a test drive every state by posting one heartbeat and then
    asking what ADL believes some minutes later -- no sleeping, and no
    reaching past the HTTP seam to set a state directly.
    """
    now = now or dj_timezone.now()

    skew = device.clock_skew_seconds
    skew_advisory = (skew is not None
                     and abs(skew) > clock_skew_advisory_seconds())

    def reading(state, message):
        return Liveness(
            state=state, clock_skew_seconds=skew,
            skew_is_advisory=skew_advisory, message=message,
        )

    if not device.is_paired:
        # Not a fault of the machine's -- there may not be a machine. But it
        # is a fault of the connection's: nothing can ever arrive on it.
        return reading(
            LivenessState.UNKNOWN,
            _("%(device)s is not paired, so no machine is sending anything "
              "for this connection.") % {"device": device.name},
        )

    interval = timedelta(minutes=heartbeat_interval_minutes())

    # A machine that has never heartbeated is measured from the moment it
    # was paired: the technician typed the code, so from then on silence is
    # the machine's to explain.
    since = device.last_heartbeat_at or device.paired_at
    heartbeat_age = _age(now, since)

    if heartbeat_age is None:  # pragma: no cover - a paired device has both
        return reading(LivenessState.UNKNOWN,
                       _("%(device)s has never reported.") % {"device": device.name})

    missed = int(heartbeat_age // interval)
    never = device.last_heartbeat_at is None

    # The lower of the two thresholds opens the branch and the higher picks
    # the state, so a deployment that configures them the wrong way round
    # still gets a ladder rather than a machine that is never called anything.
    if missed >= min(degraded_after_missed(), offline_after_missed()):
        silence = _(
            "%(device)s has not heartbeated since it was paired %(age)s ago; "
            "one is expected every %(interval)s minutes."
        ) if never else _(
            "%(device)s has missed %(missed)s heartbeats -- last heard from "
            "%(age)s ago, and one is expected every %(interval)s minutes."
        )
        return reading(
            LivenessState.OFFLINE if missed >= offline_after_missed()
            else LivenessState.DEGRADED,
            silence % {
                "device": device.name, "missed": missed,
                "age": humanize_seconds(heartbeat_age.total_seconds()),
                "interval": heartbeat_interval_minutes(),
            },
        )

    # Heartbeats are arriving. Now, and only now, is "up but not working" an
    # observation ADL can make: a cycle count from a machine that has gone
    # quiet says nothing about what the machine is doing this minute.
    stuck_after = timedelta(
        minutes=device.check_interval_minutes * cycle_stuck_multiplier()
    )
    # A paired device always has a pairing moment, so there is always a
    # point to measure the absence of a cycle from.
    cycle_age = _age(now, device.last_cycle_completed_at or device.paired_at)

    # Two ways of proving the same thing, and neither works alone. An idle
    # machine proves itself by finishing empty cycles every few minutes and
    # sends nothing at all; a machine pushing a backlog sends constantly and
    # will not finish a cycle for hours, because the agent stamps a cycle
    # only when it has been round every station on the box. Reading either
    # one on its own calls half a healthy fleet stuck -- and the half it
    # picks on is the half working hardest (wmo-raf/adl#303).
    arrival_age = _age(now, device.last_file_received_at)
    arriving = arrival_age is not None and arrival_age <= stuck_after

    if cycle_age > stuck_after and not arriving:
        return reading(
            LivenessState.CYCLE_STUCK,
            (_("%(device)s is heartbeating but has not completed a scan "
               "cycle since it was paired %(age)s ago, and nothing has "
               "arrived from it; it scans every %(interval)s minutes.")
             if device.last_cycle_completed_at is None else
             _("%(device)s is heartbeating but its last completed scan "
               "cycle was %(age)s ago and nothing has arrived from it "
               "since; it scans every %(interval)s minutes.")) % {
                "device": device.name,
                "age": humanize_seconds(cycle_age.total_seconds()),
                "interval": device.check_interval_minutes,
            },
        )

    # Green on the arrivals rather than on the cycle. Said out loud, because
    # the ordinary sentence would report a scan cycle hours old next to a
    # verdict of "fine" and read like an oversight. A backlog is a finding
    # about a machine that is otherwise working, which is the same reason
    # clock skew is a note and not a state.
    if cycle_age > stuck_after:
        return reading(
            LivenessState.ONLINE,
            _("%(device)s is sending files -- the last arrived %(arrived)s "
              "ago -- but has not finished a scan cycle for %(age)s. That is "
              "what a machine working through a backlog looks like; it "
              "finishes a cycle once it has been round every station.") % {
                "device": device.name,
                "arrived": humanize_seconds(arrival_age.total_seconds()),
                "age": humanize_seconds(cycle_age.total_seconds()),
            },
        )

    return reading(
        LivenessState.ONLINE,
        _("%(device)s heartbeated %(age)s ago and completed a scan cycle "
          "%(cycle)s ago.") % {
            "device": device.name,
            "age": humanize_seconds(heartbeat_age.total_seconds()),
            "cycle": humanize_seconds(cycle_age.total_seconds()),
        },
    )


def source_check_result(connection, now=None):
    """``connection``'s layer-5 verdict, from its machine's heartbeats.

    The ingestion diagnostic asks every plugin the same question at layer 5 --
    *is the source accepting us and offering data?* -- and for an agent
    connection the source is the country server. It cannot be dialed, so the
    answer is read from what it last said about itself, which makes this the
    one source check in the fleet that performs no I/O at all and cannot
    overrun core's probe budget.

    ``category`` is always ``None``. The closed failure vocabulary is a
    vocabulary of things a *server said* -- codes, refusals, handshakes --
    and a machine that has gone quiet has said nothing. Claiming one would
    put an invented cause in the operator's headline.
    """
    from adl.core.source_checks import SourceCheckResult, SourceCheckStatus

    liveness = liveness_of(connection.device, now)

    note = liveness.skew_note
    message = f"{liveness.message} {note}".strip() if note else liveness.message

    return SourceCheckResult(
        status=(SourceCheckStatus.FAILED if liveness.is_fault
                else SourceCheckStatus.OK),
        category=None,
        message=message,
    )
