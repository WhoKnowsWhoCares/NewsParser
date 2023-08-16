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


async def get_tg_client(loop):
    session = os.getenv('session')
    api_id = os.getenv('api_id')
    api_hash = os.getenv('api_hash')
    phone = os.getenv('phone')
    if not session or session == '':
        print('crap')
        client = await TelegramClient(StringSession(), api_id, api_hash, loop=loop).start()
        logger.info(f'Session: {client.session.save()}')
    else:
        print('ok')
        client = await TelegramClient(StringSession(session), api_id, api_hash, loop=loop).start()
    return client


async def telegram_parser(loop, telegram_channels, posted_q, news_queue,
                    n_test_chars=50, check_pattern_func=None, timeout=None):
    '''Телеграм парсер'''
    phone = os.getenv('phone')
    client = await get_tg_client(loop)
    if await client.is_user_authorized() == False:
        await client.send_code_request(phone)
        try:
            await client.sign_in(phone, input('Enter the code: '))
        except SessionPasswordNeededError:
            await client.sign_in(password=input('Password: '))
    
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
        head = title[:n_test_chars]
        if head in posted_q:
            return
        
        source = telegram_channels[event.message.peer_id.channel_id]
        link = f'{source}/{event.message.id}'
        channel = '@' + source.split('/')[-1]
        today = datetime.today().strftime('%Y-%m-%d')
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        post = {'source':channel, 'title':title, 'text':text,
                'link':link, 'publish_dt':today, 'parsing_dttm':now}

        posted_q.appendleft(head)
        await news_queue.put(post)
            
        logger.info(f'Recieved message in telegram: {head}')

    await client.run_until_disconnected()


if __name__ == "__main__":
    telegram_channels = ('https://t.me/interfaxonline',)
    posted_q = deque(maxlen=20)
    news_queue = asyncio.Queue(maxsize=20)
    loop = asyncio.get_event_loop()
    try:
        ret = loop.run_until_complete(telegram_parser(loop, telegram_channels, posted_q, news_queue)) or 0
    except KeyboardInterrupt:
        ret = 1
    loop.stop()
    loop.close()
    exit(ret)