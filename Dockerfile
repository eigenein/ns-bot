FROM ubuntu:17.04
MAINTAINER Pavel Perestoronin <eigenein@gmail.com>

ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

RUN apt update && apt -y install python3-pip

COPY . /opt/ns-bot
WORKDIR /opt/ns-bot

RUN python3 -m pip install -r requirements.txt

RUN mkdir -p /srv/ns-bot
VOLUME /srv/ns-bot

CMD ["python3", "bot.py",  "--verbose", "--log-file /srv/ns-bot/ns-bot.log"]
