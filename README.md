# [@nstrainbot](https://telegram.me/nstrainbot)

## Running

### Docker

```bash
docker build -t ns-bot .
docker run -d --restart always -v /srv/ns-bot:/srv/ns-bot \
    --env NS_BOT_TELEGRAM_TOKEN='<TELEGRAM TOKEN>' \
    --env NS_BOT_BOTAN_TOKEN='<BOTAN.IO TOKEN>' \
    --env NS_API_LOGIN='<NS API LOGIN>' \
    --env NS_API_PASSWORD='<NS API PASSWORD>' \
    ns-bot
```
