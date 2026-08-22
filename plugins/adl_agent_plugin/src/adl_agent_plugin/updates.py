"""
What each machine is told to run.

The rule the whole update story rests on is here, in one function: **a pinned
device is only ever offered its pin** (story 29). Enforced server-side, and
enforced again when the package itself is asked for, because a feed that
declined to mention a newer release while still serving it to anyone who
guessed the URL would be a pin that holds only as long as the agent asks
nicely.

Everything else an agent decides for itself, and deliberately so: whether
what it is offered is newer than what it runs, and whether the bytes hash to
what this instance said they would. Neither is a judgement ADL is in a
position to make about a machine it cannot see.
"""

from django.utils.translation import gettext as _

from .models import AGENT_TIER_ARTIFACTS, AgentRelease

#: The install tiers an agent may ask as. Anything else is a machine talking
#: to an ADL it does not understand, and is refused rather than guessed at.
AGENT_TIERS = tuple(AGENT_TIER_ARTIFACTS)

DEFAULT_TIER = "service"


class UpdateOffer:
    """What one device should be running, and why.

    A small object rather than a tuple because every caller wants a different
    part of it: the feed serialises all of it, the download endpoint reads
    only which release it names, and the tests read the sentence.
    """

    def __init__(self, release=None, artifact=None, pinned=False, reason=""):
        self.release = release
        self.artifact = artifact
        self.pinned = pinned
        self.reason = reason

    @property
    def version(self):
        return self.release.version if self.release else None


def offered_release(device):
    """The release this device may have, and whether a pin decided it.

    Returns ``(release_or_None, pinned, reason)``. The reason is written here
    rather than by the agent because this side knows which of the two silences
    it is: an instance holding nothing at all, or one holding everything
    except the version an operator pinned this machine to.
    """
    pin = (device.pinned_version or "").strip()

    if pin:
        release = AgentRelease.published().filter(version=pin).first()

        if release is None:
            return None, True, _(
                "This device is pinned to %(version)s, which this ADL "
                "instance does not hold as a published release."
            ) % {"version": pin}

        return release, True, _(
            "This device is pinned to %(version)s."
        ) % {"version": pin}

    release = AgentRelease.latest_published()

    if release is None:
        return None, False, _(
            "This ADL instance holds no published agent release."
        )

    return release, False, ""


def offer_for(device, tier):
    """The whole answer to "what should this machine be running?".

    ``tier`` decides which package is named, never which release: the two
    tiers of one release are the same release, and a machine must not be held
    back or pushed forward by how it happened to be installed.
    """
    release, pinned, reason = offered_release(device)

    if release is None:
        return UpdateOffer(pinned=pinned, reason=reason)

    artifact = release.artifact_for_tier(tier)

    if artifact is None:
        # A release published with only one tier's package built. Named
        # anyway: an agent that was told nothing would report "up to date",
        # and a fleet half of which never updates is worth being loud about.
        reason = reason or _(
            "Release %(version)s has no package for a %(tier)s-tier install."
        ) % {"version": release.version, "tier": tier}

    return UpdateOffer(
        release=release, artifact=artifact, pinned=pinned, reason=reason,
    )


def artifact_offered_to(device, version, kind):
    """The package of ``kind`` at ``version``, if this device may have it.

    The second half of the pin. This is what the download endpoint asks, and
    it asks about the release the device is *offered* rather than about the
    releases the instance holds -- so a pinned machine that constructs the URL
    of a newer package is answered exactly as a machine asking for a version
    that does not exist.
    """
    release, _pinned, _reason = offered_release(device)

    if release is None or release.version != version:
        return None

    return release.artifact_for(kind)
