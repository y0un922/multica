"""Local A/B contract demo with a fake Backend B, not an HTTP server."""
import asyncio

from examples.ab_demo import main


if __name__ == "__main__":
    asyncio.run(main())
