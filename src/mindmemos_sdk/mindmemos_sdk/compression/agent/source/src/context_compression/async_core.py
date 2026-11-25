"""Background compression; only request threads commit memory.

v4 is single-flight: a trigger arriving while a job is in flight is skipped.
dual_a is preemptive: a newer trigger supersedes the in-flight job, because the
older summary would cover a stale prefix of a history the agent has moved past.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import threading
import time

from .audit import persist
from .cancellation import Cancellation, CompressionCancelled
from .compressor import emit
from .core import ContextManager, eligible_end, validate_result
from .types import CompressionSnapshot


@dataclass
class PendingCompression:
    snapshot: CompressionSnapshot
    started: float
    cancellation: Cancellation = field(default_factory=Cancellation)
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    result: object = None
    error: Exception | None = None
    elapsed: float = 0.0
    ready_at: float = 0.0


class AsyncContextManager(ContextManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.config.method not in ('v4', 'dual_a') or self.config.execution_mode != 'async':
            raise ValueError('AsyncContextManager requires async v4 or dual_a')
        self.preemptive = self.config.method == 'dual_a'
        self._pending = None
        # Superseded workers stay here until shutdown so the usage rows they
        # write while unwinding are in the log when the run is measured.
        self._orphans = []

    def _run_background(self, job):
        snapshot = job.snapshot
        identity = dict(call_id=snapshot.call_id, step=snapshot.step,
                        phase='execution',
                        generation=snapshot.generation, revision=snapshot.revision,
                        execution_mode='async', start=snapshot.start, end=snapshot.end)
        try:
            result = self.compressor(deepcopy(snapshot), cancellation=job.cancellation)
            validate_result(result)
            if snapshot.compression_mode == 'full' and not result.stage_end:
                raise ValueError('Capacity full result must replace memory')
            # Persist even when no subsequent Agent request ever consumes C.
            persist(self.audit_sink, dict(identity, event='compression_result',
                    stage_end=result.stage_end, confidence=result.confidence,
                    reason=result.reason, C=result.C), self.event_sink)
            job.result = result
            if time.monotonic() - job.started > self.config.timeout_seconds:
                raise TimeoutError('compression_timeout')
        except Exception as exc:
            job.error = exc
        finally:
            job.ready_at = time.monotonic()
            job.elapsed = job.ready_at - job.started
            emit(self.event_sink, dict(identity, event='compression_ready',
                 success=job.error is None, elapsed_seconds=job.elapsed))
            # Publication barrier: request threads never see a partial result.
            job.done.set()

    def _supersede_pending(self, successor_id):
        """Drop the in-flight job for a newer one covering a longer prefix.

        Its summary can never reach memory, but its request may already be
        billed, so the ledger needs a terminal event under the old call_id. The
        worker is kept in _orphans until shutdown, so the usage row it writes
        while unwinding is still in the log when the run is measured.
        """
        job, self._pending = self._pending, None
        self._orphans.append(job)
        # The job owns its handle, so this cancels even a worker that has not
        # yet reached the compressor, and can never touch the successor's.
        job.cancellation.cancel()
        snapshot = job.snapshot
        elapsed = time.monotonic() - job.started
        common = dict(compression_id=snapshot.call_id, trigger_step=snapshot.step, step=self._step,
                      start=snapshot.start, end=snapshot.end, elapsed_seconds=elapsed)
        # Both closeout events belong to the victim. The successor's id is
        # already current here, so restore it rather than clearing: its snapshot
        # is audited immediately after this returns and is keyed by that id.
        # compression_failed matches the synchronous cancellation shape, so a
        # preempted call is accounted for instead of read as an unexplained gap.
        successor_call_id = self._compression_call_id
        self._compression_call_id = snapshot.call_id
        try:
            self._emit('compression_superseded', successor_id=successor_id, **common)
            self._emit('compression_failed', error_type='CompressionCancelled',
                       cause='cancelled', execution_mode='async', **common)
        finally:
            self._compression_call_id = successor_call_id

    def _may_supersede(self):
        """Only an escalation from incremental to full may discard work.

        Anything else — more of the same mode, or a smaller trigger — would
        throw away a running compression for one that is no cheaper, which
        starves long tasks: nothing ever lands and the raw buffer grows without
        bound. Escalation can happen at most once per chain, so it terminates.
        """
        inflight = self._pending.snapshot.compression_mode
        end = eligible_end(self._raw, 0, self.config.keep_recent_groups)
        prospective = self._trigger_mode(self._raw[:end])
        return inflight == 'incremental' and prospective == 'full', inflight, prospective

    def _compress_if_needed(self):
        if self._pending is not None:
            allowed, inflight, prospective = (False, self._pending.snapshot.compression_mode, None)
            if self.preemptive:
                allowed, inflight, prospective = self._may_supersede()
            if not allowed:
                self._emit('compression_inflight', compression_id=self._pending.snapshot.call_id,
                           inflight_mode=inflight, prospective_mode=prospective)
                return
        # Prepare first: a trigger that does not fire must not cost the in-flight job.
        prepared = self._prepare_compression()
        if prepared is None:
            if self._pending is not None:
                self._emit('compression_inflight', compression_id=self._pending.snapshot.call_id)
            return
        snapshot, started = prepared
        if self._pending is not None:
            self._supersede_pending(snapshot.call_id)
        try:
            self._audit('compression_snapshot', task=snapshot.task, memory=list(snapshot.memory),
                        raw=list(snapshot.raw), start=snapshot.start, end=snapshot.end,
                        config=self.config.to_dict(), compression_mode=snapshot.compression_mode,
                        **self._threshold_evidence())
            job = PendingCompression(snapshot, started)
            job.thread = threading.Thread(target=self._run_background, args=(job,),
                                          name='v4-compression-' + snapshot.call_id[:8], daemon=True)
            # close() may arrive while snapshot persistence is in progress. It
            # closes the compressor permanently, so even a late worker cannot
            # dispatch a new API request after cancellation.
            with self._lifecycle_lock:
                self._pending = job
                job.thread.start()
        except Exception as exc:
            self._pending = None
            self._emit('compression_failed', error_type=type(exc).__name__,
                       cause=getattr(exc, 'code', 'invalid_result'), start=snapshot.start, end=snapshot.end,
                       execution_mode='async', trigger_step=snapshot.step,
                       elapsed_seconds=time.monotonic()-started)
        finally:
            self._compression_call_id = None

    def _poll_compression(self):
        job = self._pending
        if job is None or not job.done.is_set():
            return False
        # Publication follows all worker I/O and audit writes; the thread may
        # still be returning, but has no remaining work that can mutate the job.
        snapshot = job.snapshot
        self._compression_call_id = snapshot.call_id
        extra = dict(execution_mode='async', trigger_step=snapshot.step,
                     steps_while_pending=self._step-snapshot.step,
                     ready_to_apply_seconds=max(0.0, time.monotonic()-job.ready_at))
        self._retry_step = self._step + self.config.retry_after_steps
        try:
            if job.error is not None:
                raise job.error
            self._commit_result(snapshot, job.result, job.elapsed, **extra)
        except Exception as exc:
            self._emit('compression_failed', error_type=type(exc).__name__,
                       cause=getattr(exc, 'code', 'timeout' if isinstance(exc, TimeoutError) else 'invalid_result'),
                       start=snapshot.start, end=snapshot.end, elapsed_seconds=job.elapsed, **extra)
        finally:
            self._pending = None
            self._compression_call_id = None
        return True

    def wait_closed(self, timeout=2.0):
        """Bounded worker/ledger drain, including successful but unused summaries."""
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=max(0.0, deadline-time.monotonic())):
            return False
        try:
            for orphan in self._orphans:
                orphan.thread.join(max(0.0, deadline-time.monotonic()))
                if orphan.thread.is_alive():
                    return False
            job = self._pending
            if job is not None:
                job.thread.join(max(0.0, deadline-time.monotonic()))
                if job.thread.is_alive():
                    return False
                # Only shutdown may drain without another main request.
                if self._closed.is_set():
                    self._poll_compression()
            return True
        finally:
            self._lock.release()
