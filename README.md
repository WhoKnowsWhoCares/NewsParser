# NewsParser - news aggregator with ML improvements
Get news from telegram/rss/web, summarize and show only best relevant according to user preferences. \
Uses MongoDB as news storage, supposing you already have it configured and running. \
For telegram parsing you need to have api_id and api_hash from [my.telegram.org](https://my.telegram.org/auth) \
And for providing information to you it users telegram bot, so you need to have one. \
As base for this project was used another [repo](https://github.com/cdies/simple_news_aggregator) 

## How to install
### Install software via terminal
First of all you need to have [Poetry](https://python-poetry.org/)
```
pip install poetry
```

### Clone github project, install dependencies
```
 git clone https://github.com/whoknowswhocares/newsparser \
 cd ./newsparser \
 poetry install
```

### Configure .env file
Rename .ent_template to .env and provide some info:
- `api_id, api_hash` - paramenters for telegram channels parsing from [my.telegram.org](https://my.telegram.org)
- `bot_token` - your bot's token from @BotFather
- `chat_id` - your chat id to initialize parsing session and to where you will recieve news
- `mongo_*` - credentials for MongoDB


## Current news sources:
> telegram channels
- [@rbc_news](https://t.me/rbc_news)
- [@rian_ru](https://t.me/rian_ru)
- [@prime1](https://t.me/prime1)
- [@interfaxonline](https://t.me/interfaxonline)
- [@bcs_express](https://t.me/bcs_express)

> RSS 
- [www.rbc.ru](https://.rbc.ru)
- [www.ria.ru](https://ria.ru)
- [www.1prime.ru](https://1prime.ru)
- [www.interfax.ru](https://www.interfax.ru)



