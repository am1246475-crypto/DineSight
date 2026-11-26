# src/backend/kafka/__init__.py
"""DineSight Kafka Integration Package."""
from .config import KAFKA_BOOTSTRAP_SERVERS, KAFKA_TOPIC_EVENTS, KAFKA_GROUP_ID
from .producer import DineSightKafkaProducer
from .consumer import DineSightKafkaConsumer

__all__ = [
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_TOPIC_EVENTS",
    "KAFKA_GROUP_ID",
    "DineSightKafkaProducer",
    "DineSightKafkaConsumer",
]

