"""Idempotent seed entrypoint. The API startup also safely seeds the same defaults."""
import asyncio
from server import startup


if __name__ == "__main__":
    asyncio.run(startup())
    print("PAHEL FOUNDATION seed complete")