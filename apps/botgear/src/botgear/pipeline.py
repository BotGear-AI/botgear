# semi-managed
import logging
import subprocess
import threading
import requests
import json
import os
import traceback
import asyncio
import time
import yaml

from config import create_app_settings
from botgear.models.config import Config as BotgearConfig

from database.interface import DatabaseAdapter
from database.factory import get_database_client
from queues.factory import get_queue
from datetime import datetime

logger = logging.getLogger(__name__)

class Pipeline:
    def __init__(self, settings: BotgearConfig):
        """
        Initializes the pipeline, including database provider and queue client.
        """
        logging.info(f"=== Initializing Pipeline ===")
        self.settings: BotgearConfig = settings
        self.db = get_database_client()
        self.queue_client = get_queue()
        self.port = self.settings.port
        self.ssl_enabled = self.settings.ssl_enabled        


    async def run(self):
        """Starts listening to the queue and processing messages."""
        logger.info(f"starting queue listener on queue \"input\" of type '{self.settings.queue_type}'...")


        while True:
            message = None
            try:
                if self.queue_client:
                    message = self.queue_client.receive_message()
                    if message:
                        logger.info(f"Received message: {message}")
                        # run = Run.model_validate(message)

                await asyncio.sleep(1)
            
            except Exception as e:

                # FIXME - finalize with error message
                logger.error(f"Error: {e} {traceback.format_exc()}")
                await asyncio.sleep(1)


if __name__ == "__main__":
    settings_obj, config_provider = create_app_settings("botgear", BotgearConfig)
    settings: BotgearConfig = settings_obj.to_config()
    logger.info(f"App Setting: {json.dumps(settings.model_dump(), indent=2)}")

    app_server = Pipeline(settings)
    import asyncio
    asyncio.run(app_server.run())
