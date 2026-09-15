FROM python:3.14.7-alpine3.24

WORKDIR /app

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "start_watchman_demo.sh"]