"""Parallel conversion runner: fans a file list out across a thread pool."""

import sys
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path

from heic2jpg.convert import Options, Result, Status, convert_one


def _tally(counts: Counter[Status], result: Result, verbose: bool) -> None:
    """Update counters and print per-file output for one conversion result."""
    counts[result.status] += 1
    if result.status is Status.FAIL:
        # Always print failures, even without -v. Silent failures are how
        # data loss happens.
        print(f"FAIL {result.src}: {result.error}", file=sys.stderr)
    elif verbose:
        label = "ok" if result.status is Status.OK else "skip"
        print(f"{label}: {result.src}", file=sys.stderr)
    for w in result.warnings:
        print(f"warning: {result.src}: {w}", file=sys.stderr)


def run_pool(
    files: list[Path],
    outs: list[Path],
    jobs: int,
    opts: Options,
    verbose: bool,
) -> tuple[int, int, int, bool]:
    """
    Run all conversions through a thread pool.

    Parameters:
    files   : list of Path to convert
    outs    : output path for each file (same order; see plan_outputs)
    jobs    : int worker count
    opts    : conversion settings forwarded to convert_one
    verbose : print per-file progress lines

    Returns:
    Tuple of ``(ok_count, skipped_count, failed_count, interrupted)``.

    Interruption:
    Relies on Python's default SIGINT behaviour: Ctrl-C raises
    KeyboardInterrupt in the main thread (worker threads never receive
    it). We catch it, cancel everything still queued, and let in-flight
    conversions finish naturally - preserving their atomic-write
    guarantee. A second Ctrl-C during that drain propagates and aborts.
    """
    counts: Counter[Status] = Counter()
    futures: list[Future[Result]] = []
    tallied: set[Future[Result]] = set()
    interrupted = False

    # Threads, not processes, because pillow-heif releases the GIL during HEIF
    # decode and Pillow releases it during JPEG encode (convert_with_pillow
    # saves to a path), so both already run in parallel.
    # `with ThreadPoolExecutor(...)` ensures the pool is shut down cleanly even
    # if an exception escapes the loop. Without the context manager, Python
    # would leak worker threads.
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        try:
            # Submit everything up front. The pool internally queues anything
            # over `max_workers` and spawns them as workers free up.
            futures = [ex.submit(convert_one, f, opts, out) for f, out in zip(files, outs, strict=True)]
            # `as_completed` yields futures as they finish, NOT in
            # submission order. This means progress feels smooth even if
            # one file is much slower than the rest.
            for fut in as_completed(futures):
                tallied.add(fut)
                _tally(counts, fut.result(), verbose)
        except KeyboardInterrupt:
            interrupted = True
            # Drop everything still queued; conversions already running
            # finish cleanly while the `with` block joins the workers.
            ex.shutdown(cancel_futures=True)

    if interrupted:
        for fut in futures:
            # exception() is only non-None for a propagated KeyboardInterrupt
            # (convert_one turns everything else into a FAIL Result).
            finished = fut.done() and not fut.cancelled() and fut.exception() is None
            if finished and fut not in tallied:
                _tally(counts, fut.result(), verbose)
        print("Interrupted.", file=sys.stderr)
    return counts[Status.OK], counts[Status.SKIP], counts[Status.FAIL], interrupted
