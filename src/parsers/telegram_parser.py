import os
import asyncio
import uvloop

from pathlib import Path
from datetime import datetime
from collections import deque
from pyrogram import Client, filters, idle
from loguru import logger
from dotenv_vault import load_dotenv

load_dotenv()


def init_parser(telegram_channels, posted_q, news_queue,
                    check_pattern_func=None):
    session = 'newsparser'
    session_file = Path(f"./{session}.session")
    if session_file.exists():
        app = Client(session)
    else:
        api_id = os.getenv('api_id','')
        api_hash = os.getenv('api_hash','')
        app = Client(session, api_id=api_id, api_hash=api_hash)
    
    @app.on_message(filters.text)
    async def readMessages(client, message):
        '''Забирает посты из телеграмм каналов и посылает их в наш канал'''
        logger.info(f'Got message in telegram')
        message_id = message.id
        chat_id = message.chat.id
        chat_name = message.chat.username
        if chat_id not in telegram_channels and chat_name not in telegram_channels:
            return
        text = message.text
        if not (check_pattern_func is None):
            if not check_pattern_func(text):
                logger.debug('Filtered message')
                return
        title = text.split('\n')[0]
        source = f'https://t.me/{chat_name}'
        link = f'{source}/{message_id}'
        today = datetime.today().strftime('%Y-%m-%d')
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        post = {'source':source, 'title':title, 'text':text,
                'link':link, 'publish_dt':today, 'parsing_dttm':now}

        await news_queue.put(post)
        logger.info(f'Recieved message in telegram: {text}')
        
    logger.debug(f'TG client created')
    return app
    
async def telegram_parser(telegram_channels, posted_q, news_queue, timeout=None):
    app = init_parser(telegram_channels, posted_q, news_queue)
    await app.start()
    await idle()
    await app.stop()
    raise KeyboardInterrupt


if __name__ == "__main__":
    telegram_channels = (
        -910844647,
    )
    posted_q = deque(maxlen=20)
    news_queue = asyncio.Queue(maxsize=20)
    
    try:
        # client = init_parser(telegram_channels, posted_q, news_queue)
        # client.run()
        uvloop.install()
        asyncio.run(telegram_parser(telegram_channels, posted_q, news_queue))
    except KeyboardInterrupt:
        logger.info('Event loop were interrupted')
    except Exception as e:
        logger.error(f'Error in telegram parser: {e}')
        
        
    
  