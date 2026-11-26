# src/backend/kafka/config.py
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC_EVENTS = os.getenv("KAFKA_TOPIC_EVENTS", "dinesight.events.raw")
KAFKA_GROUP_ID = os.getenv("KAFKA_GROUP_ID", "dinesight-backend-group")
KAFKA_CLIENT_ID = os.getenv("KAFKA_CLIENT_ID", "dinesight-client")

