import os
import asyncio

from datetime import datetime
from collections import deque
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from loguru import logger
from dotenv_vault import load_dotenv

load_dotenv()


def get_tg_client():
    # session = os.getenv('session','')
    session = 'newsparser'
    api_id = os.getenv('api_id','')
    api_hash = os.getenv('api_hash','')
    phone = os.getenv('phone','')
    bot_token = os.getenv('bot_token','')
    client = TelegramClient(session, api_id, api_hash)
    logger.info(f'Session: {client.session.save()}')
    return client


def telegram_parser(telegram_channels, posted_q, news_queue,
                    check_pattern_func=None, timeout=None):
    '''Телеграм парсер'''
    phone = os.getenv('phone')
    bot_token = os.getenv('bot_token','')
    client = get_tg_client()
    client.start(phone)
    logger.info(f'Client loged in')
    @client.on(events.NewMessage(chats=telegram_channels))
    async def handler(event):
        '''Забирает посты из телеграмм каналов и посылает их в наш канал'''
        if event.raw_text == '':
            return
        news_text = ' '.join(event.raw_text.split('\n'))
        if not (check_pattern_func is None):
            if not check_pattern_func(news_text):
                logger.debug('Filtered message')
                return
        title = event.raw_text.split('\n')[0]
        text = event.raw_text.split('\n')[1:]
        source = telegram_channels[event.message.peer_id.channel_id]
        link = f'{source}/{event.message.id}'
        channel = '@' + source.split('/')[-1]
        if title in posted_q:
            return
        
        source = telegram_channels[event.message.peer_id.channel_id]
        link = f'{source}/{event.message.id}'
        channel = '@' + source.split('/')[-1]
        today = datetime.today().strftime('%Y-%m-%d')
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        post = {'source':channel, 'title':title, 'text':text,
                'link':link, 'publish_dt':today, 'parsing_dttm':now}

        posted_q.appendleft(link)
        await news_queue.put(title)
            
        logger.info(f'Recieved message in telegram: {title}')
    return client

if __name__ == "__main__":
    telegram_channels = (
        'https://t.me/interfaxonline',
        'https://t.me/rbc_news',
        'https://t.me/rian_ru',
        'https://t.me/prime1',
        'https://t.me/bcs_express',
    )
    posted_q = deque(maxlen=20)
    news_queue = asyncio.Queue(maxsize=20)
    try:
        loop = asyncio.get_event_loop()
        client = telegram_parser(telegram_channels, posted_q, news_queue)
        client.run_until_disconnected()
    except KeyboardInterrupt:
        logger.info('Event loop were interrupted')
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()