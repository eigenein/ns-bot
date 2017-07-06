# [@nstrainbot](https://telegram.me/nstrainbot)

## Running

### Docker

```bash
mkdir -p /srv/ns-bot
cat .env
# NS_BOT_TELEGRAM_TOKEN='<TELEGRAM TOKEN>'
# NS_API_LOGIN='<NS API LOGIN>'
# NS_API_PASSWORD='<NS API PASSWORD>'

docker build -t ns-bot .
docker-compose up -d
```
