FROM python:3.12-slim

WORKDIR /app

COPY app.py .

RUN pip install --no-cache-dir flask requests m3u8

EXPOSE 5000

CMD ["python", "app.py"]
