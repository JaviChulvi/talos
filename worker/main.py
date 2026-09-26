import asyncio
import logging
import signal

from backend.app.config import get_settings
from worker.diagnostics import DiagnosticManager
from worker.lifecycle import Worker, connect_runtime, worker_lock


async def run():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    worker = Worker()
    diagnostics = DiagnosticManager(worker.sessions, connect_runtime)
    task = None
    try:
        await asyncio.to_thread(worker.recover)
        diagnostics.recover()
        while not stop.is_set():
            await diagnostics.tick()
            if task is None or task.done():
                if task is not None:
                    task.result()
                task = asyncio.create_task(asyncio.to_thread(worker.process_one))
            try:
                await asyncio.wait_for(stop.wait(), timeout=get_settings().lifecycle_poll_seconds)
            except TimeoutError:
                pass
    finally:
        await diagnostics.close()
        if task is not None:
            await task
        worker.client.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with worker_lock(get_settings().worker_state_dir):
        logging.info("Talos single lifecycle worker started")
        asyncio.run(run())


if __name__ == "__main__":
    main()
