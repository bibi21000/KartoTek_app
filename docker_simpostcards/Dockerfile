# syntax=docker/dockerfile:1

# base python image for custom image
FROM python:3.14-slim

ENV PIP_NO_CACHE_DIR=false

# create working directory and install pip dependencies
RUN mkdir -p /data
COPY pyproject.toml /app/
COPY src /app/src/
COPY docker_simpostcards/postcards.conf /app/
WORKDIR /app
RUN pip3 install . && pip3 install .[similar] && pip3 install .[simpostcards] && pip3 install .[flask] && pip3 install .[prod]

EXPOSE 8004

VOLUME ["/data"]

# run the flask server
CMD [ "gunicorn", "--workers=4", "--bind=0.0.0.0:8001", "simpostcards.run:app"]
