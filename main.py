import sys
import asyncio

from collections import deque
from contextlib import suppress
from loguru import logger
from dotenv_vault import load_dotenv

from src.parsers.telegram_parser import telegram_parser
from src.parsers.rss_parser import rss_parser
from src.parsers.bcs_parser import bcs_parser
from src.utils.mongo_utils import get_history, get_db_connection, insert_record_into_db
from src.utils.telegram_utils import init_bot

# Load environment variables
load_dotenv()

logger.remove()
LOGGING_LVL = 'WARNING'
logger.add("./logs/log_{time:YYYY-MM-DD}.log", level=LOGGING_LVL,
           rotation="00:01", retention="90 days",
           backtrace=True, diagnose=True)
logger.add(sys.stdout, level=LOGGING_LVL)

###########################
# Можно добавить телеграм канал, rss ссылку или изменить фильтр новостей

telegram_channels = (
    'https://t.me/interfaxonline',
    # 'https://t.me/rbc_news',
    # 'https://t.me/rian_ru',
    # 'https://t.me/prime1',
    # 'https://t.me/bcs_express',
    # 'https://t.me/markettwits',
    # 'https://t.me/vadya93_official',
)

rss_channels = {
    'www.rbc.ru': ('https://rssexport.rbc.ru/rbcnews/news/20/full.rss', 
                   '//div[contains(@class,"article__text")]/p/text()'),
    'www.ria.ru': ('https://ria.ru/export/rss2/archive/index.xml',
                   '//div[contains(@class,"article__text")]/text()'),
    'www.1prime.ru': ('https://1prime.ru/export/rss2/index.xml',
                      '//div[contains(@class,"article-body")]/p/text()'),
    'www.interfax.ru': ('https://www.interfax.ru/rss.asp',
                        '//article[@itemprop="articleBody"]/p/text()'),
}


###########################
# Если у парсеров много ошибок или появляются повторные новости

# Количество уже опубликованных постов, чтобы их не повторять
amount_messages = 10

# +/- интервал между запросами у rss и кастомного парсеров в секундах
timeout = 10

###########################


async def fetch_news(parser, args):
    '''
    For each source in list run parser which would put news in queue
    '''
    logger.debug("Fetching news")
    try:
        await parser(*args, timeout=timeout)
    except Exception as e:
        message = f'ERROR: {parser} parser is down! \n{e}'
        logger.error(message)


async def process_news(db_connection, news_queue, tg_queue):
    '''
    For each news in queue paste it into database, send to telegram
    '''
    logger.debug("Processing news")
    while True:
        news = await news_queue.get()
        result = insert_record_into_db(db_connection, news)
        if result:
            await tg_queue.put(news)
        news_queue.task_done()
   

async def main():
    news_queue = asyncio.Queue(maxsize = amount_messages)
    tg_queue = asyncio.Queue(maxsize = amount_messages)
    parsed_q = deque(maxlen = 10*amount_messages)
    connection = get_db_connection()
    parsed_q.extend(get_history(connection, amount_messages=10*amount_messages))
    logger.debug(f"DB history news ready")
    
    tg_bot = asyncio.create_task(init_bot(tg_queue))
    processor = asyncio.create_task(process_news(connection, news_queue, tg_queue))    
    parsers = []
    # parsers.append(asyncio.create_task(fetch_news(telegram_parser, (loop, telegram_channels, parsed_q, news_queue))))
    parsers.append(asyncio.create_task(fetch_news(bcs_parser, (parsed_q, news_queue))))
    for source, (rss_link, rss_text_xpath) in rss_channels.items():
        parsers.append(
            asyncio.create_task(
                fetch_news(rss_parser,(source, rss_link,rss_text_xpath,parsed_q, news_queue))
            )
        )
    
    await asyncio.gather(processor, tg_bot, *parsers, news_queue.join(), tg_queue.join())
    logger.debug('Main processing finished')


if __name__ == '__main__':
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info('Event loop were interrupted')
    finally:
        for task in asyncio.all_tasks(loop):
            task.cancel()