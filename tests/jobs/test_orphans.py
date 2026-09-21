"""A job process ends when the run that started it does.

SIGKILL runs none of the parent's cleanup, so its job processes used to be
orphaned and keep loading rows and writing run state -- while the run lock,
held by the dead parent, was already free, so the next `bauta run` started
alongside the orphan and the two loaded over each other.

What a parent's death looks like to a job is the end of a pipe only the parent
holds, so that is what this tests: no signals, no reading the process table,
and the same result on every platform.
"""
import logging
import time

from bauta.jobs.workers import EXIT_ORPHANED, PROCESS_CONTEXT, _initializeWorker

# The watchdog exits as soon as the pipe reports end-of-file, which is
# immediate; these seconds are for a loaded machine, not for the mechanism.
EXIT_SECONDS = 10.0
START_SECONDS = 60.0


def waitForever(logEnd, parentAlive, ready):
    """A job process, reduced to what every one of them does: start the way a
    job starts, then get on with work that would not end on its own.

    Through _initializeWorker rather than the watchdog directly, so that a job
    that stopped watching for its parent fails this. At module level because a
    spawned process must be able to import it.
    """

    _initializeWorker(logEnd, parentAlive, logging.INFO)
    ready.send('started')

    while True:
        time.sleep(0.05)


def _started():
    """A job process running waitForever, with the pipes its run holds."""

    childEnd, parentEnd = PROCESS_CONTEXT.Pipe(duplex=False)
    readyEnd, sendReady = PROCESS_CONTEXT.Pipe(duplex=False)
    # The log pipe a worker forwards its records over; nothing reads it here.
    logEnd, sendLog = PROCESS_CONTEXT.Pipe(duplex=False)
    process = PROCESS_CONTEXT.Process(target=waitForever, args=(sendLog, childEnd, sendReady), daemon=True)
    process.start()
    # Only the child holds these now, as in _JobProcess.
    childEnd.close()
    sendReady.close()
    sendLog.close()

    assert readyEnd.poll(START_SECONDS), 'the job process never started'
    assert readyEnd.recv() == 'started'

    return process, parentEnd, logEnd


def _ended(process, parentEnd, logEnd):

    parentEnd.close()
    logEnd.close()
    process.join(EXIT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join()


def test_a_job_process_exits_when_the_pipe_to_its_run_closes():
    process, parentEnd, logEnd = _started()

    try:
        assert process.is_alive()

        # What the death of the run does to the pipe, without killing pytest.
        parentEnd.close()
        process.join(EXIT_SECONDS)

        assert not process.is_alive(), 'the job process outlived the run that started it'
        assert process.exitcode == EXIT_ORPHANED
    finally:
        _ended(process, parentEnd, logEnd)


def test_a_job_process_keeps_working_while_its_run_holds_the_pipe():
    """The watchdog waits for end-of-file, not for a message, so nothing the
    run does or fails to send may end a job early.
    """
    process, parentEnd, logEnd = _started()

    try:
        process.join(1.0)

        assert process.is_alive()
    finally:
        _ended(process, parentEnd, logEnd)
