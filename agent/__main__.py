"""Run with PYTHONPATH=server uv run --project agent python -m agent."""
import argparse
import fcntl
import logging
import os
from pathlib import Path
import signal
import threading

from collector import Collector
from .cloud import Cloud, Uploader
from .setup import PrinterSettings, run_setup
from .spool import Spool


def main():
    parser = argparse.ArgumentParser(description='Collect Bambu telemetry locally for hosted Airkeep.')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('AIRKEEP_AGENT_DATA', '.local/agent')))
    parser.add_argument('--setup', action='store_true', help='Replace enrollment and printer settings through local setup')
    parser.add_argument('--setup-port', type=int, default=8788)
    args = parser.parse_args()
    if not 1 <= args.setup_port <= 65535:
        parser.error('Choose a setup port between 1 and 65535.')
    os.umask(0o077)
    args.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.data_dir.chmod(0o700)
    # Keep only one collector/uploader for this identity, including during setup.
    with (args.data_dir / 'agent.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('An agent is already using this data directory.') from None
        spool = Spool(args.data_dir / 'agent.sqlite3')
        collector = cloud = None
        stopped = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        try:
            if args.setup or spool.configuration() is None:
                run_setup(args.data_dir, spool, args.setup_port, stopped)
            config = spool.configuration()
            settings = PrinterSettings.validate(config)
            collector = Collector(settings, spool)
            cloud = Cloud(config['origin'])
            uploader = Uploader(cloud, spool, config['token'])
            # Suppress transport logs that can contain IPs, URLs or credentials.
            logging.getLogger('httpx').disabled = True
            logging.getLogger('httpcore').disabled = True
            collector.start()
            status = None
            while not stopped.is_set():
                uploader.step(collector.connected)
                if uploader.status != status:
                    status = uploader.status
                    print('Airkeep cloud ' + status, flush=True)
                    if uploader.revoked:
                        print('Stop the agent and run it with --setup to enroll again.', flush=True)
                stopped.wait(1)
        finally:
            if collector:
                collector.stop()
            if cloud:
                cloud.close()
            spool.close()


if __name__ == '__main__':
    main()
