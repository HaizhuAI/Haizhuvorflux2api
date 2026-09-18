import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

import uvicorn

import config

if __name__ == "__main__":
    uvicorn.run("main:app", host=config.HOST, port=config.PORT,
                log_level="info", loop="asyncio")
