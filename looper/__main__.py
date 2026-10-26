"""Entry point: python3 -m looper"""

from __future__ import annotations

import logging
import os
import signal
import sys

from .config import Config
from .controller import Controller


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOOPER_LOG", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",  # journald adds timestamps
        stream=sys.stdout,
    )
    log = logging.getLogger("looper")
    cfg = Config()
    ctl = Controller(cfg)
    ctl.mqtt_manager = None

    if cfg.section("web")["enabled"]:
        try:
            from . import web
            web.start(cfg.section("web"), ctl)
        except Exception:
            log.exception("web UI failed to start (playback continues)")

    mq = None
    try:
        from .mqtt_auto import MqttManager
        mq = MqttManager(cfg, ctl)
        ctl.mqtt_manager = mq
        mq.start()
    except Exception:
        log.exception("MQTT failed to start (playback continues)")

    def stop(*_):
        ctl.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        ctl.run()
    finally:
        if mq:
            mq.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
