import random
import asyncio
import httpx

from collections import deque
from scrapy.selector import Selector
from loguru import logger
from datetime import datetime

from src.utils.utils import random_user_agent_headers, random_user_agent_header_from_file


async def bcs_parser(posted_q, news_queue,
                     n_test_chars=50, timeout=2, check_pattern_func=None):
    '''Кастомный парсер сайта bcs-express.ru'''
    bcs_link = 'https://bcs-express.ru/category'
    source = 'www.bcs-express.ru'
    httpx_client = httpx.AsyncClient()

    while True:
        try:
            header = random_user_agent_headers()
            response = await httpx_client.get(bcs_link, headers=header)
            response.raise_for_status()
        except Exception as e:
            logger.error(f'{source} error pass\n{e}')
            await asyncio.sleep(timeout + random.uniform(0, 0.5))
            continue
        logger.debug(f'Successfull header: {header}')
        selector = Selector(text=response.text)

        for row in selector.xpath('//div[@class="feed__list"]/div/div')[::-1]:
            raw_text = row.xpath('*//text()').extract()

            title = raw_text[3] if len(raw_text) > 3 else ''
            summary = raw_text[5] if len(raw_text) > 5 else ''
            if 'ксперт' in summary:  # Эксперт
                title = f'{title}, {summary}'
                summary = raw_text[11] if len(raw_text) > 11 else ''
            news_text = f'{title}\n{summary}'

            if not (check_pattern_func is None):
                if not check_pattern_func(news_text):
                    logger.debug('Filtered by parser')
                    continue

            head = title[:n_test_chars]
            if head in posted_q:
                continue

            raw_link = row.xpath('a/@href').extract()
            link = raw_link[0] if len(raw_link) > 0 else ''
            if 'author' in link:
                link = raw_link[1] if len(raw_link) > 1 else ''

            today = datetime.today().strftime('%Y-%m-%d')
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            post = {'source':source, 'title':title, 'summary':summary,
                    'link':link, 'publish_dt':today, 'parsing_dttm':now}

            posted_q.appendleft(head)
            await news_queue.put(post)

        await asyncio.sleep(timeout + random.uniform(0, 0.5))
        
        
if __name__ == "__main__":

    # Очередь из уже опубликованных постов, чтобы их не дублировать
    posted_q = deque(maxlen=20)
    news_queue = asyncio.Queue(maxsize=20)

    asyncio.run(bcs_parser(posted_q, news_queue))