import random
import asyncio
import feedparser

from datetime import datetime
from collections import deque
from loguru import logger
from httpx import AsyncClient

from src.utils.utils import random_user_agent_headers

#TODO: Make parser go by link and parse full text of the post
async def rss_parser(source:str, rss_link:str, parsed_q:deque, queue,
                     n_test_chars:int = 50, timeout:int = 2, check_pattern_func = None):
    '''Парсер rss ленты'''
    httpx_client = AsyncClient()
    while True:
        try:
            header = random_user_agent_headers()
            response = await httpx_client.get(rss_link, headers=header)
            response.raise_for_status()
        except Exception as e:
            logger.error(f'rss error parsing\n{e}')
            await asyncio.sleep(timeout*2 - random.uniform(0, 0.5))
            continue
        # logger.info(f'Passed header: {header}')
        feed = feedparser.parse(response.text)

        for entry in feed.entries[:20][::-1]:
            if 'summary' not in entry and 'title' not in entry:
                logger.info('No title or summary found')
                continue
            summary = entry['summary'] if 'summary' in entry else ''
            title = entry['title'] if 'title' in entry else ''
            news_text = f'{title}\n{summary}'
            if not (check_pattern_func is None):
                if not check_pattern_func(news_text):
                    logger.info('Filtered by parser')
                    continue
            key = news_text[:n_test_chars].strip()
            if key in parsed_q:
                continue
            link = entry['link'] if 'link' in entry else ''
            today = datetime.today().strftime('%Y-%m-%d')
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            post = {'source':source, 'title':title, 'summary':summary, 
                    'link':link, 'publish_dt':today, 'parsing_dttm':now}

            parsed_q.appendleft(key)
            await queue.put(post)
            
            logger.info(post)

        await asyncio.sleep(timeout - random.uniform(0, 0.5))


if __name__ == "__main__":
    
    source = 'www.rbc.ru'
    rss_link = 'https://rssexport.rbc.ru/rbcnews/news/20/full.rss'
    parsed_q = deque(maxlen=20)
    news_queue = asyncio.Queue(maxsize=20)
    
    asyncio.run(rss_parser(source, rss_link, parsed_q, news_queue))