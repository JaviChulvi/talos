import asyncio
import logging
import signal

from sqlalchemy.exc import OperationalError

from backend.app.config import get_settings
from worker.diagnostics import DiagnosticManager
from worker.lifecycle import Worker, configure_inference, connect_runtime, worker_lock


async def run():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    worker = Worker()
    diagnostics = None
    task = None
    failures = 0

    async def cycle():
        await asyncio.to_thread(worker.recover)
        await asyncio.to_thread(worker.process_one)

    try:
        while not stop.is_set():
            delay = get_settings().lifecycle_poll_seconds
            try:
                if diagnostics is None:
                    # A replacement worker must attach to existing agent networks
                    # before it can dispatch their persisted diagnostic queue.
                    await asyncio.to_thread(worker.recover, yield_to_operations=False)
                    diagnostics = DiagnosticManager(
                        worker.sessions, connect_runtime, configure=configure_inference
                    )
                await diagnostics.tick()
                if task is None or task.done():
                    if task is not None:
                        completed, task = task, None
                        completed.result()
                        failures = 0
                    task = asyncio.create_task(cycle())
            except OperationalError:
                failures = min(failures + 1, 5)
                delay = min(2**failures, 30)
                logging.warning("Worker database unavailable; retrying in %s seconds", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
    finally:
        try:
            if diagnostics is not None:
                await diagnostics.close()
            if task is not None:
                await task
        finally:
            worker.client.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with worker_lock(get_settings().worker_state_dir):
        logging.info("Talos single lifecycle worker started")
        asyncio.run(run())


if __name__ == "__main__":
    main()
