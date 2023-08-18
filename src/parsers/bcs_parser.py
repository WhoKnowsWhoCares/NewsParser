import random
import asyncio

from collections import deque
from scrapy.selector import Selector
from loguru import logger
from datetime import datetime
from aiohttp import ClientSession

from src.utils.utils import random_user_agent_headers


async def bcs_parser(posted_q, news_queue,
                     timeout=2, check_pattern_func=None):
    '''Кастомный парсер сайта bcs-express.ru'''
    bcs_link = 'https://bcs-express.ru/category'
    source = 'www.bcs-express.ru'

    while True:
        try:
            header = random_user_agent_headers()
            async with ClientSession() as session:
                async with session.get(bcs_link,params=header) as response:
                    response_text = await response.text()
        except Exception as e:
            logger.error(f'{source} error pass\n{e}')
            await asyncio.sleep(timeout + random.uniform(0, 0.5))
            continue
        selector = Selector(text=response_text)

        for row in selector.xpath('//div[@class="feed__list"]/div/div')[::-1]:
            raw_text = row.xpath('*//text()').extract()

            title = raw_text[3] if len(raw_text) > 3 else ''
            summary = raw_text[5] if len(raw_text) > 5 else ''
            if 'ксперт' in summary:  # Эксперт
                title = f'{title}, {summary}'
                summary = raw_text[11] if len(raw_text) > 11 else ''

            if not (check_pattern_func is None):
                news_text = f'{title}\n{summary}'
                if not check_pattern_func(news_text):
                    logger.debug('Filtered by parser')
                    continue

            raw_link = row.xpath('a/@href').extract()
            link = raw_link[0] if len(raw_link) > 0 else ''
            if 'author' in link:
                link = raw_link[1] if len(raw_link) > 1 else ''
            if link in posted_q:
                continue

            today = datetime.today().strftime('%Y-%m-%d')
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            post = {'source':source, 'title':title, 'summary':summary,
                    'link':link, 'publish_dt':today, 'parsing_dttm':now}

            posted_q.appendleft(link)
            await news_queue.put(post)
            
            logger.debug(f'Recieved bcs post {source} - {title}')

        await asyncio.sleep(timeout + random.uniform(0, 0.5))
        
        
if __name__ == "__main__":
    posted_q = deque(maxlen=20)
    news_queue = asyncio.Queue(maxsize=20)
    try:
        loop = asyncio.get_event_loop()
        asyncio.run(asyncio.run(bcs_parser(posted_q, news_queue)))
    except KeyboardInterrupt:
        logger.info('Event loop were interrupted')
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()