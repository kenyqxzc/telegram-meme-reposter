FROM python:3.11-slim

WORKDIR /app

COPY config.json reposter.py ./

RUN mkdir -p images_queue

ENV PORT=10000
EXPOSE 10000

CMD ["python", "-u", "reposter.py"]
