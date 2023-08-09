import random
import asyncio
import feedparser

from collections import deque
from loguru import logger
from httpx import AsyncClient

from src.utils.utils import random_user_agent_headers

async def rss_parser(httpx_client:AsyncClient, source:str, rss_link:str, parsed_q:deque, posted_q:deque,
                     n_test_chars:int = 50, timeout:int = 2, check_pattern_func = None, logger = None):
    '''Парсер rss ленты'''

    while True:
        try:
            response = await httpx_client.get(rss_link, headers=random_user_agent_headers())
            response.raise_for_status()
        except Exception as e:
            if not (logger is None):
                logger.error(f'rss error parsing\n{e}')
            await asyncio.sleep(timeout*2 - random.uniform(0, 0.5))
            continue

        feed = feedparser.parse(response.text)

        for entry in feed.entries[:20:-1]:
            if 'summary' not in entry and 'tittle' not in entry:
                continue
            summary = entry['summary'] if 'summary' in entry else ''
            title = entry['title'] if 'title' in entry else ''
            news_text = f'{title}\n{summary}'
            if not (check_pattern_func is None):
                if not check_pattern_func(news_text):
                    continue
            key = news_text[:n_test_chars].strip()
            if key in parsed_q:
                continue
            link = entry['link'] if 'link' in entry else ''
            post = {'source':source, 'title':title, 'summary':summary, 'link':link}

            if logger is None:
                print(post, '\n')
            else:
                logger.info(post)

            parsed_q.appendleft(key)
            posted_q.append(post)

        await asyncio.sleep(timeout - random.uniform(0, 0.5))


if __name__ == "__main__":
    source = 'www.rbc.ru'
    rss_link = 'https://rssexport.rbc.ru/rbcnews/news/20/full.rss'
    parsed_q = deque(maxlen=20)
    posted_q = deque(maxlen=20)
    httpx_client = AsyncClient()
    asyncio.run(rss_parser(httpx_client, source, rss_link, parsed_q, posted_q, logger=logger))