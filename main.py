import sys
import asyncio
import concurrent.futures

from collections import deque
from telethon import TelegramClient
from loguru import logger
from dotenv_vault import load_dotenv

from src.utils.mongo_utils import get_history, get_db_connection, insert_record_into_db
from src.utils.telegram_utils import init_bot, send_tg_message_to_all

# Load environment variables
load_dotenv()

from src.parsers.telegram_parser import telegram_parser
from src.parsers.rss_parser import rss_parser
from src.parsers.bcs_parser import bcs_parser

logger.remove()
logger.add("./logs/log_{time:YYYY-MM-DD}.log", level='DEBUG',
           rotation="00:01", retention="90 days",
           backtrace=True, diagnose=True)
logger.add(sys.stdout, level='DEBUG')

###########################
# Можно добавить телеграм канал, rss ссылку или изменить фильтр новостей

telegram_channels = (
    'https://t.me/interfaxonline',
    'https://t.me/rbc_news',
    'https://t.me/rian_ru',
    'https://t.me/prime1',
    'https://t.me/bcs_express',
    'https://t.me/markettwits',
    'https://t.me/vadya93_official',
)

rss_channels = {
    'www.rbc.ru': 'https://rssexport.rbc.ru/rbcnews/news/20/full.rss',
    # 'www.ria.ru': 'https://ria.ru/export/rss2/archive/index.xml',
    # 'www.1prime.ru': 'https://1prime.ru/export/rss2/index.xml',
    # 'www.interfax.ru': 'https://www.interfax.ru/rss.asp',
}


###########################
# Если у парсеров много ошибок или появляются повторные новости

# 50 первых символов от поста - это ключ для поиска повторных постов
n_test_chars = 50

# Количество уже опубликованных постов, чтобы их не повторять
amount_messages = 50

# +/- интервал между запросами у rss и кастомного парсеров в секундах
timeout = 10

###########################


# async def fetch_news(source, rss_link, parsed_q, queue):
async def fetch_news(parser, args):
    '''
    For each source in list run parser which would put news in queue
    '''
    logger.debug("Fetching news")
    try:
        await parser(*args, n_test_chars=n_test_chars, timeout=timeout)
    except Exception as e:
        message = f'ERROR: {parser} parser is down! \n{e}'
        logger.error(message)


async def process_news(db_connection, queue):
    '''
    For each news in queue paste it into database, send to telegram
    '''
    logger.debug("Processing news")
    while True:
        news = await queue.get()
        res_code = insert_record_into_db(db_connection, news)
        if res_code:
            await send_tg_message_to_all(news)
        queue.task_done()
   

async def main():
    news_queue = asyncio.Queue(maxsize = amount_messages)
    parsed_q = deque(maxlen = amount_messages)
    connection = get_db_connection()
    parsed_q.extend(get_history(connection, n_test_chars=n_test_chars, amount_messages=amount_messages))
    logger.debug(f"DB history news ready")
    
    # with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
    #     processor = executor.submit(asyncio.run, process_news(connection, news_queue))
    #     tg_bot = executor.submit(init_bot())
    #     parsers = []
    #     for source, rss_link in rss_channels.items():
    #         parsers.append(executor.submit(asyncio.run, fetch_news(rss_parser, (source, rss_link, parsed_q, news_queue))))  

    # init_bot()
    tg_bot = asyncio.create_task(init_bot())
    processor = asyncio.create_task(process_news(connection, news_queue))    
    parsers = []
    parsers.append(asyncio.create_task(fetch_news(bcs_parser, (parsed_q, news_queue))))
    # parsers.append(asyncio.create_task(fetch_news(telegram_parser, (telegram_channels, parsed_q, news_queue))))
    # for source, rss_link in rss_channels.items():
    #     parsers.append(asyncio.create_task(fetch_news(rss_parser, (source, rss_link, parsed_q, news_queue))))
    
    await asyncio.gather(processor, tg_bot, *parsers, news_queue.join())
    logger.debug('Main processing finished')


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())