FROM python:3.11-slim

WORKDIR /app

COPY config.json reposter.py ./

RUN mkdir -p images_queue

CMD ["python", "-u", "reposter.py"]
