
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    from confluent_kafka import Consumer, KafkaError
    CONFLUENT_AVAILABLE = True
except ImportError:
    CONFLUENT_AVAILABLE = False


# Ensure kafka dir and backend root are on sys.path
KAFKA_DIR = Path(__file__).resolve().parent
BACKEND_DIR = KAFKA_DIR.parent

for p in (str(KAFKA_DIR), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


try:
    from .config import (
        KAFKA_BOOTSTRAP_SERVERS,
        KAFKA_GROUP_ID,
        KAFKA_TOPIC_EVENTS,
    )
except (ImportError, ValueError):
    from config import (
        KAFKA_BOOTSTRAP_SERVERS,
        KAFKA_GROUP_ID,
        KAFKA_TOPIC_EVENTS,
    )


logger = logging.getLogger("dinesight.kafka.consumer")


class DineSightKafkaConsumer:
    """
    Kafka Consumer for DineSight events.

    Reads events from Kafka, calculates processing latency,
    persists events into PostgreSQL through save_event(),
    and dispatches real-time updates to WebSocket clients.

    Kafka offsets are committed manually only after successful
    database processing.
    """

    def __init__(
        self,
        bootstrap_servers: str = KAFKA_BOOTSTRAP_SERVERS,
        group_id: str = KAFKA_GROUP_ID,
        topic: str = KAFKA_TOPIC_EVENTS,
        auto_offset_reset: str = "earliest",
        broadcast_callback: Optional[
            Callable[[Dict[str, Any]], None]
        ] = None,
    ) -> None:

        self.bootstrap_servers = bootstrap_servers
        self.group_id = group_id
        self.topic = topic
        self.auto_offset_reset = auto_offset_reset
        self.broadcast_callback = broadcast_callback

        self._consumer: Optional[Any] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Metrics
        self.metrics = {
            "consumed": 0,
            "saved": 0,
            "duplicates": 0,
            "ignored": 0,
            "errors": 0,
            "latest_latency_ms": 0.0,
            "avg_latency_ms": 0.0,
            "total_latency_ms": 0.0,
            "start_time": time.time(),
        }

        if not CONFLUENT_AVAILABLE:
            logger.warning(
                "confluent_kafka is not installed; "
                "Consumer cannot start in broker mode."
            )
            return

        conf = {
            "bootstrap.servers": self.bootstrap_servers,
            "group.id": self.group_id,
            "auto.offset.reset": self.auto_offset_reset,
            "enable.auto.commit": False,
            "session.timeout.ms": 10000,
            "max.poll.interval.ms": 300000,
        }

        try:
            self._consumer = Consumer(conf)
            self._consumer.subscribe([self.topic])

            logger.info(
                "Kafka Consumer initialized and subscribed to topic: %s "
                "(group: %s)",
                self.topic,
                self.group_id,
            )

        except Exception as exc:
            logger.exception(
                "Failed to initialize Kafka Consumer: %s",
                exc,
            )
            self._consumer = None

    def process_message(
        self,
        msg_value: bytes,
    ) -> Dict[str, Any]:
        """
        Deserialize a Kafka message and save it to PostgreSQL.

        Returns:
            saved / duplicate / ignored / error
        """

        try:
            event_data = json.loads(
                msg_value.decode("utf-8")
            )

            if not isinstance(event_data, dict):
                raise ValueError(
                    "Kafka message must contain a JSON object"
                )

            now = time.time()

            # Calculate processing latency
            kafka_meta = event_data.pop("_kafka", {})
            producer_ts = kafka_meta.get("producer_ts")

            if producer_ts is not None:
                latency_ms = max(
                    0.0,
                    (now - float(producer_ts)) * 1000.0,
                )

                self.metrics["latest_latency_ms"] = round(
                    latency_ms,
                    2,
                )

                self.metrics["total_latency_ms"] += latency_ms

            self.metrics["consumed"] += 1

            # Save event to PostgreSQL
            from db import save_event

            result = save_event(event_data)

            if not isinstance(result, dict):
                raise RuntimeError(
                    "save_event() returned an invalid result"
                )

            status = result.get("status")

            if status == "saved":
                self.metrics["saved"] += 1

            elif status == "duplicate":
                self.metrics["duplicates"] += 1

            elif status == "ignored":
                self.metrics["ignored"] += 1

            else:
                raise RuntimeError(
                    f"Unexpected save_event status: {status}"
                )

            # Broadcast only newly saved events.
            # Broadcast failure must not invalidate DB success.
            if (
                self.broadcast_callback
                and status == "saved"
            ):
                try:
                    self.broadcast_callback(event_data)

                except Exception:
                    logger.exception(
                        "WebSocket broadcast failed"
                    )

            # Update average latency
            if producer_ts is not None:
                self.metrics["avg_latency_ms"] = round(
                    self.metrics["total_latency_ms"]
                    / max(1, self.metrics["consumed"]),
                    2,
                )

            return result

        except Exception as exc:
            self.metrics["errors"] += 1

            logger.exception(
                "Error processing Kafka message: %s",
                exc,
            )

            return {
                "status": "error",
                "error": str(exc),
            }

    def poll_and_process(
        self,
        timeout: float = 1.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Poll one Kafka message, process it, then commit
        its offset only after successful database processing.
        """

        if not self._consumer:
            return None

        msg = self._consumer.poll(timeout)

        if msg is None:
            return None

        if msg.error():

            if msg.error().code() == KafkaError._PARTITION_EOF:
                return None

            logger.error(
                "Kafka Consumer error: %s",
                msg.error(),
            )

            self.metrics["errors"] += 1

            return {
                "status": "error",
                "error": str(msg.error()),
            }

        # Process message before committing its offset
        result = self.process_message(msg.value())

        status = result.get("status")

        # Only successful processing results can be committed
        if status not in (
            "saved",
            "duplicate",
            "ignored",
        ):

            logger.error(
                "Message processing failed. "
                "Kafka offset NOT committed."
            )

            return result

        # Commit only after successful DB processing
        try:
            self._consumer.commit(
                message=msg,
                asynchronous=False,
            )

            logger.info(
                "Kafka offset committed successfully (%s)",
                status,
            )

        except Exception as exc:
            self.metrics["errors"] += 1

            logger.exception(
                "Kafka offset commit failed: %s",
                exc,
            )

            return {
                "status": "error",
                "error": f"Offset commit failed: {exc}",
            }

        return result

    def start_background(self) -> None:
        """Start consumer loop in a background daemon thread."""

        if self._running:
            return

        if not self._consumer:
            logger.error(
                "Cannot start background consumer: "
                "Kafka Consumer is unavailable."
            )
            return

        self._running = True

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="KafkaConsumerThread",
        )

        self._thread.start()

        logger.info("Kafka Consumer thread started.")

    def _run_loop(self) -> None:
        """
        Background consumer loop.

        Stop consuming if a message cannot be processed,
        so later offsets are not committed past a failed message.
        """

        while self._running:

            try:
                result = self.poll_and_process(
                    timeout=0.5,
                )

                if (
                    result
                    and result.get("status") == "error"
                ):
                    logger.error(
                        "Consumer stopped because "
                        "message processing failed."
                    )

                    self._running = False
                    break

            except Exception:
                logger.exception(
                    "Unexpected exception in consumer loop"
                )

                self.metrics["errors"] += 1
                self._running = False
                break

    def stop(self) -> None:
        """Stop consumer loop and close Kafka connection."""

        self._running = False

        if (
            self._thread
            and self._thread.is_alive()
            and threading.current_thread() != self._thread
        ):
            self._thread.join(timeout=3.0)

        if self._consumer:
            try:
                self._consumer.close()

                logger.info(
                    "Kafka Consumer closed successfully."
                )

            except Exception as exc:
                logger.warning(
                    "Error closing Kafka Consumer: %s",
                    exc,
                )

            finally:
                self._consumer = None

    close = stop

    def get_metrics(self) -> Dict[str, Any]:
        """Return consumer processing metrics."""

        elapsed = max(
            0.1,
            time.time() - self.metrics["start_time"],
        )

        throughput = round(
            self.metrics["consumed"] / elapsed,
            1,
        )

        return {
            **self.metrics,
            "uptime_seconds": round(elapsed, 1),
            "events_per_second": throughput,
        }


def main() -> None:

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "[%(asctime)s] %(levelname)s "
            "%(name)s: %(message)s"
        ),
    )

    parser = argparse.ArgumentParser(
        description="DineSight Kafka Consumer Service"
    )

    parser.add_argument(
        "--broker",
        default=KAFKA_BOOTSTRAP_SERVERS,
        help="Kafka broker host:port",
    )

    parser.add_argument(
        "--topic",
        default=KAFKA_TOPIC_EVENTS,
        help="Kafka topic to consume",
    )

    parser.add_argument(
        "--group-id",
        default=KAFKA_GROUP_ID,
        help="Consumer group ID",
    )

    args = parser.parse_args()

    print(
        f"Starting DineSight Kafka Consumer "
        f"on {args.broker} "
        f"(topic: {args.topic})..."
    )

    consumer = DineSightKafkaConsumer(
        bootstrap_servers=args.broker,
        group_id=args.group_id,
        topic=args.topic,
    )

    if not consumer._consumer:
        print(
            "Kafka Consumer could not be initialized. "
            "Check broker connection and confluent_kafka installation."
        )
        return

    try:

        while True:

            res = consumer.poll_and_process(
                timeout=1.0,
            )

            if res:

                metrics = consumer.get_metrics()

                print(
                    f"[Kafka Consumer] "
                    f"Event processed ({res.get('status')}) | "
                    f"Total: {metrics['consumed']} | "
                    f"Saved: {metrics['saved']} | "
                    f"Duplicates: {metrics['duplicates']} | "
                    f"Ignored: {metrics['ignored']} | "
                    f"Errors: {metrics['errors']} | "
                    f"Latency: {metrics['latest_latency_ms']} ms | "
                    f"Avg: {metrics['avg_latency_ms']} ms"
                )

                if res.get("status") == "error":
                    print(
                        "[Kafka Consumer] Stopping due to processing failure."
                    )
                    break

    except KeyboardInterrupt:
        print("\nStopping Kafka Consumer...")

    finally:
        consumer.stop()


if __name__ == "__main__":
    main()
