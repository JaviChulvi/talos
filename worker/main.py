import logging
import signal
import threading


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    logging.info("Talos worker scaffold ready; lifecycle execution is not configured yet")
    stop.wait()


if __name__ == "__main__":
    main()
