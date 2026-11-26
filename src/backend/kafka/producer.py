# src/backend/kafka/producer.py
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Callable, Dict, Optional

try:
    from confluent_kafka import Producer, KafkaException
    CONFLUENT_AVAILABLE = True
except ImportError:
    CONFLUENT_AVAILABLE = False

from pathlib import Path

# Ensure kafka dir and backend root are on sys.path
KAFKA_DIR = Path(__file__).resolve().parent
BACKEND_DIR = KAFKA_DIR.parent
for p in (str(KAFKA_DIR), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from .config import KAFKA_BOOTSTRAP_SERVERS, KAFKA_CLIENT_ID, KAFKA_TOPIC_EVENTS
except (ImportError, ValueError):
    from config import KAFKA_BOOTSTRAP_SERVERS, KAFKA_CLIENT_ID, KAFKA_TOPIC_EVENTS


logger = logging.getLogger("dinesight.kafka.producer")


class DineSightKafkaProducer:
    """
    Kafka Producer for DineSight computer vision and API events.

    Features:
    - Idempotence: exactly-once delivery semantics per partition (enable.idempotence=True).
    - Partitioning: partitioned by `camera_id` to maintain strict chronological order per stream.
    - Retry logic: automatic broker retries with exponential backoff.
    - Latency headers: injects `producer_ts` for end-to-end processing latency measurement.
    """

    def __init__(
        self,
        bootstrap_servers: str = KAFKA_BOOTSTRAP_SERVERS,
        client_id: str = KAFKA_CLIENT_ID,
        default_topic: str = KAFKA_TOPIC_EVENTS,
        enable_idempotence: bool = True,
        retries: int = 5,
    ) -> None:
        self.bootstrap_servers = bootstrap_servers
        self.default_topic = default_topic
        self.client_id = client_id
        self._producer: Optional[Any] = None
        self.is_connected = False
        self.sent_count = 0
        self.failed_count = 0

        if not CONFLUENT_AVAILABLE:
            logger.warning("confluent_kafka is not installed; running in offline mock mode.")
            return

        conf = {
            "bootstrap.servers": self.bootstrap_servers,
            "client.id": f"{self.client_id}-{int(time.time())}",
            "enable.idempotence": enable_idempotence,
            "acks": "all" if enable_idempotence else "1",
            "retries": retries,
            "retry.backoff.ms": 200,
            "queue.buffering.max.messages": 100000,
            "queue.buffering.max.ms": 20,  # small batching window for low latency
        }

        try:
            self._producer = Producer(conf)
            self.is_connected = True
            logger.info("Kafka Producer initialized (broker: %s, topic: %s)", bootstrap_servers, default_topic)
        except Exception as exc:
            logger.error("Failed to initialize Kafka Producer: %s", exc)
            self._producer = None

    def _delivery_report(self, err: Any, msg: Any, custom_callback: Optional[Callable] = None) -> None:
        if err is not None:
            self.failed_count += 1
            logger.error("Kafka delivery failed: %s", err)
            if custom_callback:
                custom_callback(err, None)
        else:
            self.sent_count += 1
            if custom_callback:
                custom_callback(None, msg)

    def send_event(
        self,
        event: Dict[str, Any],
        topic: Optional[str] = None,
        on_delivery: Optional[Callable] = None,
        flush_sync: bool = False,
    ) -> bool:
        """
        Send a DineSight event to Kafka.
        The event is partitioned by camera_id so all events for a camera are ordered.
        """
        target_topic = topic or self.default_topic
        camera_id = str(event.get("camera_id", "default"))

        # Inject producer timestamp for latency monitoring
        payload_dict = dict(event)
        if "_kafka" not in payload_dict:
            payload_dict["_kafka"] = {"producer_ts": time.time()}

        payload_bytes = json.dumps(payload_dict).encode("utf-8")
        key_bytes = camera_id.encode("utf-8")

        if not self._producer:
            # Fallback/dry-run mode
            self.sent_count += 1
            if on_delivery:
                on_delivery(None, {"mock": True, "event": event})
            return True

        try:
            self._producer.produce(
                topic=target_topic,
                key=key_bytes,
                value=payload_bytes,
                on_delivery=lambda err, msg: self._delivery_report(err, msg, on_delivery),
            )
            self._producer.poll(0)

            if flush_sync:
                self._producer.flush(timeout=2.0)

            return True
        except BufferError:
            logger.warning("Kafka local producer queue is full; flushing...")
            self._producer.flush(timeout=3.0)
            return False
        except KafkaException as exc:
            logger.error("KafkaException while producing message: %s", exc)
            self.failed_count += 1
            return False
        except Exception as exc:
            logger.error("Unexpected error producing message: %s", exc)
            self.failed_count += 1
            return False

    def flush(self, timeout: float = 5.0) -> int:
        """Wait for all outstanding messages to be delivered. Returns number of remaining messages."""
        if self._producer:
            return self._producer.flush(timeout=timeout)
        return 0

    def close(self) -> None:
        self.flush(timeout=3.0)

