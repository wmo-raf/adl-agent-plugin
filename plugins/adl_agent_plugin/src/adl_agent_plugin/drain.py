"""
Turning staged files into observations.

This is the second half of the agent's journey. The first half ends with
bytes on storage and a ledger row saying ``received``; this half decodes
those bytes, hands the records to core, and writes down what became of each
file.

Almost none of the decoding is here. The agent ships **raw files** for exactly
this reason (story 12): the FTP plugin's decoder ecosystem -- the standard CSV
reader, TOA5, and every country-specific decoder written against that registry
-- already knows how to read what vendor software writes, and it reads local
file paths, which is what a staged file is. So this module resolves the
connection's decoder through the FTP plugin, and hands each file to the shared
decode-and-stamp pipeline that plugin exposes (wmo-raf/adl#271). What is left
here is what is genuinely the agent's own:

- **finding the work** -- which rows are waiting, oldest first;
- **giving the pipeline a local path**, even when the bytes live somewhere
  that has no paths;
- **writing down the outcome** -- ``processed`` with a count, or ``failed``
  with the reason a technician needs.

Nothing here decides *when* to run. The scheduled pass and the nudge that
follows an upload both arrive through ``Plugin.process_station``, which holds
the per-station lock that keeps them from colliding.
"""

import logging
import os
import tempfile
from contextlib import contextmanager

from celery.exceptions import SoftTimeLimitExceeded
from django.utils.translation import gettext as _

from adl_ftp_plugin.decoder_resolution import resolve_decoder_for_connection
from adl_ftp_plugin.processing import decode_and_stamp


logger = logging.getLogger(__name__)


def resolve_connection_decoder(connection, task_logger=None):
    """The connection's decoder bound to its configuration, or ``None``.

    A connection with no decoder chosen yet, or one naming a decoder this
    instance does not have installed, is a configuration fault and not a fault
    of any file: the files stay ``received``, and choosing the decoder drains
    them. Marking them failed instead would hide one misconfiguration behind a
    hundred file-level errors, every one of which would then need resetting.
    """
    log = task_logger or logger

    if not connection.decoder:
        log.error(
            "No decoder is set on connection %s. Its files are being received "
            "and staged, but nothing can read them until one is chosen.",
            connection.name,
        )
        return None

    try:
        return resolve_decoder_for_connection(connection, task_logger=log)
    except Exception as e:
        # Chiefly a decoder named on the connection whose plugin is not
        # installed on this instance -- the registry raises rather than
        # answering None.
        log.error(
            "Decoder %s on connection %s could not be resolved: %s",
            connection.decoder, connection.name, e,
        )
        return None


def drain_station_link(station_link, data_files, decoder, task_logger=None):
    """Generator over the records of every file in ``data_files``.

    Yields records for core to persist, one file at a time, with core's flush
    marker between files so that each file is stamped only once its own
    records are in the database (that ordering is the shared pipeline's, not
    this module's).

    A file that will not decode is marked failed and the drain moves on: one
    unreadable file must not cost a station the rest of its backlog.

    The files are passed in rather than queried here so that the caller can
    count what it is about to drain without asking the same question twice.
    """
    log = task_logger or logger

    for data_file in data_files:
        yield from _drain_file(data_file, decoder, station_link, log)


def _drain_file(data_file, decoder, station_link, log):
    """One staged file, decoded and accounted for."""
    recording = RecordingDecoder(decoder)

    try:
        with staged_local_path(data_file) as path:
            decoded = yield from decode_and_stamp(
                StagedFile(data_file, path),
                recording,
                station_link,
                task_logger=log,
            )
    except SoftTimeLimitExceeded:
        # The batch's time budget ran out, not this file. Left ``received`` so
        # the next run picks it up, and re-raised so the limit actually stops
        # the batch instead of rolling on to the hard kill.
        raise
    except Exception as e:
        # Getting at the bytes failed -- storage unreachable, the instance out
        # of disk, the object not there. Never a decode failure: the pipeline
        # catches those itself and answers False below.
        #
        # Left ``received`` rather than marked failed, and deliberately. A
        # failed row is never retried and nothing in this slice can bring one
        # back, so one bad minute from the object store would permanently
        # sideline a country's files. A storage fault is the instance's to fix,
        # not the file's to be blamed for; when it is fixed the next run drains
        # them. The cost is a log line per run until then, which is the right
        # thing to be noisy about.
        log.error(
            "Could not read the staged bytes of %s: %s. Leaving it to be "
            "picked up again once storage is healthy.",
            data_file.file_name, e,
        )
        return

    if not decoded:
        data_file.mark_failed(
            recording.error or _("The file could not be decoded.")
        )


class RecordingDecoder:
    """The connection's decoder, remembering why a file would not decode.

    The shared pipeline catches a decoder's exception, logs it and reports
    failure -- which is right for the FTP plugin, whose next run simply tries
    the file again. The agent has to *keep* the reason: a failed file is a
    diagnosable event on the file's own admin page (story 20), and by the time
    the pipeline has answered "no", the exception is gone.

    So the decoder is wrapped rather than the pipeline changed: this remembers
    the message on its way past and re-raises, leaving the pipeline's own
    handling exactly as it was.

    Decoding is the whole of the interface: the pipeline asks a decoder for
    nothing else, so this delegates nothing else either.
    """

    def __init__(self, decoder):
        self.decoder = decoder
        self.error = None

    def decode(self, file_path):
        self.error = None
        try:
            return self.decoder.decode(file_path)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            raise


class StagedFile:
    """One ledger row, as the shared decode-and-stamp pipeline sees a file.

    The pipeline duck-types four things off a staged file: a local path to
    decode, a name for the log, and the two stamps it writes once core has
    persisted the file's records. The row answers three of them itself. It
    cannot answer the path -- staged bytes may sit on object storage, which
    has none -- and its notion of "processed" is a status as well as a
    timestamp, so both are answered here and written back in one save.
    """

    def __init__(self, data_file, path):
        self.data_file = data_file
        self.file = _StagedPath(path)
        self.file_name = data_file.file_name
        self.processed_at = None
        self.values_saved = None

    def save(self, update_fields=None):
        """Write the pipeline's stamps back to the row, and the status with them.

        ``update_fields`` is accepted and ignored: the pipeline names the two
        columns it knows about, and the row writes those two plus the rest of
        its verdict (see ``AgentStationDataFile.OUTCOME_FIELDS``), which is a
        superset. Narrowing to what was asked for would leave a re-uploaded
        file's cleared error standing beside its new count.
        """
        self.data_file.mark_processed(
            values_saved=self.values_saved, processed_at=self.processed_at,
        )


class _StagedPath:
    """The one attribute the pipeline reads off ``staged_file.file``."""

    __slots__ = ("path",)

    def __init__(self, path):
        self.path = path


@contextmanager
def staged_local_path(data_file):
    """A path on local disk holding this file's bytes, while the block runs.

    Decoders open files by path, so bytes on a storage backend that has no
    paths -- MinIO or S3, which an instance is free to configure -- are copied
    to a temporary file for the duration of the decode and removed afterwards.
    On the ordinary local-disk setup nothing is copied and the storage's own
    path is used.
    """
    path = _storage_path(data_file)

    if path is not None:
        yield path
        return

    with tempfile.NamedTemporaryFile(suffix=f"-{data_file.file_name}") as copy:
        with data_file.file.open("rb") as source:
            for chunk in source.chunks():
                copy.write(chunk)
        copy.flush()

        yield copy.name


def _storage_path(data_file):
    """The storage's own local path for this file, or ``None``."""
    try:
        path = data_file.file.path
    except (NotImplementedError, AttributeError, ValueError):
        return None

    return path if os.path.exists(path) else None
