import random
import asyncio
import feedparser

from aiohttp import ClientSession
from datetime import datetime
from collections import deque
from loguru import logger
from scrapy.selector import Selector

from src.utils.utils import random_user_agent_headers

async def get_text(link:str, rss_text_xpath: str):
    try:
        header = random_user_agent_headers()
        async with ClientSession() as session:
            async with session.get(link, params=header) as response:
                response_text = await response.text()
    except Exception as e:
        logger.error(f'rss error parsing text of {link}: {e}')
    selector = Selector(text=response_text)
    text = [row.extract().strip() for row in selector.xpath(rss_text_xpath)]
    text = '\n'.join(filter(None, text))
    logger.debug(f'Parsed text for link {link}: {text}')
    return text


async def rss_parser(source:str, rss_link:str, rss_text_xpath: str,
                     parsed_q:deque, queue,
                     timeout:int = 5, check_pattern_func = None):
    '''Парсер rss ленты'''
    while True:
        try:
            header = random_user_agent_headers()
            async with ClientSession() as session:
                async with session.get(rss_link,params=header) as response:
                    response_text = await response.text()
        except Exception as e:
            logger.error(f'rss error parsing on {source}: {e}')
            await asyncio.sleep(timeout - random.uniform(0, 0.5))
            continue
        feed = feedparser.parse(response_text)

        for entry in feed.entries[:20][::-1]:
            if 'summary' not in entry and 'title' not in entry:
                logger.debug('No title or summary found')
                continue
            summary = entry['summary'] if 'summary' in entry else ''
            title = entry['title'] if 'title' in entry else ''
            link = entry['link'] if 'link' in entry else ''
            if link in parsed_q:
                continue
            text = entry['text'] if 'text' in entry else ''
            if text == '':
                text = entry['full-text'] if 'full-text' in entry else ''
            if text == '':
                if rss_text_xpath and rss_text_xpath != '':
                    text = await get_text(link, rss_text_xpath)
            if not (check_pattern_func is None):
                news_text = f'{title}\n{summary}\n{text}'
                if not check_pattern_func(news_text):
                    logger.debug('Filtered by parser')
                    continue
            today = datetime.today().strftime('%Y-%m-%d')
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            post = {'source':source, 'title':title, 'summary':summary, 'text':text,
                    'link':link, 'publish_dt':today, 'parsing_dttm':now}

            parsed_q.appendleft(link)
            await queue.put(post)
            
            logger.debug(f'Recieved rss post {source} - {title}')

        await asyncio.sleep(timeout - random.uniform(0, 0.5))


if __name__ == "__main__":
    source = 'www.rbc.ru'
    # rss_link = 'https://rssexport.rbc.ru/rbcnews/news/20/full.rss'
    rss_link = 'https://1prime.ru/export/rss2/index.xml'
    parsed_q = deque(maxlen=20)
    news_queue = asyncio.Queue(maxsize=20)
    try:
        loop = asyncio.get_event_loop()
        asyncio.run(rss_parser(source, rss_link, '', parsed_q, news_queue))
    except KeyboardInterrupt:
        logger.info('Event loop were interrupted')
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()